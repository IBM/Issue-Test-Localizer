import argparse
import os
import json
import logging
from datasets import load_dataset
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict
import ast
import subprocess
import coverage
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from model_config import prompt_model


def prompt_model_to_select_test(
    tests, file, func, function_body, logger, model_name
):
    tests_lines = "\n".join(sorted(tests))
    prompt = f"""
You are an expert in test minimization. 
For function `{func}` in file `{file}`, below are the function body and the tests that cover same lines of the function.
Your task is to minimize the test suite, select top three tests: 
###Function Body###
---BEGIN FUNCTION BODY---
{function_body}
---END FUNCTION BODY---
###Tests Covering the Function###
---BEGIN TESTS---
{tests_lines}
---END TESTS---
You should follow the format below to provide your answer. Do not include any additional text or explanations.
Here is an example:
---BEGIN SELECTED TESTS---
test1
test2
...
---END SELECTED TESTS---
"""
    logger.info(f"Prompt for model {model_name}:\n{prompt}\n")
    response = prompt_model(model_name, prompt)
    logger.info(f"Response\n{response}\n")
    if response:
        if (
            "---BEGIN SELECTED TESTS---" in response
            and "---END SELECTED TESTS---" in response
        ):
            selected_tests = (
                response.split("---BEGIN SELECTED TESTS---")[1]
                .split("---END SELECTED TESTS---")[0]
                .strip()
                .split("\n")
            )
            selected_tests = [test.strip() for test in selected_tests if test.strip()]
            return selected_tests
    else:
        logger.warning(
            f"No response from model {model_name} for function {func} in file {file}."
        )

def setup_logger(instance_id, log_dir):  # ="ten_files_cov_265_0_minimization_logs"
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(instance_id)
    logger.setLevel(logging.INFO)

    # Prevent propagation to root logger (avoid duplicate messages)
    logger.propagate = False

    # avoid duplicate handlers
    if not logger.handlers:
        log_path = os.path.join(log_dir, f"{instance_id}.log")
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def read_log_file(file_path):
    with open(file_path, 'r') as file:
        content = file.read()
    return content

def process_log_content(content):
    lines = content.splitlines()
    pass_tests = []
    for line in lines:
        line = line.strip()
        if "PASSED" in line:
            test = line.split(' ')[0].strip().split("[")[0]
            pass_tests.append(test)
    pass_tests = list(set(pass_tests))
    return {"PASS": pass_tests}

def process_django_log(content):
    content = content.split("+ coverage run ./tests/runtests.py")[-1].split('+ coverage report -m')[0]
    lines = content.splitlines()
    pass_tests = []
    for line in lines:
        line = line.strip()
        if '... ok' in line and "(" in line and "test" in line:
            test_method = line.split(' ')[0].strip()
            test_class = line.split('(')[-1].split(')')[0].strip()
            # check if the last part with upper initial, then it is a class
            parts = test_class.split('.')
            if len(parts) > 1 and parts[-1][0].isupper():
                test_file = '/'.join(parts[:-1]) + ".py"
                test_class = parts[-1]
                pass_tests.append(f"{test_file}::{test_class}::{test_method}")
            else:
                test_file = '/'.join(parts) + ".py"
                pass_tests.append(f"{test_file}::{test_method}")
    pass_tests = list(set(pass_tests))
    # print(len(pass_tests))
    return {"PASS": pass_tests}

def parse_test_results(log_file):
    test_res = {}
    content = read_log_file(log_file)
    if 'django' in log_file:
        test_res = process_django_log(content)
        if len(test_res['PASS']) == 0:
            print(f"No passed tests found in {log_file}")
    else:
        test_res = process_log_content(content)
        if len(test_res['PASS']) == 0:
            print(f"No passed tests found in {log_file}")
    return test_res

def collect_passing_tests(content):
    passing_tests = []
    return passing_tests

def get_log_file(test_log_dir):
    log_files = {}
    for dirpath, _, files in os.walk(test_log_dir):
        for filename in files:
            if filename.endswith(".txt"):
                prev_dir = os.path.basename(os.path.dirname(os.path.join(dirpath, filename)))
                instance_id = prev_dir
                log_files[instance_id] = os.path.join(dirpath, filename)
    return log_files


