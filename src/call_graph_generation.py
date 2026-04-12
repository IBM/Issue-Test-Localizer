import json
import os
from collections import defaultdict

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())


def _make_parser():
    """Create a tree-sitter parser for Python."""
    return Parser(PY_LANGUAGE)

def _node_text(node):
    """Get the text content of a tree-sitter node."""
    return node.text.decode("utf-8", errors="replace")


def _get_name_from_node(node):
    if node is None:
        return None
    if node.type == "identifier":
        return _node_text(node)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        obj_name = _get_name_from_node(obj)
        attr_name = _get_name_from_node(attr)
        if obj_name and attr_name:
            return f"{obj_name}.{attr_name}"
        return attr_name
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        return _get_name_from_node(func_node)
    return None


def _find_function_name(node):
    """Get the name identifier from a function_definition node."""
    name_node = node.child_by_field_name("name")
    return _node_text(name_node) if name_node else None


def _find_class_name(node):
    """Get the name identifier from a class_definition node."""
    name_node = node.child_by_field_name("name")
    return _node_text(name_node) if name_node else None

def _collect_calls(node):
    calls = []
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        name = _get_name_from_node(func_node)
        if name:
            # Strip 'self.' prefix — these are method calls on the same class
            if name.startswith("self."):
                name = name[5:]
            calls.append(name)
    for child in node.children:
        calls.extend(_collect_calls(child))
    return calls


def _extract_file_call_graph(source_bytes, module_name):
    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    graph = defaultdict(list)

    for node in root.children:
        if node.type == "function_definition":
            func_name = _find_function_name(node)
            if func_name:
                fq_name = f"{module_name}.{func_name}"
                calls = _collect_calls(node)
                for c in calls:
                    if c not in graph[fq_name]:
                        graph[fq_name].append(c)

        elif node.type == "class_definition":
            class_name = _find_class_name(node)
            if not class_name:
                continue
            body = node.child_by_field_name("body")
            if body is None:
                continue
            for child in body.children:
                if child.type == "function_definition":
                    method_name = _find_function_name(child)
                    if method_name:
                        fq_name = f"{module_name}.{class_name}.{method_name}"
                        calls = _collect_calls(child)
                        for c in calls:
                            if c not in graph[fq_name]:
                                graph[fq_name].append(c)

        elif node.type == "decorated_definition":
            # Handle @decorator\ndef ... or @decorator\nclass ...
            for child in node.children:
                if child.type == "function_definition":
                    func_name = _find_function_name(child)
                    if func_name:
                        fq_name = f"{module_name}.{func_name}"
                        calls = _collect_calls(child)
                        for c in calls:
                            if c not in graph[fq_name]:
                                graph[fq_name].append(c)
                elif child.type == "class_definition":
                    class_name = _find_class_name(child)
                    if not class_name:
                        continue
                    body = child.child_by_field_name("body")
                    if body is None:
                        continue
                    for grandchild in body.children:
                        if grandchild.type == "function_definition":
                            method_name = _find_function_name(grandchild)
                            if method_name:
                                fq_name = f"{module_name}.{class_name}.{method_name}"
                                calls = _collect_calls(grandchild)
                                for c in calls:
                                    if c not in graph[fq_name]:
                                        graph[fq_name].append(c)
                        elif grandchild.type == "decorated_definition":
                            for gc in grandchild.children:
                                if gc.type == "function_definition":
                                    method_name = _find_function_name(gc)
                                    if method_name:
                                        fq_name = f"{module_name}.{class_name}.{method_name}"
                                        calls = _collect_calls(gc)
                                        for c in calls:
                                            if c not in graph[fq_name]:
                                                graph[fq_name].append(c)

    return dict(graph)

def _extract_imports(root_node):
    imports = []
    for node in root_node.children:
        if node.type == "import_statement":
            # import os, sys, pathlib.Path
            for child in node.children:
                if child.type == "dotted_name":
                    imports.append({
                        "module": _node_text(child),
                        "names": [_node_text(child).split(".")[-1]],
                        "type": "import",
                    })
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        imports.append({
                            "module": _node_text(name_node),
                            "names": [_node_text(name_node).split(".")[-1]],
                            "type": "import",
                        })

        elif node.type == "import_from_statement":
            # from module import name1, name2
            module_node = node.child_by_field_name("module_name")
            module_name = _node_text(module_node) if module_node else ""
            names = []
            for child in node.children:
                if child.type == "dotted_name" and child != module_node:
                    names.append(_node_text(child))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        names.append(_node_text(name_node))
            if not names:
                # from module import * or just the module itself
                names = [module_name.split(".")[-1]] if module_name else []
            imports.append({
                "module": module_name,
                "names": names,
                "type": "from",
            })

    return imports


def get_callgraph(repo_path, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    output_json = os.path.join(output_dir, "callgraph.json")

    if os.path.exists(output_json):
        return output_json

    # Collect all Python files
    py_files = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if not d.startswith('.') and d != '__pycache__'
        ]
        for f in files:
            if f.endswith('.py'):
                py_files.append(os.path.join(root, f))

    # Build combined call graph
    call_graph = {}
    for filepath in py_files:
        try:
            with open(filepath, 'rb') as f:
                source = f.read()
        except (OSError, IOError):
            continue

        rel_path = os.path.relpath(filepath, repo_path)
        module = rel_path.replace(os.sep, '.').removesuffix('.py')

        file_graph = _extract_file_call_graph(source, module)
        call_graph.update(file_graph)

    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(call_graph, f, indent=2)

    return output_json


