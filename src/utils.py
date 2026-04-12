import ast
import json
import logging
import os
import re
import subprocess

from datasets import load_dataset

from call_graph_generation import get_callgraph, related_mode


class UseFinder(ast.NodeVisitor):
    def __init__(self, target_name):
        self.target_name = target_name
        self.found = False

    def visit_Name(self, node):
        if node.id == self.target_name:
            self.found = True
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == self.target_name:
            self.found = True
        self.generic_visit(node)


class DefFinder(ast.NodeVisitor):
    def __init__(self, target_name):
        self.target_name = target_name
        self.found = False

    def visit_FunctionDef(self, node):
        if node.name == self.target_name:
            self.found = True
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        if node.name == self.target_name:
            self.found = True
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        if node.name == self.target_name:
            self.found = True
        self.generic_visit(node)


def find_def_paths(root_dir, target_name):
    matched_files = []

    if target_name.endswith(".py"):
        return matched_files

    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            if not filename.endswith(".py") or re.search(
                "test", full_path, re.IGNORECASE
            ):
                continue
            source = read_file(full_path)
            if not source:
                continue
            try:
                tree = ast.parse(source, filename=full_path)
            except SyntaxError:
                continue

            finder = DefFinder(target_name)
            finder.visit(tree)

            if finder.found:
                matched_files.append(full_path)

    return matched_files


def read_json_file(file_path):
    if not os.path.exists(file_path):
        print(f"File {file_path} does not exist.")
        return None
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def combine_json_files(json_file1, json_file2, output_file):
    if not os.path.exists(json_file1) or not os.path.exists(json_file2):
        print(f"One of the JSON files does not exist: {json_file1}, {json_file2}")
        return

    with open(json_file1, "r", encoding="utf-8") as f1, open(
        json_file2, "r", encoding="utf-8"
    ) as f2:
        data1 = json.load(f1)
        data2 = json.load(f2)

    combined_data = {}

    for key in data1.keys():
        if key not in combined_data:
            combined_data[key] = []
        combined_data[key].extend(data1[key])

    for key in data2.keys():
        if key not in combined_data:
            combined_data[key] = []
        combined_data[key].extend(data2[key])

    for key in combined_data:
        combined_data[key] = list(set(combined_data[key]))  # Remove duplicates

    with open(output_file, "w", encoding="utf-8") as out_f:
        json.dump(combined_data, out_f, indent=4, ensure_ascii=False)

    print(f"Combined JSON data written to {output_file}")
    return output_file


def read_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, FileNotFoundError):
        return None


def search_python_files_for_string(root_dir, symbol_name, repo_name):
    matches = []
    pattern = re.compile(r"\b" + re.escape(symbol_name) + r"\b")

    for dirpath, _, filenames in os.walk(root_dir):
        for fname in filenames:
            if fname.endswith(".py"):
                full_path = os.path.join(dirpath, fname)
                if not re.search("test", full_path, re.IGNORECASE):
                    content = read_file(full_path)
                    if content and pattern.search(content):
                        full_path = full_path.replace(repo_name + "/", "")
                        matches.append(full_path)

    return matches


def check_if_def_used_in_file(file_path, target_name, logger=None):
    if not os.path.exists(file_path):
        logger and logger.warning(f"File {file_path} does not exist.")
        return False

    source = read_file(file_path)
    if not source:
        return False

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return False

    def_finder = DefFinder(target_name)
    def_finder.visit(tree)

    use_finder = UseFinder(target_name)
    use_finder.visit(tree)

    if def_finder.found or use_finder.found:
        return True
    return False


def check_substring_in_file(file_path, substring):
    file_content = read_file(file_path)
    return substring in file_content


def check_exact_string_in_file(file_path, exact_string):
    file_content = read_file(file_path)
    return re.search(r"\b" + re.escape(exact_string) + r"\b", file_content) is not None


def search_pyfiles_by_key_or_name(nested_dict, query, path=""):
    matches = []

    for key, value in nested_dict.items():
        key_matches = query in key

        if key == "*FILES":
            for filename in value:
                if not filename.endswith(".py"):
                    continue
                # ignore cases of query in filenames
                if re.search(re.escape(query), filename, re.IGNORECASE) or re.search(
                    re.escape(query), path, re.IGNORECASE
                ):
                    full_path = os.path.join(path, filename)
                    if (
                        "/tests/" not in full_path.lower()
                        and "/test/" not in full_path.lower()
                    ):
                        matches.append(full_path)
                if "/" in query:
                    if query.split("/")[-1] == filename:
                        full_path = os.path.join(path, filename)
                        if query in full_path:
                            if (
                                "/tests/" not in full_path.lower()
                                and "/test/" not in full_path.lower()
                            ):
                                matches.append(full_path)

        elif isinstance(value, dict):
            new_path = os.path.join(path, key)
            # if folder name matches query, we match all files under it
            sub_matches = search_pyfiles_by_key_or_name(value, query, new_path)
            matches.extend(sub_matches)

    return matches


def get_repo_structure(root_dir):
    structure = {}
    for dirpath, _, filenames in os.walk(root_dir):
        rel_dir = os.path.relpath(dirpath, root_dir)
        parts = rel_dir.split(os.sep) if rel_dir != "." else []
        current = structure
        for part in parts:
            current = current.setdefault(part, {})

        if filenames:
            current.setdefault("*FILES", []).extend(filenames)

    return structure


def get_dataset(dataset_name):
    dataset = load_dataset(dataset_name)
    return dataset["test"]


