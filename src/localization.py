import ast
import concurrent.futures
import json
import multiprocessing
import os
import re
import signal
import sys
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import chain
from multiprocessing import Process, Queue
from typing import Dict, List, Set

from call_graph_generation import get_callgraph

import prompts
from call_graph_generation import (get_all_forward_callees, related_mode,
                                   run_with_timeout_relatedmode)
from locate_tests import locate_tests
from model_config import generate_text, prompt_model
from utils import (check_exact_string_in_file, check_if_def_used_in_file,
                   check_substring_in_file, cleanup_logger,
                   count_statistics, dump_json, extract_function_defs,
                   find_def_paths, get_key_info_from_code, get_repo_structure,
                   prepare_repo, read_file, search_pyfiles_by_key_or_name,
                   search_python_files_for_string, setup_logger)

# from find_test_for_focal import setup_repo_collect_tests, get_cov_per_testmethod



def get_instance_data(instance_id, dataset):
    for instance in dataset:
        if instance["instance_id"] == instance_id:
            return instance
    return None


class Inspector:
    def __init__(self, logger, instance_id, dataset_name, dataset):
        self.logger = logger
        self.instance_id = instance_id
        self.dataset = dataset
        self.dataset_name = dataset_name
        self.instance_data = get_instance_data(self.instance_id, self.dataset)
        if self.instance_data is None:
            logger.error(f"Instance {instance_id} not found in the dataset.")
            sys.exit(1)
        self.problem_statement = f'{self.instance_data["problem_statement"]}\n{self.instance_data["hints_text"]}'
        self.repo_name = self.instance_data["repo"]
        self.base_commit = self.instance_data["base_commit"]
        self.local_repo_path = prepare_repo(self.repo_name, self.base_commit)
        self.repo_structure = get_repo_structure(self.local_repo_path)
        self.logger.info(
            f"Inspecting instance {self.instance_id} in dataset {self.dataset_name}"
        )
        self.non_test_pyfiles = self.get_non_test_pyfiles()
        # self.all_fq_tests = setup_repo_collect_tests(self.local_repo_path, self.base_commit)
        # self.logger.info(f"{len(self.all_fq_tests)} test files found in the repository.")
        self.logger.info(f"Repository: {self.repo_name} at commit {self.base_commit}")
        self.logger.info(f"Problem statement: {self.problem_statement}")
        self.logger.info(f"Local repository path: {self.local_repo_path}")
        # self.logger.info(f"Repository structure: {json.dumps(self.repo_structure, indent=2)}")

    def get_non_test_pyfiles(self):
        non_test_pyfiles = {}
        self.logger.info("Getting non-test files from the repository structure.")
        self.logger.info(
            f"Repository structure: {json.dumps(self.repo_structure, indent=2)}"
        )

        def filter_dict(d, path=""):
            result = {}
            for key, value in d.items():
                current_path = f"{path}/{key}" if path else key
                parts = current_path.lower().split("/")
                if "test" in parts or "tests" in parts:
                    continue
                if isinstance(value, dict):
                    filtered = filter_dict(value, current_path)
                    if filtered:
                        result[key] = filtered
                else:
                    if isinstance(value, list):
                        value = [v for v in value if v.endswith(".py")]
                    if len(value) > 0:
                        result[key] = value
            return result

        non_test_pyfiles = filter_dict(self.repo_structure)
        self.logger.info(
            f"Non-test files found: {json.dumps(non_test_pyfiles, indent=2)}"
        )
        return non_test_pyfiles

    def prompt_model(self, model, prompt):
        self.logger.info(f"Prompting model {model} with the following prompt:")
        self.logger.info(prompt)
        try:
            # response = generate_text(model, prompt)
            response = prompt_model(model, prompt)

            self.logger.info(f"Model response: {response}")
            return response

        except Exception as e:
            self.logger.error(f"Error generating text with model {model}: {e}")
            # TODO: error handling

    def parse_suspicious_info(self, model_response):
        self.logger.info("Parsing suspicious information from model response.")
        suspicious_methods, suspicious_classes, suspicious_files = [], [], []
        try:
            if (
                "---BEGIN SUSPICIOUS METHODS---" in model_response
                and "---END SUSPICIOUS METHODS---" in model_response
            ):
                suspicious_methods = (
                    model_response.split("---BEGIN SUSPICIOUS METHODS---")[1]
                    .split("---END SUSPICIOUS METHODS---")[0]
                    .strip()
                    .split("\n")
                )
            if (
                "---BEGIN SUSPICIOUS CLASSES---" in model_response
                and "---END SUSPICIOUS CLASSES---" in model_response
            ):
                suspicious_classes = (
                    model_response.split("---BEGIN SUSPICIOUS CLASSES---")[1]
                    .split("---END SUSPICIOUS CLASSES---")[0]
                    .strip()
                    .split("\n")
                )
            if (
                "---BEGIN SUSPICIOUS FILES---" in model_response
                and "---END SUSPICIOUS FILES---" in model_response
            ):
                suspicious_files = (
                    model_response.split("---BEGIN SUSPICIOUS FILES---")[1]
                    .split("---END SUSPICIOUS FILES---")[0]
                    .strip()
                    .split("\n")
                )
            suspicious_methods = [
                method.strip() for method in suspicious_methods if method.strip()
            ]
            suspicious_classes = [
                cls.strip() for cls in suspicious_classes if cls.strip()
            ]
            for class_name in suspicious_classes:
                if "." in class_name:
                    class_name = class_name.split(".")[0]
                    if class_name not in suspicious_classes:
                        suspicious_classes.append(class_name.strip())
            suspicious_files = [
                file.strip() for file in suspicious_files if file.strip()
            ]
        except IndexError as e:
            self.logger.error(f"Error parsing model response: {e}")
            self.logger.error("Model response might not be in the expected format.")

        self.logger.info(f"Suspicious methods: {suspicious_methods}")
        self.logger.info(f"Suspicious classes: {suspicious_classes}")
        self.logger.info(f"Suspicious files: {suspicious_files}")
        return {
            "suspicious_methods": suspicious_methods,
            "suspicious_classes": suspicious_classes,
            "suspicious_files": suspicious_files,
        }

    def generate_call_paths_json(self, call_path_dir="call_paths"):
        repo_shortcut = self.repo_name.split("/")[-1]
        base_commit = self.base_commit
        ts_dir = f"{call_path_dir}/{repo_shortcut}_{base_commit}"
        os.makedirs(ts_dir, exist_ok=True)

        # get_callgraph() caches its output — returns immediately if already exists
        callgraph_json = get_callgraph(self.local_repo_path, ts_dir)
        print(f"Call graph JSON at {callgraph_json}")
        return callgraph_json

    def get_call_graph_json(self):
        self.logger.info("Generating call graph JSON for the repository.")
        call_paths_json = self.generate_call_paths_json()
        self.logger.info(f"Call graph JSON saved at {call_paths_json}")
        return call_paths_json

    def catogarize_file_type(self, files):
        categorized_files = {"test": [], "non_test": []}
        for file_path in files:
            if (
                re.search("test", file_path, re.IGNORECASE)
                and file_path not in categorized_files["test"]
            ):
                full_path = os.path.join(self.local_repo_path, file_path)
                categorized_files["test"].append(full_path)
            elif file_path not in categorized_files["non_test"]:
                full_path = os.path.join(self.local_repo_path, file_path)
                categorized_files["non_test"].append(full_path)
        return categorized_files

    def symbolic_loc_suspicious(self, suspicious_info):
        self.logger.info(
            "Symbolically locating files from model initial extraction results."
        )
        # self.logger.info(f"Repository structure: {json.dumps(self.repo_structure, indent=2)}")
        located_files = {}
        for key in suspicious_info:
            if len(suspicious_info[key]) > 0:
                for item in suspicious_info[key]:
                    if item not in located_files:
                        located_files[item] = {"test": [], "non_test": []}
                    self.logger.info(
                        f"Searching for suspicious files in the repository structure for {key}: {item}"
                    )
                    files = search_pyfiles_by_key_or_name(self.repo_structure, item)
                    self.logger.info(f"Found suspicious file paths: {files}")
                    if files:
                        catogarize_files = self.catogarize_file_type(files)
                        # located_files[item]['test'] = catogarize_files['test']
                        located_files[item]["non_test"] = catogarize_files["non_test"]
                    else:
                        self.logger.warning(
                            f"No files found for suspicious {key}: {item}; Will try to match seperately."
                        )
                        if "." in item:  # e.g., Field.max_length
                            class_name = item.split(".")[
                                0
                            ]  # fow now, only take the first part classname
                            files = search_pyfiles_by_key_or_name(
                                self.repo_structure, class_name
                            )
                            self.logger.info(
                                f"Found suspicious file paths for {class_name}: {files}"
                            )
                            if files:
                                catogarize_files = self.catogarize_file_type(files)
                                # located_files[item]['test'] = catogarize_files['test']
                                located_files[item]["non_test"] = catogarize_files[
                                    "non_test"
                                ]

        self.logger.info(
            f"Located suspicious files: {json.dumps(located_files, indent=2)}"
        )
        return located_files

    def finegrain_loc_suspicious(self, symbolic_located_files):
        self.logger.info("Fine-graining symbolic location of suspicious files.")
        for item in symbolic_located_files:
            if item.endswith(".py"):
                continue
            else:
                if "." not in item:
                    located_files = find_def_paths(self.local_repo_path, item)
                    for file_path in located_files:
                        if file_path not in symbolic_located_files[item]["non_test"]:
                            symbolic_located_files[item]["non_test"].append(file_path)
                else:
                    sep_components = item.split(".")
                    class_name = sep_components[0]
                    attr = sep_components[-1]
                    located_files = find_def_paths(
                        self.local_repo_path, class_name
                    )  # full path here
                    for file_path in located_files:
                        if file_path not in symbolic_located_files[item][
                            "non_test"
                        ] and check_substring_in_file(file_path, attr):
                            symbolic_located_files[item]["non_test"].append(file_path)

        self.logger.info(
            f"Fine-grained located files: {json.dumps(symbolic_located_files, indent=2)}"
        )
        return symbolic_located_files

    def filter_out_irrelevant_files(self, located_files):
        self.logger.info("Filtering out irrelevant files from the located files.")
        filtered_files = {}
        for item in located_files:
            if item not in filtered_files:
                filtered_files[item] = {"test": [], "non_test": []}
            if item.endswith(".py"):
                filtered_files[item]["non_test"] = located_files[item]["non_test"]
                continue

            self.logger.info(f"Filtering files for {item}.")
            self.logger.info(
                f"Num: {len(located_files[item]['non_test'])} non-test files found."
            )

            num = 0

            for file in located_files[item]["non_test"]:
                num += 1
                if "." in item:  # e.g., Field.max_length
                    class_name = item.split(".")[0]
                else:
                    class_name = item

                if (
                    check_if_def_used_in_file(file, class_name, self.logger)
                    and file not in filtered_files[item]["non_test"]
                ):
                    filtered_files[item]["non_test"].append(file)
                else:
                    self.logger.info(
                        f"File {file} does not define or call {class_name}. Removing it from located files."
                    )

        self.logger.info(
            f"Filtered located files: {json.dumps(filtered_files, indent=2)}"
        )
        return filtered_files

    def get_relative_path(self, file_path):
        if file_path.startswith(self.local_repo_path):
            return file_path[len(self.local_repo_path) :].lstrip(os.sep)

    def from_path_to_classname(self, relative_file_path):
        if not relative_file_path.endswith(".py"):
            self.logger.error(f"File {relative_file_path} is not a Python file.")
            return None
        return relative_file_path.replace(os.sep, ".").replace(".py", "")

    def parse_relevant_files(self, model_response):
        self.logger.info("Parsing relevant files from model response.")
        relevant_files = []
        try:
            if (
                "---BEGIN RELEVANT FILES---" in model_response
                and "---END RELEVANT FILES---" in model_response
            ):
                relevant_files = (
                    model_response.split("---BEGIN RELEVANT FILES---")[1]
                    .split("---END RELEVANT FILES---")[0]
                    .strip()
                    .split("\n")
                )
                relevant_files = [
                    file.strip() for file in relevant_files if file.strip()
                ]
        except IndexError as e:
            self.logger.error(f"Error parsing model response: {e}")
            self.logger.error("Model response might not be in the expected format.")

        self.logger.info(f"Relevant files: {relevant_files}")
        return relevant_files

    def get_signatures_from_file(self, file):
        code = read_file(file)
        results = get_key_info_from_code(code)
        self.logger.info(f"Key info from file {file}: {json.dumps(results, indent=2)}")
        return results

    def get_relevant_file_info(self, relevant_files):
        self.logger.info("Getting relevant file information.")
        relevant_info = {}
        for file in relevant_files:
            if not file.endswith(".py"):
                continue
            self.logger.info(f"Processing file: {file}")
            abs_path = os.path.join(self.local_repo_path, file)
            if not os.path.exists(abs_path):
                self.logger.warning(
                    f"File {abs_path} does not exist. try with closest match."
                )
                files = search_pyfiles_by_key_or_name(self.repo_structure, file)
                for file in files:
                    abs_path = os.path.join(self.local_repo_path, file)
                    info = self.get_signatures_from_file(abs_path)
                    if file not in relevant_info:
                        relevant_info[file] = info
            else:
                info = self.get_signatures_from_file(abs_path)
                if file not in relevant_info:
                    relevant_info[file] = info
        self.logger.info(
            f"Relevant file information: {json.dumps(relevant_info, indent=2)}"
        )
        return relevant_info

    def get_related_call_graph(self, code_info, call_paths_json):
        # read from call_paths
        call_paths = json.load(open(call_paths_json, "r"))
        method_index = {}

        for file_path, file_data in code_info.items():
            module_path = file_path.replace("/", ".").rstrip(".py")
            for cls in file_data.get("classes", []):
                cls_name = cls["name"]
                for method in cls.get("methods", []):
                    fq_name = f"{module_path}.{cls_name}.{method['name']}"
                    method["caller"] = []
                    method["callee"] = []
                    method_index[fq_name] = method

            for func in file_data.get("functions", []):
                fq_name = f"{module_path}.{func['name']}"
                func["caller"] = []
                func["callee"] = []
                method_index[fq_name] = func

        for caller, callees in call_paths.items():
            if caller in method_index:
                for callee in callees:
                    if callee in method_index:
                        method_index[caller]["callee"].append(callee)
                        method_index[callee]["caller"].append(caller)

        return code_info

    def parse_suspicious_func_response(self, model_response):
        self.logger.info("Parsing suspicious function response from model.")
        suspicious_funcs = {}
        try:
            if (
                "---BEGIN SUSPICIOUS FUNCTIONS---" in model_response
                and "---END SUSPICIOUS FUNCTIONS---" in model_response
            ):
                lines = (
                    model_response.split("---BEGIN SUSPICIOUS FUNCTIONS---")[1]
                    .split("---END SUSPICIOUS FUNCTIONS---")[0]
                    .strip()
                    .split("\n")
                )
                for line in lines:
                    file_path = line.split(":")[0].strip()
                    func = line.split(":")[-1].strip()
                    if file_path not in suspicious_funcs:
                        suspicious_funcs[file_path] = []
                    if func not in suspicious_funcs[file_path]:
                        suspicious_funcs[file_path].append(func)
        except IndexError as e:
            self.logger.error(f"Error parsing model response: {e}")
            self.logger.error("Model response might not be in the expected format.")

        self.logger.info(f"Suspicious functions: {suspicious_funcs}")
        return suspicious_funcs

    def normalize_suspicious_functions_with_ast(self, suspicious_funcs_dict):
        normalized_funcs = {}

        for file_path, raw_funcs in suspicious_funcs_dict.items():
            abs_path = os.path.join(self.local_repo_path, file_path)
            if not os.path.exists(abs_path):
                self.logger.warning(
                    f"File {abs_path} does not exist. Should try with closest match."
                )
                continue

            defined_funcs = extract_function_defs(abs_path, self.local_repo_path)
            name_to_fq = {short: fq for short, fq in defined_funcs}

            cleaned_funcs = []
            for func in raw_funcs:
                func = func.strip()
                if not func or func == "```":
                    continue

                key = func.split(".")[-1] if "." in func else func
                fq_name = name_to_fq.get(key, None)
                self.logger.info(
                    f"Processing function {func}, found fq_name: {fq_name}"
                )
                if fq_name and fq_name not in cleaned_funcs:
                    cleaned_funcs.append(fq_name)

            # search in other files if not found in this one
            if len(cleaned_funcs) == 0:
                for other_file, other_funcs in suspicious_funcs_dict.items():
                    if other_file == file_path:
                        continue
                    other_abs_path = os.path.join(self.local_repo_path, other_file)
                    if not os.path.exists(other_abs_path):
                        continue
                    other_defined = extract_function_defs(
                        other_abs_path, self.local_repo_path
                    )
                    other_name_to_fq = {short: fq for short, fq in other_defined}

                    for func in raw_funcs:
                        func = func.strip()
                        if not func or func == "```":
                            continue
                        key = func.split(".")[-1] if "." in func else func
                        fq_name = other_name_to_fq.get(key, None)
                        if fq_name and fq_name not in cleaned_funcs:
                            cleaned_funcs.append(fq_name)
                            # Move key to the other file if found
                            normalized_funcs.setdefault(other_file, []).append(fq_name)

                # Skip setting empty list for current file if handled by fallback
                if file_path not in normalized_funcs:
                    normalized_funcs[file_path] = []

            else:
                normalized_funcs[file_path] = cleaned_funcs

        return normalized_funcs

    def get_lca(self, fq_name_list, call_paths_json):
        # non-tests
        print("Getting LCAs...")
        results = {}
        all_lcas_non_tests = list(
            set(
                chain.from_iterable(
                    run_with_timeout_relatedmode(
                        300, call_paths_json, func, True, self.logger
                    )
                    for func in fq_name_list
                )
            )
        )

        self.logger.info(
            f"Lowest Common Ancestor(s) for all functions: {all_lcas_non_tests}"
        )

        for func in all_lcas_non_tests:
            if func not in results:
                results[func] = []
            lca_tests = list(
                set(
                    run_with_timeout_relatedmode(
                        300, call_paths_json, func, False, self.logger
                    )
                )
            )
            results[func] = lca_tests

        self.logger.info(f"Tests for LCAs: {json.dumps(results, indent=2)}")
        return all_lcas_non_tests, results