def get_imports_from_file(filepath, repo_path=None):
    try:
        with open(filepath, 'rb') as f:
            source = f.read()
    except (OSError, IOError):
        return []

    parser = _make_parser()
    tree = parser.parse(source)
    return _extract_imports(tree.root_node)


def resolve_module_to_file(module_name, repo_path):
    """
    Resolve a dotted module name to a file path within the repo.

    Returns:
        Absolute file path if found, else None.
    """
    parts = module_name.split('.')
    # Try as package (directory with __init__.py)
    candidate = os.path.join(repo_path, *parts, '__init__.py')
    if os.path.isfile(candidate):
        return candidate
    # Try as module file
    candidate = os.path.join(repo_path, *parts) + '.py'
    if os.path.isfile(candidate):
        return candidate
    return None


import threading


def load_graph_from_json(json_path):
    """Load a call graph JSON, normalizing 'self.' references."""
    with open(json_path, "r", encoding="utf-8") as f:
        graph = json.load(f)
    cleaned = {}
    for key, values in graph.items():
        new_key = key.replace(".self.", ".")
        cleaned[new_key] = [v.replace(".self.", ".") for v in values]
    return cleaned


def _build_reverse_graph(graph):
    """Build caller <- callee reverse mapping."""
    reverse = defaultdict(set)
    for caller, callees in graph.items():
        for callee in callees:
            reverse[callee].add(caller)
    return reverse


def find_all_paths(graph, start, end, path=None, visited=None):
    """Find all paths between two functions in the call graph."""
    if path is None:
        path = []
    if visited is None:
        visited = set()
    path = path + [start]
    visited.add(start)
    if start == end:
        return [path]
    paths = []
    for node in graph.get(start, []):
        if node not in visited:
            paths.extend(find_all_paths(graph, node, end, path, visited.copy()))
    return paths


def get_all_forward_callees(call_paths_json, start):
    """Return all transitive callees of a function."""
    graph = load_graph_from_json(call_paths_json)
    visited = set()

    def _walk(node):
        if node in visited:
            return
        visited.add(node)
        for callee in graph.get(node, []):
            _walk(callee)

    _walk(start)
    visited.discard(start)
    return list(visited)


def _explore_backward_paths(reverse_graph, start, path=None, visited=None):
    """Find all backward (caller) paths from a function."""
    if path is None:
        path = []
    if visited is None:
        visited = set()
    path = path + [start]
    visited.add(start)
    paths = [path]
    for predecessor in reverse_graph.get(start, []):
        if predecessor not in visited:
            paths.extend(_explore_backward_paths(reverse_graph, predecessor, path, visited.copy()))
    return paths


def _find_lca(backward_paths):
    """Find Lowest Common Ancestor(s) of backward paths."""
    if not backward_paths:
        return []
    reversed_paths = [list(reversed(p)) for p in backward_paths]
    min_len = min(len(p) for p in reversed_paths)
    lca = []
    for i in range(min_len):
        current = reversed_paths[0][i]
        if all(p[i] == current for p in reversed_paths):
            lca.append(current)
        else:
            break
    return lca if lca else []


def related_mode(call_paths_json, target, exclude_tests=False, logger=None, _result_holder=None):
    if _result_holder is None:
        _result_holder = {}

    if logger:
        logger.info(f"Finding related paths for '{target}'...")

    if exclude_tests:
        # Filter test entries from graph
        with open(call_paths_json, "r", encoding="utf-8") as f:
            raw = json.load(f)
        cleaned = {
            k: [v for v in vs if "test_" not in v.split(".")[-1].lower() and ".tests." not in v.lower()]
            for k, vs in raw.items()
            if "test_" not in k.lower() and ".tests." not in k.lower()
        }
        graph = {k.replace(".self.", "."): [v.replace(".self.", ".") for v in vs] for k, vs in cleaned.items()}
    else:
        graph = load_graph_from_json(call_paths_json)

    reverse_graph = _build_reverse_graph(graph)
    backward_paths = _explore_backward_paths(reverse_graph, target)
    _result_holder["backward_paths"] = backward_paths

    if not backward_paths:
        _result_holder["lca"] = []
        return []

    if logger:
        logger.info(f"Found {len(backward_paths)} backward paths for '{target}'")

    lca = _find_lca(backward_paths)
    _result_holder["lca"] = lca

    if logger and lca:
        logger.info(f"LCA nodes: {lca}")

    if not exclude_tests:
        return [m for m in lca if "test" in m.lower()]
    return lca


def run_with_timeout_relatedmode(timeout_seconds, *args, **kwargs):
    """Run related_mode with a timeout (thread-based)."""
    result_holder = {}

    def target():
        try:
            related_mode(*args, _result_holder=result_holder, **kwargs)
        except Exception:
            result_holder["lca"] = []

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if "lca" in result_holder:
        if kwargs.get("exclude_tests", False):
            return result_holder["lca"]
        return [m for m in result_holder.get("lca", []) if "test" in m.lower()]
    return []