def prepare_repo(repo, base_commit, base_dir="temp_repos"):
    gitrepo = f"https://github.com/{repo}.git"

    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    local_repo_name = repo.split("/")[-1]
    local_repo_path = os.path.join(base_dir, local_repo_name)

    if not os.path.exists(local_repo_path):
        print(f"Cloning repository {gitrepo} into {local_repo_path}")
        clone_cmd = ["git", "clone", gitrepo, local_repo_path]
        subprocess.run(clone_cmd, check=True)
    else:
        print(f"Repository already exists at {local_repo_path}")

    print(f"Clean changes in {local_repo_path}")
    stash_cmd = ["git", "stash"]
    subprocess.run(stash_cmd, cwd=local_repo_path, check=True)

    clean_cmd = ["git", "clean", "-fd"]
    subprocess.run(clean_cmd, cwd=local_repo_path, check=True)

    print(f"Checking out commit {base_commit} in {local_repo_path}")
    checkout_cmd = ["git", "checkout", base_commit]
    subprocess.run(checkout_cmd, cwd=local_repo_path, check=True)

    return local_repo_path


def read_json_to_dict(file_path):
    if not os.path.exists(file_path):
        print(f"File {file_path} does not exist.")
        return {}
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_func_code(local_repo_path, file, func_names):
    func_code = {}
    file_path = os.path.join(local_repo_path, file)
    if not os.path.exists(file_path):
        print(f"File {file_path} does not exist.")
        return None

    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=file_path)

    func_code = {}
    class_methods = {}
    whole_classes = set()
    top_level_funcs = set()

    for name in func_names:
        parts = name.split(".")
        if len(parts) == 2:
            class_name, method_name = parts
            class_methods[(class_name, method_name)] = name
            print(class_methods)
        elif len(parts) == 1:
            if name[0].isupper():
                whole_classes.add(name)
            else:
                top_level_funcs.add(name)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in top_level_funcs:
                func_code[node.name] = ast.unparse(node).strip()

        elif isinstance(node, ast.ClassDef):
            if node.name in whole_classes:
                func_code[node.name] = ast.unparse(node).strip()
            for subnode in node.body:
                if isinstance(subnode, ast.FunctionDef):
                    key = (node.name, subnode.name)
                    if key in class_methods:
                        func_code[class_methods[key]] = ast.unparse(subnode).strip()
                    elif subnode.name in top_level_funcs:
                        func_code[subnode.name] = ast.unparse(subnode).strip()

    return func_code


def setup_logger(log_file):
    logger = logging.getLogger(log_file)
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh.setFormatter(formatter)

    logger.addHandler(fh)
    return logger


def cleanup_logger(logger):
    handlers = logger.handlers[:]
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()


def generate_call_paths_json(repo_path, output_dir):
    """Generate call graph using tree-sitter. Returns path to JSON."""
    return get_callgraph(repo_path, output_dir)


def get_lca(repo, local_repo_path, base_commit, full_func_list):
    repo_shortcut = repo.split("/")[-1]
    output_dir = f"call_paths/{repo_shortcut}_{base_commit}"
    local_repo_path = prepare_repo(repo, base_commit)
    call_paths_json = generate_call_paths_json(local_repo_path, output_dir)
    all_lcas = list(
        set(
            chain.from_iterable(
                related_mode(call_paths_json, func) for func in full_func_list
            )
        )
    )

    return all_lcas


def count_statistics(info_dict):
    # only consider non_test for now
    all_unique_files = []
    elements = []
    for key in info_dict:
        if not key.endswith(".py") and key not in elements:
            elements.append(key)
        non_test_files = info_dict[key]["non_test"]
        all_unique_files.extend(non_test_files)
    all_unique_files = list(set(all_unique_files))
    return all_unique_files, elements


def dump_json(data, file_path):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"Data dumped to {file_path}")


def get_key_info_from_code(source):

    tree = ast.parse(source)
    results = {"imports": [], "classes": [], "functions": []}

    for node in tree.body:
        if isinstance(node, ast.Import):
            results["imports"].append(ast.unparse(node).strip())
        elif isinstance(node, ast.ImportFrom):
            results["imports"].append(ast.unparse(node).strip())

        elif isinstance(node, ast.ClassDef):
            class_info = {
                "name": node.name,
                "decorators": [ast.unparse(d).strip() for d in node.decorator_list],
                "docstring": ast.get_docstring(node),
                "methods": [],
            }

            for body_item in node.body:
                if isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_info["methods"].append(process_function(body_item))
            results["classes"].append(class_info)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results["functions"].append(process_function(node))

    return results


import ast


def process_function(node):
    args = [arg.arg for arg in node.args.args]
    decorators = [ast.unparse(d).strip() for d in node.decorator_list]
    docstring = ast.get_docstring(node)
    is_async = isinstance(node, ast.AsyncFunctionDef)

    # collect all return expressions
    return_statements = []
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Return):
            try:
                code = ast.unparse(stmt.value).strip() if stmt.value else "return"
            except Exception:
                code = "<unparsable return>"
            return_statements.append(code)

    return {
        "name": node.name,
        "args": args,
        # "decorators": decorators,
        "docstring": docstring,
        # "is_async": is_async,
        "returns": return_statements,  # contains actual return expressions
    }


def extract_function_defs(file_path, local_repo_path):
    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=file_path)

    module_path = (
        file_path.replace(local_repo_path, "")
        .removeprefix(os.sep)
        .replace("/", ".")
        .removesuffix(".py")
    )
    results = []

    class StackVisitor(ast.NodeVisitor):
        def __init__(self):
            self.class_stack = []

        def visit_FunctionDef(self, node):
            if self.class_stack:
                cls = ".".join(self.class_stack)
                fqname = f"{module_path}.{cls}.{node.name}"
            else:
                fqname = f"{module_path}.{node.name}"
            results.append((node.name, fqname))
            self.generic_visit(node)

        def visit_ClassDef(self, node):
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

    StackVisitor().visit(tree)
    return results