import difflib
from typing import List


def get_top_similar_tests(main_methods, test_methods, top_num):
    result = {}

    for main_method in main_methods:
        if not test_methods:
            return []

        similarities = []
        for test_method in test_methods:
            ratio = difflib.SequenceMatcher(None, main_method, test_method).ratio()
            similarities.append((test_method, ratio))

        similarities.sort(key=lambda x: x[1], reverse=True)

        k = max(1, top_num)
        top_tests = [method for method, _ in similarities[:k]]
        result[main_method] = top_tests
    return result


def check_coverage(close_tests, local_repo_path, base_commit):
    results = {}
    for method in close_tests:
        test_fq_names = close_tests[method]
        result_jsons = get_cov_per_testmethod(
            local_repo_path, base_commit, test_fq_names
        )
        print(result_jsons)
        results[method] = result_jsons
    return results


def check_if_covered(results):
    covered_tests = {}
    for method, test_jsons in results.items():
        if method not in covered_tests:
            covered_tests[method] = []
        short_name = method.split(".")[-1]

        for json_path in test_jsons:
            is_covered = False
            if not os.path.exists(json_path):
                print(f"[WARN] File not found: {json_path}")
                continue

            try:
                with open(json_path, "r") as f:
                    data = json.load(f)
                    covered = data["covered_functions"]
                    for func in covered:
                        if func.endswith(f".{short_name}") or func.endswith(
                            f"/{short_name}"
                        ):
                            is_covered = True
                            break

            except Exception as e:
                print(f"[ERROR] Failed reading {json_path}: {e}")

            if is_covered:
                covered_tests[method].append(json_path)
    return covered_tests