def get_base_commit(instance_id, dataset):
    for item in dataset:
        if item['instance_id'] == instance_id:
            return item['base_commit'], item['repo']
        
def _names_from_target(t):
    if isinstance(t, ast.Name):
        yield t.id
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _names_from_target(e)

def get_func_lines_code(repo, base_commit, file, fq_funcs, repo_base_dir = "temp_repos"):
    repo_dir = os.path.join(repo_base_dir, repo.split('/')[-1])

    if not os.path.exists(repo_dir):
        print(f"Cloning repository {repo}...")
        subprocess.run([
            "git", "clone", f"https://github.com/{repo}.git", repo_dir
        ], check=True)
    else:
        print(f"Repository {repo} already exists.")

    subprocess.run(["git", "fetch"], cwd=repo_dir, check=True)
    subprocess.run(["git", "checkout", base_commit], cwd=repo_dir, check=True)

    file_path = os.path.join(repo_dir, file)
    if not os.path.exists(file_path):
        print(f"Error: File {file} not found at commit {base_commit}.")
        return set(),repo_dir

    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)

    ##
    # Convert file path to module prefix for stripping FQ names
    # e.g. 'django/core/management/commands/runserver.py' -> 'django.core.management.commands.runserver'
    module_prefix = file.replace("/", ".").removesuffix(".py")

    res = {}
    for func in fq_funcs:
        # Strip module prefix from FQ name to get short name
        # 'django.core.management.commands.runserver.Command.inner_run' -> 'Command.inner_run'
        # 'django.core.servers.basehttp.run' -> 'run'
        short_func = func
        if func.startswith(module_prefix + "."):
            short_func = func[len(module_prefix) + 1:]

        parts = short_func.split(".")

        func_lines = set()
        class_methods = {}
        whole_classes = set()
        top_level_funcs = set()
        if len(parts) == 2:
            class_name, method_name = parts
            class_methods[(class_name, method_name)] = func
            if class_name not in whole_classes:
                whole_classes.add(class_name)
            if method_name not in top_level_funcs:
                top_level_funcs.add(method_name)
        elif len(parts) == 1:
            first_letter = next((c for c in short_func if c.isalpha()), "")
            if first_letter.isupper():
                whole_classes.add(short_func)
            else:
                top_level_funcs.add(short_func)

        for node in tree.body:
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                print("Found function:", node.name)
                if node.name in top_level_funcs:
                    start_lineno = node.lineno
                    end_lineno = getattr(node, "end_lineno", node.body[-1].lineno)
                    func_lines = set(range(start_lineno, end_lineno + 1))
                    print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                    res[func] = (func_lines,ast.unparse(node))
                    break
                for subnode in node.body:
                    if isinstance(subnode, ast.FunctionDef) or isinstance(subnode, ast.AsyncFunctionDef):
                        print("Found function:", subnode.name)
                        key = (node.name, subnode.name)
                        if key in class_methods:
                            start_lineno = subnode.lineno
                            end_lineno = getattr(subnode, "end_lineno", subnode.body[-1].lineno)
                            func_lines = set(range(start_lineno, end_lineno + 1))
                            print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                            res[func] = (func_lines,ast.unparse(node))
                            break
                        if subnode.name in top_level_funcs:
                            start_lineno = subnode.lineno
                            end_lineno = getattr(subnode, "end_lineno", subnode.body[-1].lineno)
                            func_lines = set(range(start_lineno, end_lineno + 1))
                            print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                            res[func] = (func_lines,ast.unparse(node))
                            break
            elif isinstance(node, ast.ClassDef):
                if node.name in whole_classes:
                    start_lineno = node.lineno
                    end_lineno = getattr(node, "end_lineno", node.body[-1].lineno)
                    func_lines = set(range(start_lineno, end_lineno + 1))
                    print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                    res[func] = (func_lines,ast.unparse(node))
                    break

                for subnode in node.body:
                    if isinstance(subnode, ast.FunctionDef) or isinstance(subnode, ast.AsyncFunctionDef):
                        key = (node.name, subnode.name)
                        if key in class_methods:
                            start_lineno = subnode.lineno
                            end_lineno = getattr(subnode, "end_lineno", subnode.body[-1].lineno)
                            func_lines = set(range(start_lineno, end_lineno + 1))
                            print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                            res[func] = (func_lines,ast.unparse(node))
                            break
                        if subnode.name in top_level_funcs:
                            start_lineno = subnode.lineno
                            end_lineno = getattr(subnode, "end_lineno", subnode.body[-1].lineno)
                            func_lines = set(range(start_lineno, end_lineno + 1))
                            print(f"Found {func} at lines {start_lineno}-{end_lineno}")
                            res[func] = (func_lines,ast.unparse(node))
                            break
    ##
        if not func_lines:
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    targets = [n for t in node.targets for n in _names_from_target(t)]
                    for name in targets:
                        if name in top_level_funcs or name in whole_classes:
                            start = node.lineno
                            end = getattr(node, "end_lineno", start)
                            func_lines = set(range(start, end + 1))
                            print(f"Found {name} (assign) at lines {start}-{end}")
                            res[name] = (func_lines, ast.unparse(node))
                            break
                elif isinstance(node, ast.AnnAssign):
                    targets = list(_names_from_target(node.target))
                    for name in targets:
                        if name in top_level_funcs or name in whole_classes:
                            start = node.lineno
                            end = getattr(node, "end_lineno", start)
                            func_lines = set(range(start, end + 1))
                            print(f"Found {name} (annassign) at lines {start}-{end}")
                            res[name] = (func_lines, ast.unparse(node))
                            break

                elif isinstance(node, ast.AugAssign):
                    targets = list(_names_from_target(node.target))
                    for name in targets:
                        if name in top_level_funcs or name in whole_classes:
                            start = node.lineno
                            end = getattr(node, "end_lineno", start)
                            func_lines = set(range(start, end + 1))
                            print(f"Found {name} (augassign) at lines {start}-{end}")
                            res[name] = (func_lines, ast.unparse(node))
                            break
        if not func_lines:
            print(f"Function {func} not found in {file} at commit {base_commit}.")

    return res, repo_dir


