def format_func_list(suspicious_funcs, func_code_list):
    formatted_funcs = ""
    for file in suspicious_funcs:
        funcs = suspicious_funcs[file]
        for func in funcs:
            # rel_func = func.split('.')[-1]
            rel_func = func
            if rel_func in func_code_list:
                func_code = func_code_list[rel_func]
                formatted_funcs += f"File: {file}, Function: {func}\n{func_code}\n\n"
    return formatted_funcs


def select_test_file_prompt(
    issue, suspicious_funcs, func_code_list, all_test_files, top_k=10
):
    formatted_funcs = format_func_list(suspicious_funcs, func_code_list)
    formatted_test_files = "\n".join(all_test_files)
    prompt = """You are an expert in code localization and debugging.
Given a GitHub issue, a function, and a list of potential test files, your task is to identify the top {top_k} most relevant test files that test the function.
###GitHub Issue Description###
---BEGIN GITHUB ISSUE DESCRIPTION---
{issue}
---END GITHUB ISSUE DESCRIPTION---
### Suspicious Functions and Their Code Snippets###
---BEGIN SUSPICIOUS FUNCTIONS AND CODE SNIPPETS---
{formatted_funcs}
---END SUSPICIOUS FUNCTIONS AND CODE SNIPPETS---
###Potential Test Files###
---BEGIN POTENTIAL TEST FILES---
{formatted_test_files}
---END POTENTIAL TEST FILES---
You should follow the format below to provide your answer. Do not include any additional text or explanations, just the list of relevant test files.
Here is an example:
---BEGIN RELEVANT TEST FILES---
test_file1.py
test_file2.py
...
---END RELEVANT TEST FILES---
"""
    return prompt.format(
        issue=issue,
        formatted_funcs=formatted_funcs,
        formatted_test_files=formatted_test_files,
        top_k=top_k,
    )


def select_test_method_prompt(main_method, test_file, test_methods, top_num):
    prompt = """You are an expert in code localization and debugging. 
Given a main method, a test file, and a list of potential test methods, you should identify the top {top_num} most relevant test methods that test the main method.
###Main Method###
---BEGIN MAIN METHOD---
{main_method}
---END MAIN METHOD---
###Test File###
---BEGIN TEST FILE---
{test_file}
---END TEST FILE---
###Potential Test Methods###
---BEGIN POTENTIAL TEST METHODS---
{test_methods}
---END POTENTIAL TEST METHODS---
You should follow the format below to provide your answer. Do not include any additional text or explanations, just the list of relevant test methods of their fully qualified names.
Here is an example:
---BEGIN RELEVANT TEST METHODS---
testclass1.test_method1
testclass2.test_method2
---END RELEVANT TEST METHODS---
"""
    return prompt.format(
        main_method=main_method,
        test_file=test_file,
        test_methods=test_methods,
        top_num=top_num,
    )


def trim_down_tests_prompt(issue, all_tests, top_k=5):
    prompt = """You are an expert in code localization and debugging. 
Given a GitHub issue, and a list of suspicious methods and the potential tests,
each suspicious method is the key of the dictionary, and the value is a list of potential tests,
your task is to identify top 5 the most relevant tests to the issue.
###GitHub Issue Description###
---BEGIN GITHUB ISSUE DESCRIPTION---
{issue}
---END GITHUB ISSUE DESCRIPTION---

###Potential Tests###
---BEGIN POTENTIAL TESTS---
{all_tests}
---END POTENTIAL TESTS---

You should follow the format below to provide your answer. Do not include any additional text or explanations, just the list of relevant test files.
Here is an example:
```---BEGIN RELEVANT TESTS---
test1
test2
...---END RELEVANT TESTS---
```
"""
    return prompt.format(issue=issue, all_tests=all_tests, top_k=top_k)


def get_suspicious_info_prompt(problem_statement):
    suspicious_info_prompt = """
You are an expert in code localization and debugging. 
Your task is to extract all the suspicious files, classes, attributes, or methods if any of them are mentioned in the problem statement.

###GitHub Repository Issue Description###
---BEGIN PROBLEM STATEMENT---
{problem_statement}
---END PROBLEM STATEMENT---

Here is an example:
```
---BEGIN SUSPICIOUS FILES---
file1.py
file2.py
...
---END SUSPICIOUS FILES---

---BEGIN SUSPICIOUS CLASSES---
ClassName1
ClassName2.AttributeName
...
---END SUSPICIOUS CLASSES---

---BEGIN SUSPICIOUS METHODS---
MethodName1
MethodName2
...
---END SUSPICIOUS METHODS---
```
"""
    return suspicious_info_prompt.format(problem_statement=problem_statement)