def reverse_call_graph(call_graph_json):
    with open(call_graph_json, "r", encoding="utf-8") as f:
        call_graph = json.load(f)
    reversed_graph = defaultdict(list)
    for caller, callees in call_graph.items():
        for callee in callees:
            reversed_graph[callee].append(caller)
    return reversed_graph


def extract_test_methods_in_callers(caller_paths):
    test_methods = []
    for caller_path in caller_paths:
        for caller in caller_path:
            if ".test_" in caller or caller.startswith("test_"):
                test_methods.append(caller)
    return test_methods


def get_closest_tests(paths):
    min_len = float("inf")
    closest_tests = []

    for path in paths:
        if not path:
            continue
        if ".test_" not in path[0]:
            continue

        path_len = len(path)
        if path_len < min_len:
            min_len = path_len
            closest_tests = [path[0]]
        elif path_len == min_len:
            closest_tests.append(path[0])

    return sorted(set(closest_tests))


def find_call_paths_to_targets_fast(call_graph_json, suspicious_funcs, max_depth=25):
    with open(call_graph_json, "r", encoding="utf-8") as f:
        call_graph = json.load(f)
    reversed_graph = reverse_call_graph(call_graph_json)
    results = defaultdict(list)

    for func in suspicious_funcs:
        result = {"caller_paths": [], "callee_paths": []}

        # --- Caller paths (reverse traversal)
        stack = deque()
        stack.append((func, [func]))
        visited = set()
        while stack:
            current, path = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            callers = reversed_graph.get(current, [])
            if not callers:
                result["caller_paths"].append(
                    path[::-1]
                )  # reverse to go entry -> target
            else:
                for caller in callers:
                    if len(path) < max_depth:
                        stack.append((caller, path + [caller]))

        # --- Callee paths (forward traversal)
        stack = deque()
        stack.append((func, [func]))
        visited = set()
        while stack:
            current, path = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            callees = call_graph.get(current, [])
            if not callees:
                result["callee_paths"].append(path)
            else:
                for callee in callees:
                    if len(path) < max_depth:
                        stack.append((callee, path + [callee]))

        results[func] = result

    return results