from bisect import bisect_left
from query_cov import query_coverage_data as _raw_query_cov


def query_coverage_data(
    instance_id, modified_file, modified_lines, cov_dir_base, logger
):
    """
    Query coverage data for a specific file and set of line numbers.

    Uses the .coverage file generated by generate_cov_batch.py (Step 3).
    Expected path: {cov_dir_base}/{instance_id}/{instance_id}_coverage

    The .coverage file contains dynamic contexts (test function names) per line,
    generated via `.coveragerc` with `dynamic_context = test_function`.

    For lines with no direct test coverage, falls back to the nearest
    previously-covered line's context.

    Args:
        instance_id: SWE-bench instance ID.
        modified_file: Relative path to the suspicious file (e.g., "django/db/models/fields/__init__.py").
        modified_lines: Set of line numbers in the suspicious function.
        cov_dir_base: Base directory containing per-instance coverage subdirs.
        logger: Logger instance.

    Returns:
        Dict mapping line numbers to lists of test context strings,
        or None if no coverage data exists.
    """
    cov_dir = os.path.join(cov_dir_base, instance_id)
    if not os.path.exists(cov_dir):
        logger.info(
            f"Coverage directory does not exist for instance {instance_id}: {cov_dir}"
        )
        return None

    coverage_file = os.path.join(cov_dir, f"{instance_id}_coverage")
    if not os.path.exists(coverage_file):
        logger.info(f"Coverage file does not exist: {coverage_file}")
        return None

    all_contexts = {}
    cov = coverage.Coverage(data_file=coverage_file)
    cov.load()
    data = cov.get_data()
    files = data.measured_files()
    if len(files) == 0:
        logger.info(f"No coverage data found for instance {instance_id}")
        return None

    for file in files:
        # Coverage files store absolute paths from inside the container (/testbed/...)
        format_file = file.replace("/testbed/", "")
        if modified_file == format_file:
            lineno_contexts = data.contexts_by_lineno(file)
            nonempty_lines = sorted(ln for ln, ctx in lineno_contexts.items() if ctx != [''])
            logger.info(f"Found context for file {file}:\n{lineno_contexts}")
            for lineno, contexts in lineno_contexts.items():
                if lineno in modified_lines:
                    if lineno not in all_contexts:
                        all_contexts[lineno] = []
                    contexts = [c for c in contexts if c != ""]
                    all_contexts[lineno].extend(contexts)
                    if len(contexts) == 0:
                        logger.info(f"Not found context for line {lineno}, retrieve to tests covered the nearest lines")
                        # Fallback: retrieve tests from nearest covered line
                        i = bisect_left(nonempty_lines, lineno) - 1
                        if i >= 0:
                            prev_ln = nonempty_lines[i]
                            fallback = lineno_contexts.get(prev_ln, [])
                            if fallback:
                                logger.info(
                                    f"No direct context for line {lineno}; "
                                    f"using {len(fallback)} context(s) from previous covered line {prev_ln}."
                                )
                                if prev_ln not in all_contexts:
                                    all_contexts[prev_ln] = []
                                all_contexts[prev_ln].extend(fallback)
                            else:
                                logger.info(f"No direct context for {lineno} and previous line {prev_ln} empty.")

    logger.info(f"Line coverage contexts for instance {instance_id}:\n{json.dumps(all_contexts, indent=2)}")
    return all_contexts


