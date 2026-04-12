import argparse
import io
import json
import logging
import os
import re
import sys
import tarfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import docker
from tqdm import tqdm

from utils import read_json_to_dict

from swebench.harness.constants import (
    KEY_INSTANCE_ID,
    KEY_MODEL,
)
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
    remove_image,
)
from swebench.harness.docker_build import (
    build_container,
    build_env_images,
    close_logger,
    setup_logger,
)
from swebench.harness.test_spec import make_test_spec
from swebench.harness.run_evaluation import get_gold_predictions
from swebench.harness.utils import load_swebench_dataset

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def copy_file_from_container(container, container_path, host_path):
    file_name = os.path.basename(container_path)
    tar_stream, _ = container.get_archive(container_path)
    os.makedirs(os.path.dirname(host_path), exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(b"".join(tar_stream)), mode="r|*") as tar:
        for member in tar:
            if member.name.endswith(file_name):
                member.name = os.path.basename(host_path)
                tar.extract(member, path=os.path.dirname(host_path))
                break


def path_to_module(paths):
    return [
        path.replace("/", ".").removesuffix(".py").removeprefix("tests.")
        for path in paths
    ]


def _get_extra_pip_installs(instance_id):
    lines = []
    if "flask" in instance_id:
        lines.append("python -m pip install 'werkzeug<3'\n")
    elif "matplotlib" in instance_id:
        lines.append("python -m pip install 'numpy<2'\n")
    elif "pylint" in instance_id:
        lines.append("python -m pip install --upgrade wrapt\n")
    elif "xarray" in instance_id:
        lines.append("python -m pip install 'numpy<2' 'pandas<2'\n")
    elif "pytest" in instance_id:
        lines.append("python -m pip install hypothesis xmlschema\n")
    elif "astropy" in instance_id:
        lines.append("python -m pip install hypothesis pyerfa 'numpy<=1.23.2'\n")
        lines.append("python -m pip install -e .[test] coverage pytest\n")
    return lines


def _build_coveragerc_commands():
    return [
        'echo "[run]" > .coveragerc\n',
        'echo "dynamic_context = test_function" >> .coveragerc\n',
        'echo "" >> .coveragerc\n',
        'echo "[report]" >> .coveragerc\n',
        'echo "show_missing = True" >> .coveragerc\n',
        'echo "ignore_errors = True" >> .coveragerc\n',
    ]


def _build_django_omit():
    return [
        'echo "omit =" >> .coveragerc\n',
        'echo "    /testbed/generated/*" >> .coveragerc\n',
    ]


def extract_env_setup_script(eval_script):
    lines = eval_script.split('\n')
    setup_lines = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('git checkout ') and len(line.split()) > 2:
            break

        if line.startswith('git apply'):
            break

        if 'EOF_' in line and '<<' in line:
            break

        setup_lines.append(lines[i])
        i += 1

    return '\n'.join(setup_lines) + '\n'


def build_coverage_script(instance_id, test_modules):
    lines = []
    lines.append("\n# === TestLoc: Coverage Generation ===\n")
    lines.append("echo 'Current Git Commit:'\n")
    lines.append("git rev-parse HEAD\n\n")

    for cmd in _get_extra_pip_installs(instance_id):
        lines.append(cmd)

    lines.append("python -m pip install pytest pytest-cov coverage\n\n")
    lines.append("git diff\n")

    for cmd in _build_coveragerc_commands():
        lines.append(cmd)

    is_django = "django" in instance_id
    if is_django:
        for cmd in _build_django_omit():
            lines.append(cmd)

    lines.append("\n")
    lines.append("start_time=$(date +%s)\n")
    lines.append('echo "Start time: $(date -d @$start_time)"\n')

    if is_django:
        modules_str = " ".join(path_to_module(test_modules))
        lines.append(
            f"coverage run ./tests/runtests.py {modules_str} "
            "--verbosity 2 --settings=test_sqlite --parallel 1\n"
        )
    else:
        if test_modules:
            test_files_str = " ".join(test_modules)
            lines.append(f"coverage run -m pytest -vv {test_files_str}\n")
        else:
            lines.append("coverage run -m pytest -vv\n")

    lines.append("end_time=$(date +%s)\n")
    lines.append('echo "End time: $(date -d @$end_time)"\n')
    lines.append("coverage report -m\n\n")
    lines.append("chmod 777 /testbed/.coverage\n")
    lines.append("chmod 777 /testbed/.coveragerc\n")

    return "".join(lines)