def extract_top_level_symbols(file_path: str) -> Set[str]:
    """
    Extract top-level class and function names from a file.
    """
    symbols = set()
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=file_path)
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    symbols.add(node.name)
    except Exception:
        pass
    return symbols


def find_importing_test_files_from_suspicious_funcs(repo_root, suspicious_map, logger):
    results = {}

    for suspicious_path in suspicious_map:
        suspicious_abs_path = os.path.join(repo_root, suspicious_path)
        suspicious_mod = (
            suspicious_path.replace(".py", "").replace("/", ".").strip()
        )  # e.g. astropy.table.ndarray_mixin
        suspicious_module_name = suspicious_mod.split(".")[-1]  # e.g. ndarray_mixin
        defined_symbols = extract_top_level_symbols(suspicious_abs_path)
        logger.info(f"Processing suspicious path: {suspicious_path}, {suspicious_mod}")
        logger.info(defined_symbols)
        print(f"Processing suspicious path: {suspicious_path}, {suspicious_mod}")

        logger.info(
            f"Searching for test files importing {suspicious_mod} or symbols in {suspicious_path}..."
        )

        importing_tests = []

        for root, _, files in os.walk(repo_root):
            for f in files:
                if not f.endswith(".py") or "test" not in f.lower():
                    continue

                fpath = os.path.join(root, f)
                rel_path = os.path.relpath(fpath, repo_root)

                try:

                    with open(fpath, "r", encoding="utf-8") as source:
                        tree = ast.parse(source.read(), filename=fpath)
                        # print(f"Processing file: {fpath}")
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                for alias in node.names:
                                    if (
                                        alias.name == suspicious_mod
                                        or alias.name.startswith(suspicious_mod + ".")
                                    ):
                                        importing_tests.append(fpath)
                                        print(1, fpath)
                                        print(
                                            f"Found import in {fpath}: {alias.name} matches {suspicious_mod}"
                                        )
                                    elif (
                                        alias.name in suspicious_mod
                                        and "." in alias.name
                                    ):
                                        # check two file common paths
                                        common = os.path.commonpath(
                                            [suspicious_path, rel_path]
                                        )
                                        if common:
                                            importing_tests.append(fpath)
                                            # print(2)
                                            print(
                                                f"Found import in {fpath}: {alias.name} matches {suspicious_mod} in common path {common}"
                                            )

                            elif isinstance(node, ast.ImportFrom):
                                mod = node.module

                                if not mod:
                                    continue

                                # exact match to suspicious module
                                if mod == suspicious_mod or mod.startswith(
                                    suspicious_mod + "."
                                ):
                                    importing_tests.append(fpath)
                                    print(3)
                                    print(
                                        f"Found import in {fpath}: {mod} matches {suspicious_mod}"
                                    )

                                elif suspicious_mod.startswith(mod):
                                    alias_names = [alias.name for alias in node.names]
                                    suspicious_alias = suspicious_mod.replace(
                                        mod + ".", ""
                                    )
                                    # print("Checking alias names:", alias_names, suspicious_alias)
                                    if suspicious_alias in alias_names:
                                        importing_tests.append(fpath)
                                        # print(4)
                                        print(
                                            f"Found import in {fpath}: {suspicious_alias} matches {mod}"
                                        )
                                    for alias_name in alias_names:
                                        if suspicious_alias == alias_name:
                                            importing_tests.append(fpath)
                                            # print(5)
                                            print(
                                                f"Found import in {fpath}: {alias_name} matches {suspicious_alias}"
                                            )
                                    if bool(set(defined_symbols) & set(alias_names)):
                                        importing_tests.append(fpath)
                                        print(
                                            f"Found import in {fpath}: {defined_symbols} matches {alias_names}"
                                        )
                                        # print(6)

                                # from ... import <module_name>
                                elif mod.endswith(suspicious_mod.rsplit(".", 1)[0]):
                                    for alias in node.names:
                                        if alias.name == suspicious_module_name:
                                            importing_tests.append(fpath)
                                            print(
                                                f"Found import in {fpath}: {alias.name} matches {suspicious_module_name}"
                                            )
                                            print(7)
                                # from suspicious_mod import defined_symbol
                                elif mod == suspicious_mod:
                                    for alias in node.names:
                                        if alias.name in defined_symbols:
                                            importing_tests.append(fpath)
                                            print(
                                                f"Found import in {fpath}: {alias.name} matches defined symbols in {suspicious_mod}"
                                            )
                                            print(8)

                                elif "." + mod in suspicious_mod:
                                    # print(f"***** {mod} {suspicious_mod} *****")
                                    for alias in node.names:
                                        if alias.name in defined_symbols:
                                            common = os.path.commonpath(
                                                [suspicious_path, rel_path]
                                            )
                                            if common:
                                                importing_tests.append(fpath)
                                                print(
                                                    f"Found import in {fpath}: {alias.name} matches defined symbols in {suspicious_mod}"
                                                )
                                                print(9)
                                # else:
                                #     print(fpath)
                                #     print(f"Checking if {suspicious_mod} is imported from {mod}")
                                #     print(ast.dump(node))
                                #     print("3")
                                #     print(ast.unparse(node))
                except Exception as e:
                    logger.info(f"Error processing file {fpath}: {e}")
                    continue

        results[suspicious_path] = list(set(importing_tests))

    filtered_map = select_top_similar_test_files(results, top_num=3)
    # logger.info(f"Filtered suspicious test files: {json.dumps(filtered_map, indent=2)}")

    return results, filtered_map