def _build_test_to_lines(line_to_tests, filtered_tests, logger):
    """Invert line->tests map to test->set of lines, filtering to passed tests."""
    test_to_lines = defaultdict(set)
    for line_key, tests in line_to_tests.items():
        for test in tests:
            if test not in filtered_tests:
                continue
            if test:
                test_to_lines[test].add(line_key)

    logger.info("Inverted test coverage map (test -> lines):")
    s = {k: sorted(list(v)) for k, v in test_to_lines.items()}
    for k in s:
        logger.info(f"{k}:\n {s[k]}")
    return test_to_lines


def _handle_ties(candidates, file, func, function_body, logger, model_name):
    """Handle tie-breaking: if <= 3 candidates, keep all; else use LLM."""
    if len(candidates) <= 3:
        return candidates
    try:
        return prompt_model_to_select_test(
            candidates, file, func, function_body.keys(), logger, model_name
        )
    except Exception as e:
        logger.error(f"Error occurred while prompting model: {e}")
        logger.error(f"Retrying without function body due to token limits")
        import time
        time.sleep(40)
        return prompt_model_to_select_test(
            candidates, file, func, {}, logger, model_name
        )


def minimize_tests_greedy_additional(line_to_tests, file, func, function_body, logger, filtered_tests, model_name):
    """
    Greedy-Additional algorithm (Algorithm 1 in the paper).
    Each iteration re-ranks tests by coverage of *remaining uncovered* lines.
    """
    test_to_lines = _build_test_to_lines(line_to_tests, filtered_tests, logger)
    uncovered_lines = set(line_to_tests.keys())
    selected_tests = []

    while uncovered_lines:
        test_coverage = []
        max_coverage_size = 0

        for test, lines in test_to_lines.items():
            covered = lines & uncovered_lines
            size = len(covered)
            if size > 0:
                if size > max_coverage_size:
                    max_coverage_size = size
                    test_coverage = [(test, covered)]
                elif size == max_coverage_size:
                    test_coverage.append((test, covered))

        if not test_coverage:
            logger.warning(
                f"No more tests cover the remaining {len(uncovered_lines)} lines"
            )
            break

        logger.info(
            f"Test coverage found {len(test_coverage)} tests covering {len(uncovered_lines)} uncovered lines"
        )
        if len(test_coverage) > 1:
            candidates = [test for test, _ in test_coverage]
            chosen_test = _handle_ties(candidates, file, func, function_body, logger, model_name)
            selected_tests.extend(chosen_test)
            for test in chosen_test:
                uncovered_lines -= test_to_lines[test]
        else:
            test, covered = test_coverage[0]
            selected_tests.append(test)
            uncovered_lines -= covered

    return list(set(selected_tests))


