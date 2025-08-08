import argparse
import json
import logging
import os
from pathlib import Path

from datasets import load_dataset
from model_config import prompt_model
from prompts import select_test_file_prompt
from utils import parse_func_code, prepare_repo, read_json_file

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


def get_dataset(dataset_name):
    return load_dataset(dataset_name, split="test")


def get_instance_info(dataset_info, instance_id):
    return next(
        (
            {
                "repo": inst["repo"],
                "base_commit": inst["base_commit"],
                "problem_statement": inst["problem_statement"],
                "hints_text": inst["hints_text"],
            }
            for inst in dataset_info
            if inst["instance_id"] == instance_id
        ),
        None
    )


def collect_all_test_files(local_repo_path):
    """Return all Python test files in repo."""
    return [
        str(Path(root) / file)
        for root, _, files in os.walk(local_repo_path)
        for file in files
        if file.endswith(".py") and "test" in file.lower()
    ]


def collect_repo_info(repo_name, base_commit, file, funcs, repo_base="temp_repos"):
    """Clone/checkout repo, parse suspicious function code, and list test files."""
    if not os.path.exists(repo_base):
        os.makedirs(repo_base)
    local_repo_path = prepare_repo(repo_name, base_commit, base_dir=repo_base)
    func_dict = parse_func_code(local_repo_path, file, funcs)
    all_test_files = [
        os.path.relpath(test_file, local_repo_path)
        for test_file in collect_all_test_files(local_repo_path)
    ]
    return func_dict, all_test_files


def get_suspicious_funcs(norm_funcs, instance_info, instance_id):
    """Extract suspicious function code for all files in one instance."""
    all_dict = {}
    all_test_files = []
    for file, funcs in norm_funcs.items():
        funcs = [f for f in funcs if f]
        logging.info(f"  Processing file: {file} with {len(funcs)} functions")
        func_dict, all_test_files = collect_repo_info(
            instance_info["repo"], instance_info["base_commit"], file, funcs
        )
        if func_dict:
            all_dict.update(func_dict)
        else:
            logging.warning(f"  No functions found in {file} for {instance_id}")
    return all_dict, all_test_files


def parse_response(response):
    """Extract list of relevant test files from model response."""
    if not response:
        return []
    if "---BEGIN RELEVANT TEST FILES---" in response and "---END RELEVANT TEST FILES---" in response:
        section = response.split("---BEGIN RELEVANT TEST FILES---")[1].split("---END RELEVANT TEST FILES---")[0]
        return [line.strip() for line in section.strip().splitlines() if line.strip()]
    return []


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def main(suspicious_info, all_results_json, test_selection_json, confirmed_suspicious_file, dataset_name, model_name):
    all_results, test_selection_results, confirmed_suspicious_funcs = {}, {}, {}
    dataset_info = get_dataset(dataset_name)

    for instance_id, suspicious_funcs in suspicious_info.items():
        logging.info("=" * 80)
        logging.info(f"[START] Instance {instance_id}")
        logging.info(f"Suspicious functions declared: {sum(len(funcs) for funcs in suspicious_funcs.values())}")

        instance_info = get_instance_info(dataset_info, instance_id)
        if not instance_info:
            logging.error(f"No dataset info found for instance {instance_id}")
            logging.info(f"[SKIP] Instance {instance_id}")
            continue

        # extract suspicious function code
        func_dict, all_test_files = get_suspicious_funcs(suspicious_funcs, instance_info, instance_id)
        if not func_dict:
            logging.warning(f"No extracted functions for {instance_id}, skipping.")
            logging.info(f"[END] Instance {instance_id}")
            continue
        logging.info(f"Extracted {len(func_dict)} functions from repo {instance_info['repo']}")

        # confirm suspicious functions
        confirmed_sus = {fp: list(filter(None, funcs)) for fp, funcs in suspicious_funcs.items()}
        confirmed_suspicious_funcs[instance_id] = confirmed_sus

        # compare declared vs extracted
        expected_funcs = {func for funcs in suspicious_funcs.values() for func in funcs}
        extracted_funcs = set(func_dict.keys())
        missing_funcs = expected_funcs - extracted_funcs
        if missing_funcs:
            logging.warning(f"Missing functions ({len(missing_funcs)}): {sorted(missing_funcs)}")

        # prompt model for test file selection
        logging.info("Generating prompt for model...")
        prompt = select_test_file_prompt(
            issue=instance_info['problem_statement'],
            suspicious_funcs=confirmed_sus,
            func_code_list=func_dict,
            all_test_files=all_test_files,
            top_k=10
        )

        logging.info(f"Sending prompt to model: {model_name}")
        response = prompt_model(model_name, prompt=prompt, temperature=0.7)

        # parse and store results
        test_files = parse_response(response)
        logging.info(f"Model selected {len(test_files)} test files")

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

        logging.info(f"[END] Instance {instance_id}")

    # save outputs
    save_json(all_results_json, all_results)
    save_json(test_selection_json, test_selection_results)
    save_json(confirmed_suspicious_file, confirmed_suspicious_funcs)
    logging.info("All instances processed.")


def parse_args():
    parser = argparse.ArgumentParser(description="Prompt model to return top 10 test files")
    parser.add_argument("suspicious_func_json", help="Path to suspicious function JSON file")
    parser.add_argument("output_dir_base", help="Base directory for output files")
    parser.add_argument("dataset_name", choices=["princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench_Lite"], help="Dataset to use")
    parser.add_argument("model_name", help="Model name (e.g., GCP/claude-3-7-sonnet)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    suspicious_info = read_json_file(args.suspicious_func_json)

    output_dir = Path(args.output_dir_base) / "model_test_files" / args.dataset_name.split("/")[-1]
    output_dir.mkdir(parents=True, exist_ok=True)

    all_results_json = output_dir / "model_selected.json"
    test_selection_json = output_dir / "test_file_selection.json"
    confirmed_suspicious_file = output_dir / "confirmed_suspicious_funcs.json"

    main(
        suspicious_info,
        all_results_json,
        test_selection_json,
        confirmed_suspicious_file,
        args.dataset_name,
        args.model_name
    )