def select_top_similar_test_files(suspicious_map, top_num=5):
    filtered_map = {}
    for suspicious_path, test_files in suspicious_map.items():
        if not test_files:
            continue
        top_tests = get_top_similar_tests([suspicious_path], test_files, top_num)
        filtered_map[suspicious_path] = top_tests.get(suspicious_path, [])
    return filtered_map


def collect_test_methods_from_files(test_files):
    results = {}

    for fpath in test_files:
        test_methods = []
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fpath)

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    test_methods.append(f"{fpath}::{node.name}")
                elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef) and child.name.startswith(
                            "test_"
                        ):
                            test_methods.append(f"{fpath}::{node.name}.{child.name}")
        except Exception as e:
            print(f"Error processing file {fpath}: {e}")
            continue

        if test_methods:
            results[fpath] = list(set(test_methods))

    return results


def inspector_single_instance(model, instance_id, dataset_name, dataset, log_dir):
    results = {
        "instance_id": instance_id,
        "model": model,
        "dataset_name": dataset_name,
        "relevant_files": None,
        "relevant_info": None,
        "call_paths_json": None,
        "suspicious_func_results": None,
        "normalized_funcs": None,
        "lcas": None,
        "potential_tests": None,
    }

    log_file = os.path.join(log_dir, f"inspector_{instance_id}.log")
    logger = setup_logger(log_file)
    print(f"Logging to {log_file}")

    inspector_instance = Inspector(logger, instance_id, dataset_name, dataset)
    call_paths_json = inspector_instance.get_call_graph_json()

    # """
    # prompt model to get relevant file lists; input: issue description and non-test pyfiles
    relevant_file_prompt = prompts.get_relevant_file_prompt(
        inspector_instance.problem_statement, inspector_instance.non_test_pyfiles
    )
    print(f"Prompting model with the following prompt:\n{relevant_file_prompt}")
    model_response = inspector_instance.prompt_model(
        model=model, prompt=relevant_file_prompt
    )
    print(f"Model response: {model_response}")
    relevant_files = inspector_instance.parse_relevant_files(model_response)

    if not relevant_files:
        logger.error("No relevant files found in the model response. Exiting.")
        raise ("No relevant files found in the model response. Exiting.")
        cleanup_logger(logger)
        exit(0)

    relevant_info = inspector_instance.get_relevant_file_info(relevant_files)

    relevant_info = inspector_instance.get_related_call_graph(
        relevant_info, call_paths_json
    )
    suspicious_func_results = []

    def prompt_suspicious_funcs(info):
        suspicious_func_prompt = prompts.get_suspicious_info_prompt_with_files(
            inspector_instance.problem_statement, info
        )
        print(f"Prompting model with the following prompt:\n{suspicious_func_prompt}")

        model_response = inspector_instance.prompt_model(
            model=model, prompt=suspicious_func_prompt
        )
        print(f"Model response: {model_response}")
        return model_response

    model_response = prompt_suspicious_funcs(relevant_info)
    suspicious_func_results = inspector_instance.parse_suspicious_func_response(
        model_response
    )

    print(f"Suspicious functions: {json.dumps(suspicious_func_results, indent=2)}")
    normalized_funcs = inspector_instance.normalize_suspicious_functions_with_ast(
        suspicious_func_results
    )
    print(f"Normalized suspicious functions: {json.dumps(normalized_funcs, indent=2)}")
    # """
    """
    result_json = f"debugging_verified_logs7/princeton-nlp/SWE-bench_Verified/GCP_claude-3-7-sonnet/GCP_claude-3-7-sonnet_results/all_results_princeton-nlp_SWE-bench_Verified_{instance_id}.json"
    with open(result_json, 'r') as f:
        results = json.load(f)
        relevant_files = results['relevant_files']
        relevant_info = results['relevant_info']
        suspicious_func_results = results['suspicious_func_results']
        normalized_funcs = results['normalized_funcs']
    """

    print(normalized_funcs)
    """
    potential_test_files, filtered_potential_test_files = find_importing_test_files_from_suspicious_funcs(inspector_instance.local_repo_path, normalized_funcs, logger)
    print("potential_test_files\n", json.dumps(potential_test_files, indent=2))
    print("filtered_potential_test_files\n", json.dumps(filtered_potential_test_files, indent=2))
    potential_test_methods = {}
    for main_file in potential_test_files:
        if main_file not in potential_test_methods:
            potential_test_methods[main_file] = {}
        test_files = potential_test_files[main_file]
        test_methods = collect_test_methods_from_files(test_files)
        potential_test_methods[main_file] = test_methods
    

    # print(f"Potential test methods: {json.dumps(potential_test_methods, indent=2)}")

    relevant_funcs = list(chain.from_iterable(normalized_funcs.values()))
    # print(relevant_funcs)
    paths = find_call_paths_to_targets_fast(call_paths_json, relevant_funcs)
    tests = {}
    closest_tests = {}
    for main_method in paths:
        if main_method not in tests:
            tests[main_method] = []
            closest_tests[main_method] = []
        test_methods = extract_test_methods_in_callers(paths[main_method]['caller_paths'])
        tests[main_method] = list(set(test_methods))
        close_tests = get_closest_tests(paths[main_method]['caller_paths'])
        closest_tests[main_method] = list(set(close_tests))
        logger.info(f"Number of test methods extracted for {main_method}: {len(tests[main_method])}")
        logger.info(f"Closest test methods for {main_method}: {json.dumps(closest_tests, indent=2)}")
    logger.info(f"Extracted test methods from caller paths: {json.dumps(tests, indent=2)}")
    logger.info(f"Call paths for relevant functions: {json.dumps(paths, indent=2)}")
    """
    # located_tests = {}
    # ancestors_info = {}

    # all_lcas, lca_test_methods = inspector_instance.get_lca(
    #     list(chain.from_iterable(normalized_funcs.values())), call_paths_json
    # )

    # potential_tests = locate_tests(inspector_instance.local_repo_path, all_lcas, logger)
    # print(f"Potential tests found: {json.dumps(lca_test_methods, indent=2)}")

    # print(f"Adding potential tests to lca_test_methods {json.dumps(potential_tests, indent=2)}")
    # for method in potential_tests:
    #     for test in potential_tests[method]:
    #         if test not in lca_test_methods[method]:
    #             lca_test_methods[method].append(test)

    results = {
        "instance_id": instance_id,
        "dataset_name": dataset_name,
        "relevant_files": relevant_files,
        "relevant_info": relevant_info,
        "call_paths_json": call_paths_json,
        "suspicious_func_results": suspicious_func_results,
        "normalized_funcs": normalized_funcs,
        # "closest_tests": closest_tests,
        # "all_tests": tests,
        # "ancestors_info": paths,
        # "potential_test_files": potential_test_files,
        # "filtered_potential_test_files": filtered_potential_test_files,
        # "potential_test_methods": potential_test_methods,
        # "potential_relevant_tests": potential_relevant_tests,
        # 'lcas': all_lcas,
        # "num_lcas": len(all_lcas),
        # "close_tests": close_tests,
        # "summary_jsons": summary_jsons,
        # "tests_covered": check_if_covered_results,
    }

    # print(json.dumps(results, indent=2))
    # exit(0)
    cleanup_logger(logger)
    return results