def run_instance_coverage(
    test_spec,
    pred,
    client,
    run_id,
    test_modules,
    cov_dir,
    timeout=36000,
    force=False,
    rm_image=False,
    force_rebuild=False,
):
    instance_id = test_spec.instance_id
    host_cov_dir = os.path.join(cov_dir, instance_id)
    host_cov_path = os.path.join(host_cov_dir, f"{instance_id}_coverage")

    if os.path.exists(host_cov_path) and not force:
        logging.info(f"[SKIP] {instance_id}: coverage already exists at {host_cov_path}")
        return instance_id, "skipped"

    os.makedirs(host_cov_dir, exist_ok=True)
    log_file = Path(host_cov_dir) / "instance.log"
    logger = setup_logger(instance_id, log_file)

    container = None
    try:
        container = build_container(
            test_spec, client, run_id, logger, rm_image, force_rebuild
        )
        container.start()
        logger.info(f"Container for {instance_id} started: {container.id}")

        env_setup = extract_env_setup_script(test_spec.eval_script)
        coverage_snippet = build_coverage_script(instance_id, test_modules)
        full_script = env_setup + coverage_snippet

        eval_file = Path(host_cov_dir) / "eval.sh"
        eval_file.write_text(full_script)
        logger.info(f"Eval script written to {eval_file}")
        copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))

        test_output, timed_out, total_runtime = exec_run_with_timeout(
            container, "/bin/bash /eval.sh", timeout
        )
        logger.info(f"Test runtime: {total_runtime:_.2f} seconds")

        if timed_out:
            logger.warning(f"Timed out after {timeout} seconds")

        test_output_path = Path(host_cov_dir) / "test_output.txt"
        test_output_path.write_text(test_output)
        logger.info(f"test_output.txt saved to {test_output_path}")

        try:
            copy_file_from_container(
                container, "/testbed/.coverage", host_cov_path
            )
            logger.info(f".coverage copied to {host_cov_path}")
        except Exception as e:
            logger.warning(f"Failed to copy .coverage: {e}")
            return instance_id, "no_coverage"

        try:
            copy_file_from_container(
                container, "/testbed/.coveragerc",
                os.path.join(host_cov_dir, ".coveragerc"),
            )
            logger.info(f".coveragerc copied to {host_cov_dir}/.coveragerc")
        except Exception as e:
            logger.warning(f"Failed to copy .coveragerc: {e} (non-fatal)")

        return instance_id, "success"

    except Exception as e:
        error_msg = f"Error processing {instance_id}: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)
        return instance_id, "error"
    finally:
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)


def run_coverage_batch(
    dataset_name,
    suspicious_funcs,
    selected_tests,
    cov_dir,
    max_workers=4,
    run_id="coverage",
    timeout=36000,
    only_run=None,
    force=False,
    namespace="swebench",
):
    client = docker.from_env()

    dataset = load_swebench_dataset(dataset_name, "test")

    predictions = get_gold_predictions(dataset_name, "test")
    pred_map = {p[KEY_INSTANCE_ID]: p for p in predictions}

    instances_to_process = []
    test_modules_map = {}
    for instance in dataset:
        iid = instance[KEY_INSTANCE_ID]

        if only_run and iid not in only_run:
            continue

        if iid not in suspicious_funcs:
            logging.info(f"[SKIP] {iid}: not in suspicious functions")
            continue

        if iid not in selected_tests:
            logging.info(f"[SKIP] {iid}: not in selected tests")
            continue

        test_files = selected_tests[iid].get("selected_test_files", [])
        if not test_files:
            logging.info(f"[SKIP] {iid}: no selected test files")
            continue

        test_files = [f for f in test_files if not f.endswith("/conftest.py")]
        if not test_files:
            logging.info(f"[SKIP] {iid}: no test files after filtering conftest")
            continue

        if iid not in pred_map:
            logging.info(f"[SKIP] {iid}: no prediction entry")
            continue

        instances_to_process.append(instance)
        test_modules_map[iid] = test_files

    logging.info(f"Processing {len(instances_to_process)} instances for coverage generation")

    if not instances_to_process:
        logging.info("No instances to process. Exiting.")
        return

    build_env_images(client, instances_to_process, False, max_workers)

    test_specs = {
        inst[KEY_INSTANCE_ID]: make_test_spec(inst)
        for inst in instances_to_process
    }

    os.makedirs(cov_dir, exist_ok=True)
    results = {"success": 0, "skipped": 0, "error": 0, "no_coverage": 0}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for inst in instances_to_process:
            iid = inst[KEY_INSTANCE_ID]
            future = executor.submit(
                run_instance_coverage,
                test_spec=test_specs[iid],
                pred=pred_map[iid],
                client=client,
                run_id=run_id,
                test_modules=test_modules_map[iid],
                cov_dir=cov_dir,
                timeout=timeout,
                force=force,
            )
            futures[future] = iid

        for future in tqdm(
            as_completed(futures), total=len(futures), desc="Generating coverage"
        ):
            iid = futures[future]
            try:
                _, status = future.result()
                results[status] = results.get(status, 0) + 1
                logging.info(f"[{status.upper()}] {iid}")
            except Exception as e:
                results["error"] += 1
                logging.error(f"[ERROR] {iid}: {e}")

    logging.info(
        f"Coverage generation complete. "
        f"Success: {results['success']}, Skipped: {results['skipped']}, "
        f"No coverage: {results['no_coverage']}, Errors: {results['error']}"
    )
    logging.info(f"Output directory: {cov_dir}")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_name", required=True,
        choices=["princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench_Lite"])
    parser.add_argument("--suspicious_funcs_file", required=True)
    parser.add_argument("--selected_tests_file", required=True)
    parser.add_argument("--model_name", required=True)
    parser.add_argument("--coverage_dir", required=True)
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument("--run_id", default="testloc_cov")
    parser.add_argument("--timeout", type=int, default=36000)
    parser.add_argument("--only_run", nargs="*", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--namespace", default="swebench")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    dataset_name = args.dataset_name
    dataset_short = dataset_name.split("/")[-1]
    model_safe = args.model_name.replace("/", "_")

    cov_dir = args.coverage_dir
    only_run = set(args.only_run) if args.only_run else None

    logging.info(f"Dataset: {dataset_name}")
    logging.info(f"Coverage output dir: {cov_dir}")
    logging.info(f"Suspicious functions: {args.suspicious_funcs_file}")
    logging.info(f"Selected tests: {args.selected_tests_file}")

    suspicious_funcs = read_json_to_dict(args.suspicious_funcs_file)
    selected_tests = read_json_to_dict(args.selected_tests_file)

    run_coverage_batch(
        dataset_name=dataset_name,
        suspicious_funcs=suspicious_funcs,
        selected_tests=selected_tests,
        cov_dir=cov_dir,
        max_workers=args.max_workers,
        run_id=args.run_id,
        timeout=args.timeout,
        only_run=only_run,
        force=args.force,
        namespace=args.namespace,
    )