def minimize_tests_greedy_total(line_to_tests, file, func, function_body, logger, filtered_tests, model_name):
    """
    Greedy-Total algorithm (Algorithm 2 in the paper).
    Tests are ranked by *total* coverage across all suspicious lines (fixed ordering).
    Stops after 3 consecutive non-improving iterations.
    """
    test_to_lines = _build_test_to_lines(line_to_tests, filtered_tests, logger)
    all_lines = set(line_to_tests.keys())

    # Compute total coverage per test (over ALL lines, not just uncovered)
    test_total_cov = {}
    for test, lines in test_to_lines.items():
        test_total_cov[test] = len(lines & all_lines)

    # Sort tests by total coverage descending
    sorted_tests = sorted(test_total_cov.keys(), key=lambda t: -test_total_cov[t])

    selected_tests = []
    cumulative_covered = set()
    no_improve = 0
    remaining = list(sorted_tests)

    while remaining and no_improve < 3:
        # Find tests with max total coverage among remaining
        max_cov = test_total_cov[remaining[0]]
        top_tests = [t for t in remaining if test_total_cov[t] == max_cov]

        prev_size = len(cumulative_covered)

        if len(top_tests) > 1:
            candidates = top_tests
            chosen = _handle_ties(candidates, file, func, function_body, logger, model_name)
        else:
            chosen = [top_tests[0]]

        for test in chosen:
            cumulative_covered |= test_to_lines[test]
            if test in remaining:
                remaining.remove(test)

        new_size = len(cumulative_covered)
        if new_size > prev_size:
            no_improve = 0
            selected_tests.extend(chosen)
            logger.info(f"Coverage improved: {prev_size} -> {new_size} lines")
        else:
            no_improve += 1
            selected_tests.extend(chosen)
            logger.info(f"No improvement ({no_improve}/3), coverage stays at {new_size}")

    return list(set(selected_tests))


def minimize_tests(line_to_tests, file, func, function_body, logger, filtered_tests, model_name, strategy="greedy_additional"):
    """
    Dispatch to the appropriate minimization strategy.

    Args:
        strategy: 'greedy_additional' (Algorithm 1) or 'greedy_total' (Algorithm 2).
    """
    if strategy == "greedy_total":
        return minimize_tests_greedy_total(
            line_to_tests, file, func, function_body, logger, filtered_tests, model_name
        )
    else:
        return minimize_tests_greedy_additional(
            line_to_tests, file, func, function_body, logger, filtered_tests, model_name
        )

def process_instance(
    instance_id, passed_tests_fq, suspicious_info, cov_dir, log_dir, dataset, model_name, result_json
):
    logger = setup_logger(instance_id, log_dir)
    
    logger.info(f"{passed_tests_fq}")
    passed_tests = [test.replace(".py::", "::").replace("/", ".").replace("::", ".") for test in passed_tests_fq]
    logger.info(f"Processed passed tests: {passed_tests}")
    
    
    if len(passed_tests) <= 5 and len(passed_tests) > 0:
        logger.info(
            f"Instance {instance_id} has {len(passed_tests)} tests, skip minimization!"
        )
        return passed_tests

    if instance_id not in suspicious_info:
        logger.warning(
            f"Instance {instance_id} not found in suspicious info, skipping..."
        )
        return []

    normalized_funcs = suspicious_info[instance_id]
    base_commit, repo_name = get_base_commit(instance_id, dataset)
    all_selected_tests = []

    logger.info(f"*************** Processing instance {instance_id} ***************")
    logger.info(passed_tests)

    combined_line_to_tests = defaultdict(list)
    function_info = {}
    for file in normalized_funcs:
        logger.info(f"Processing file: {file} to find {normalized_funcs[file]} ")
        linenos, local_repo_path = get_func_lines_code(
            repo_name, base_commit, file, normalized_funcs[file]
        )
        for func in linenos:
            lines, func_code = linenos[func]
            result = query_coverage_data(
                instance_id, file, lines, cov_dir, logger
            )
            
            if result == None:
                logger.warning(f"No coverage data found for {file}:{lines}")
                continue

            for line, tests_list in result.items():
                key = (file, line)
                combined_line_to_tests[key].extend(t for t in tests_list if t != "")

            function_info[(file, func)] = func_code

    # Log before minimization
    all_tests = set(t for testlist in combined_line_to_tests.values() for t in testlist)
    logger.info(f"\n#Before Minimization: {len(all_tests)} unique tests")
    passed_all_tests = set(t for t in all_tests if t in passed_tests)
    if len(passed_all_tests) > 0:
        tests = list(passed_all_tests)
        logger.info(f"Passed tests before minimization: {len(tests)}")
    else:
        tests = list(all_tests)
        logger.info(f"Passed tests = 0 , will use original {len(tests)} tests")

    # Perform minimization on combined coverage
    file_str = ", ".join(sorted(set(f for f, _ in function_info)))
    func_str = ", ".join(sorted(set(f for _, f in function_info)))
    selected_tests = minimize_tests(
        line_to_tests=combined_line_to_tests,
        file=file_str,
        func=func_str,
        function_body=function_info,
        logger=logger,
        filtered_tests=tests,
        model_name=model_name
    )

    logger.info(f"#After Minimization: {len(selected_tests)} tests selected\n")
    logger.info("Selected tests:")
    for test in selected_tests:
        logger.info(f"  {test}")

    # Collect into master test list if needed
    all_selected_tests.extend(selected_tests)
    logger.info(f"*************** END instance {instance_id} ***************\n")

    return all_selected_tests