def filter_paths_by_tests(ancestor_paths: dict, test_methods: list) -> dict:
    filtered = {}

    for method, paths in ancestor_paths.items():
        filtered[method] = {}
        count = 1

        for path_name, path in paths.items():
            # Check if the path starts from a known test method
            if path[0] in test_methods:
                filtered[method][f"path{count}"] = path
                count += 1

        if not filtered[method]:
            del filtered[method]  # Remove if no matching paths

    return filtered


from collections import defaultdict, deque


def get_lowest_level_test_methods(levels_dict):
    result = {}

    # Sort keys as strings representing integers in descending order
    sorted_levels = sorted(levels_dict.keys(), key=int, reverse=True)

    for level in sorted_levels:
        methods = levels_dict[level]
        test_methods = [m for m in methods if ".test_" in m or m.startswith("test_")]
        if test_methods:
            result["level"] = int(level)
            result["test_methods"] = test_methods
            return result

    return {"level": None, "test_methods": []}


def get_ancestors(method, call_paths_json_file, max_depth=None):
    # [ancestor_level_1, ancestor_level_2, ..., root]
    print("get ancestor for method:", method)

    # Load call graph from JSON file
    with open(call_paths_json_file, "r", encoding="utf-8") as f:
        call_paths_json = json.load(f)

    # Build reverse call graph
    reverse_graph = defaultdict(list)
    for caller, callees in call_paths_json.items():
        for callee in callees:
            reverse_graph[callee].append(caller)

    # BFS to find ancestors by level
    visited = set()
    ancestors_by_level = defaultdict(list)
    queue = deque([(method, 0)])
    visited.add(method)

    while queue:
        current, level = queue.popleft()
        if max_depth is not None and level >= max_depth:
            continue

        for parent in reverse_graph.get(current, []):
            if parent not in visited:
                ancestors_by_level[level + 1].append(parent)
                queue.append((parent, level + 1))
                visited.add(parent)

    return dict(ancestors_by_level)