def get_relevant_file_prompt(problem_statement, non_test_pyfiles):
    relevant_file_prompt = """
You are an expert in code localization and debugging. 
Given a problem statement, your task is to identify the most relevant TOP 5 files from the provided dict of non-test Python files, 
which is a nested dictionary where the keys are directory names and the values are lists of file names, starting with the key name "*FILES".
You should put the TOP 5 relevant files in order of relevance.

###GitHub Repository Issue Description###
---BEGIN PROBLEM STATEMENT---
{problem_statement}
---END PROBLEM STATEMENT---

###Non-test Python Files###
---BEGIN NON-TEST PYTHON FILES---
{non_test_pyfiles}
---END NON-TEST PYTHON FILES---

You should follow the format below to provide your answer. Do not include any additional text or explanations, just the list of relevant files.
Here is an example:
```
---BEGIN RELEVANT FILES---
directory1/file1.py
directory1/file2.py
directory2/directory3/file3.py
...
---END RELEVANT FILES---
```
"""

    return relevant_file_prompt.format(
        problem_statement=problem_statement, non_test_pyfiles=non_test_pyfiles
    )


def extract_method_signatures(class_info):
    method_signatures = []
    for method in class_info.get("methods", []):
        method_name = method["name"]
        args = method.get("args", [])
        signature = f"def {method_name}({', '.join(args)})\n"
        if len(method["caller"]) > 0:
            signature += f" (called by: {', '.join(method['caller'])})\n"
        if len(method["callee"]) > 0:
            signature += f" (calls: {', '.join(method['callee'])})\n"
        # signature += "---------------------------\n"
        method_signatures.append(signature)
    return method_signatures


def format_relavant_info(relevant_info):
    # print(json.dumps(relevant_info, indent=2))
    formatted = ""
    for file in relevant_info:
        formatted += f"File:{file}\n"
        # imports = '\n'.join(relevant_info[file]['imports'])
        # formatted += imports
        for function_info in relevant_info[file]["functions"]:
            name = function_info["name"]
            args = function_info.get("args", [])
            signature = f"def {name}({', '.join(args)})"
            formatted += f"{signature}\n"

            # if len(function_info['caller']) > 0:
            #     formatted += f" (called by: {', '.join(function_info['caller'])})\n"
            # if len(function_info['callee']) > 0:
            #     formatted += f" (calls: {', '.join(function_info['callee'])})\n"

            # formatted += "---------------------------\n"
        for class_info in relevant_info[file]["classes"]:
            method_signatures = extract_method_signatures(class_info)
            formatted += "==========================\n"
            formatted += f"\nClass: {class_info['name']}\n"
            methods_str = "\n ".join(method_signatures)
            formatted += f"\nMethods:\n {methods_str}"

    return formatted


import json


def get_suspicious_info_prompt_with_files(problem_statement, relevant_info):
    formatted = format_relavant_info(relevant_info)
    prompt = """
You are an expert in code localization and debugging.
Given a GitHub repository issue description and a list of relevant files, with all the function signatures, and their call graphs, 
your task is to identify those functions related to the issue, sorted by their relevance.

###GitHub Repository Issue Description###
---BEGIN PROBLEM STATEMENT---
{problem_statement}
---END PROBLEM STATEMENT---
###Relevant Information###
---BEGIN RELEVANT INFORMATION---
{printed_relevant_info}
---END RELEVANT INFORMATION---

You should follow the format below to provide your answer. Do not include any additional text or explanations, just the list of suspicious functions.
Here is an example:
```
---BEGIN SUSPICIOUS FUNCTIONS---
filepath1: class1.function1
filepath2: class2.function2
...
---END SUSPICIOUS FUNCTIONS---
```
    """
    return prompt.format(
        problem_statement=problem_statement, printed_relevant_info=formatted
    )