def main(coverage_dir, test_log_dir, suspicious_info, dataset, log_dir, model_name, result_json, prev_results, rerun_instances=None, strategy="greedy_additional"):
    test_logs = get_log_file(test_log_dir)

    for instance_id, log_file in test_logs.items():
        # Skip already-processed instances unless in rerun list
        if instance_id in prev_results:
            if rerun_instances and instance_id not in rerun_instances:
                continue
            elif not rerun_instances:
                continue

        # If rerun list is specified, only process those instances
        if rerun_instances and instance_id not in rerun_instances:
            continue

        print(f"Processing {log_file} for instance {instance_id}")
        test_results = parse_test_results(log_file)
        passed_tests = test_results['PASS']
        all_selected_tests = process_instance(instance_id, passed_tests, suspicious_info, coverage_dir, log_dir, dataset, model_name, result_json)
        prev_results[instance_id] = all_selected_tests

        with open(result_json, "w") as file:
            json.dump(prev_results, file, indent=4)

from utils import read_json_file


def parse_args():
    parser = argparse.ArgumentParser(description="Process coverage and test log directories.")
    parser.add_argument(
        "--coverage_dir",
        required=True,
        help="Path to the coverage directory"
    )
    parser.add_argument(
        "--test_log_dir",
        required=True,
        help="Path to the test log directory"
    )
    parser.add_argument(
        "--suspicious_json",
        required=True,
        help="Path to the suspicious JSON file"
    )
    parser.add_argument(
        "--datasetname",
        required=True,
        choices=["princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench_Lite"],
        help="Name of the dataset"
    )
    parser.add_argument(
        "--log_dir",
        required=True,
        help="Path to the log directory"
    )
    parser.add_argument(
        "--model_name",
        required=True,
        help="Name of the model to use"
    )
    parser.add_argument(
        "--strategy",
        default="greedy_additional",
        choices=["greedy_additional", "greedy_total"],
        help="Minimization strategy: greedy_additional (Algorithm 1) or greedy_total (Algorithm 2)"
    )
    parser.add_argument(
        "--rerun_instances",
        nargs="*",
        default=None,
        help="Optional list of instance IDs to rerun (overrides skip logic)"
    )

    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    coverage_dir = args.coverage_dir
    test_log_dir = args.test_log_dir
    suspicious_json = args.suspicious_json
    datasetname = args.datasetname
    log_dir = args.log_dir
    model_name = args.model_name
    strategy = args.strategy
    rerun_instances = args.rerun_instances

    suspicious_info = read_json_file(suspicious_json)
    # Save minimized_tests.json and per-instance logs alongside coverage data
    result_json = os.path.join(coverage_dir, "minimized_tests.json")
    log_dir = os.path.join(coverage_dir, "minimization_logs")
    os.makedirs(log_dir, exist_ok=True)

    prev_results = {}
    if os.path.exists(result_json):
        with open(result_json, "r") as file:
            prev_results = json.load(file)

    print(f"Coverage dir: {coverage_dir}")
    print(f"Test log dir: {test_log_dir}")
    print(f"Suspicious JSON: {suspicious_json}")
    print(f"Dataset name: {datasetname}")
    print(f"Strategy: {strategy}")

    dataset = load_dataset(datasetname)
    dataset = dataset['test']

    main(coverage_dir, test_log_dir, suspicious_info, dataset, log_dir, model_name, result_json, prev_results, rerun_instances, strategy)