def get_ancestor_paths(method, call_paths_json_file, max_depth=None):
    print("get ancestor paths for method:", method)

    # Load call graph from JSON file
    with open(call_paths_json_file, "r", encoding="utf-8") as f:
        call_paths_json = json.load(f)

    # Build reverse call graph
    reverse_graph = defaultdict(list)
    for caller, callees in call_paths_json.items():
        for callee in callees:
            reverse_graph[callee].append(caller)

    # DFS to find all ancestor paths
    results = {method: {}}
    paths = []

    def dfs(current, path, depth):
        if max_depth is not None and depth > max_depth:
            return
        if current not in reverse_graph or not reverse_graph[current]:
            # Root node
            paths.append(path.copy())
            return
        for parent in reverse_graph[current]:
            if parent not in path:  # avoid cycles
                path.append(parent)
                dfs(parent, path, depth + 1)
                path.pop()

    dfs(method, [method], 0)

    # Save paths
    for i, path in enumerate(paths):
        results[method][f"path{i+1}"] = path[::-1]  # reverse to show root -> method

    return results


def run_inspection_and_save(
    model,
    instance_id_local,
    dataset_name,
    dataset_name_cleaned,
    dataset,
    log_dir,
    result_dir,
):
    results = inspector_single_instance(
        model, instance_id_local, dataset_name, dataset, log_dir
    )
    if results is not None:
        json_file = f"all_results_{dataset_name_cleaned}_{instance_id_local}.json"
        dump_json(results, os.path.join(result_dir, json_file))
        print(
            f"Results for instance {instance_id_local} saved to {os.path.join(log_dir, json_file)}"
        )


