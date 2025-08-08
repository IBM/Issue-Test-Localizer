import ast
import json
import logging
import os
import sys

from datasets import load_dataset

from model_config import prompt_model
from prompts import select_test_file_prompt
from utils import parse_func_code, prepare_repo, read_json_file


def get_dataset(dataset_name):
    return load_dataset(dataset_name, split="test")


def get_instance_info(dataset_info, instance_id):
    for instance in dataset_info:
        if instance["instance_id"] == instance_id:
            info = {
                "repo": instance["repo"],
                "base_commit": instance["base_commit"],
                "problem_statement": instance["problem_statement"],
                "hints_text": instance["hints_text"],
            }
            return info
    return None


def collect_all_test_files(local_repo_path):
    test_files = []
    for root, dirs, files in os.walk(local_repo_path):
        for file in files:
            file_path = os.path.join(root, file)
            if file.endswith(".py") and "test" in file_path.lower():
                test_files.append(file_path)
    return test_files


def get_suspicious_funcs(norm_funcs, instance_info, instance_id):
    all_dict = {}
    all_test_files = []
    for file in norm_funcs:
        funcs = norm_funcs[file]
        funcs = [func for func in funcs if func != ""]
        print(f"Processing file: {file} with functions: {funcs}")
        func_dict, all_test_files = collect_repo_info(
            instance_info["repo"], instance_info["base_commit"], file, funcs
        )
        # print(func_dict)
        if func_dict:
            all_dict.update(func_dict)
        else:
            print(f"No functions found in {file} for instance {instance_id}.")

    return all_dict, all_test_files


def collect_repo_info(repo_name, base_commit, file, funcs, repo_base="temp_repos"):
    local_repo_path = prepare_repo(repo_name, base_commit, base_dir=repo_base)
    func_dict = parse_func_code(local_repo_path, file, funcs)
    all_test_files = collect_all_test_files(local_repo_path)
    all_test_files = [
        os.path.relpath(test_file, local_repo_path) for test_file in all_test_files
    ]
    return func_dict, all_test_files


def parse_response(response):
    if not response:
        return []
    if (
        "---BEGIN RELEVANT TEST FILES---" in response
        and "---END RELEVANT TEST FILES---" in response
    ):
        test_files = (
            response.split("---BEGIN RELEVANT TEST FILES---")[1]
            .split("---END RELEVANT TEST FILES---")[0]
            .strip()
        )
        return [file.strip() for file in test_files.split("\n") if file.strip()]
    else:
        return []


def main(
    suspicious_info, all_results_json, test_selection_json, confirmed_suspicious_file, dataset_name
):
    all_results, test_selection_results = {}, {}
    dataset_info = get_dataset(dataset_name)
    confirmed_suspicious_funcs = {}
    for instance_id in suspicious_info:
        print(f"Processing instance {instance_id}...")
        suspicious_funcs = suspicious_info[instance_id]
        instance_info = get_instance_info(dataset_info, instance_id)
        func_dict, all_test_files = get_suspicious_funcs(
            suspicious_funcs, instance_info, instance_id
        )
        if not func_dict:
            print("Warning: func_dict is empty or None")
            continue
        confirmed_sus = {}
        for file_path, funcs in suspicious_funcs.items():
            if file_path not in confirmed_sus:
                confirmed_sus[file_path] = []
            for func in funcs:
                if func == "":
                    continue
                print(func)
                if func not in confirmed_sus[file_path]:
                    confirmed_sus[file_path].append(func)

        for k in func_dict:
            print(f"\n--- Function: {k} ---")
            print(func_dict[k])

        original_count = sum(len(v) for v in suspicious_funcs.values())
        extracted_count = len(func_dict)

        print(f"\nOriginal suspicious functions: {original_count}")
        print(f"Extracted functions:   {extracted_count}")

        if original_count != extracted_count:
            print("Mismatch between declared and extracted functions!")
            # Flatten all suspicious function names (fully qualified)

        expected_funcs = set()
        for file_path, funcs in suspicious_funcs.items():
            for func in funcs:
                expected_funcs.add(func)

        extracted_funcs = set(func_dict.keys())
        missing_funcs = expected_funcs - extracted_funcs

        if missing_funcs:
            print("\nMissing functions:", instance_id)
            for func in sorted(missing_funcs):
                print(f"  - {func}")

        if instance_id not in confirmed_suspicious_funcs:
            confirmed_suspicious_funcs[instance_id] = confirmed_sus

        prompt = select_test_file_prompt(
            issue=f"{instance_info['problem_statement']}",
            suspicious_funcs=confirmed_sus,
            func_code_list=func_dict,
            all_test_files=all_test_files,
            top_k=10,
        )
        response = prompt_model(
            model_name="GCP/claude-3-7-sonnet", prompt=prompt, temperature=0.7
        )
        test_files = parse_response(response)
        print(json.dumps(func_dict, indent=2))
        print(len(all_test_files))
        print(prompt)
        print(f"Response from model: {response}")
        print(f"Selected test files for {instance_id}: {test_files}")
        all_results[instance_id] = {
            "selected_test_files": test_files,
            "original_suspicious_funcs": suspicious_funcs,
            "confirmed_suspicious_funcs": confirmed_sus,
            "func_code": func_dict,
            "#all_test_files": len(all_test_files),
            "prompt": prompt,
            "response": response,
        }
        test_selection_results[instance_id] = {
            "selected_test_files": test_files,
            "suspicious_funcs": confirmed_sus,
        }
        # save all_results, test_selection_results to json files
        with open(all_results_json, "w") as f:
            json.dump(all_results, f, indent=4)
        with open(test_selection_json, "w") as f:
            json.dump(test_selection_results, f, indent=4)

        # save confirmed suspicious functions to a json file

        with open(confirmed_suspicious_file, "w") as f:
            json.dump(confirmed_suspicious_funcs, f, indent=4)


if __name__ == "__main__":
    args = sys.argv[1:]
    suspicious_func_json = args[0]
    output_dir_base = args[1]
    dataset_name = args[2] #"princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench_Lite""

    suspicious_info = read_json_file(suspicious_func_json)

    output_dir = f"{output_dir_base}/model_test_files/{dataset_name.split('/')[-1]}"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # outputs:
    # all_results_json has all information; test selection json has only selected test files
    # confirmed_suspicious_file has confirmed suspicious functions
    all_results_json = f"{output_dir}/model_selected.json"
    test_selection_json = f"{output_dir}/test_file_selection.json"
    confirmed_suspicious_file = f"{output_dir}/confirmed_suspicious_funcs.json"
    

    main(
        suspicious_info,
        all_results_json,
        test_selection_json,
        confirmed_suspicious_file,
        dataset_name
    )