def run_with_timeout(timeout_sec, func, *args, **kwargs):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            print(f"[ERROR] Timeout after {timeout_sec} seconds")
            return None


def main(model, dataset_name, dataset, log_dir, instance_id=None, max_workers=1):
    print(f"Running localization with model: {model} and dataset: {dataset_name}")
    dataset_name_cleaned = dataset_name.replace("/", "_").replace(".json", "")
    result_dir = os.path.join(log_dir, f'{model.replace("/", "_")}_results')
    # long_instances = ['django__django-11087', 'pydata__xarray-6992', 'pydata__xarray-7229', 'sympy__sympy-20428', 'pydata__xarray-2905']
    long_instances = []
    long_instances_lock = threading.Lock()

    if not os.path.exists(result_dir):
        os.makedirs(result_dir)

    def process_instance(instance):
        instance_id_local = instance["instance_id"]
        result_file = os.path.join(
            result_dir, f"all_results_{dataset_name_cleaned}_{instance_id_local}.json"
        )

        if os.path.exists(result_file):
            print(
                f"result file {result_file} already exists. Skipping instance {instance_id_local}."
            )
            return

        print(f"Inspecting instance: {instance_id_local}")

        if (
            instance_id_local in long_instances
        ):  # or "pydata__xarra" in instance_id_local:
            print(
                f"Instance {instance_id_local} is in the long instances list. Prioritizing it later."
            )
            with long_instances_lock:
                if instance_id_local not in long_instances:
                    long_instances.append(instance_id_local)
            return

        try:
            run_with_timeout(
                600,
                run_inspection_and_save,
                model,
                instance_id_local,
                dataset_name,
                dataset_name_cleaned,
                dataset,
                log_dir,
                result_dir,
            )
            # results = inspector_single_instance(model, instance_id_local, dataset_name, dataset, log_dir)
            # json_file = f"all_results_{dataset_name_cleaned}_{instance_id_local}.json"
            # dump_json(results, os.path.join(result_dir, json_file))
            # print(f"Results for instance {instance_id_local} saved to {os.path.join(log_dir, json_file)}")

        except TimeoutError as te:
            print(f"Timeout on instance {instance_id_local}: {te}")
            with long_instances_lock:
                long_instances.append(instance_id_local)
        except Exception as e:
            print(f"Error processing instance {instance_id_local}: {e}")

    if instance_id:
        print(f"Inspecting instance: {instance_id}")
        results = inspector_single_instance(
            model, instance_id, dataset_name, dataset, log_dir
        )
        json_file = f"all_results_{dataset_name_cleaned}_{instance_id}.json"
        dump_json({instance_id: results}, os.path.join(result_dir, json_file))
        print(
            f"Results for instance {instance_id} saved to {os.path.join(log_dir, json_file)}"
        )
    else:
        print("Running on all instances (multithreaded).")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(process_instance, instance) for instance in dataset
            ]
            for _ in as_completed(futures):
                pass  # wait for all threads to finish

        print(f"Long instances that timed out or skipped: {long_instances}")
