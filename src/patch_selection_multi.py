import argparse
import concurrent.futures as cf
import glob
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

from tqdm.auto import tqdm


@dataclass
class PatchRunResult:
    patch_file: str
    instance_id: str
    passed: int
    failed: int


@dataclass
class ReproOutcome:
    patch_file: str
    instance_id: str
    test_name: str
    passed: bool


class ResultStore:
    """
    - regression: patch_file -> { instance_id -> { test_name -> passed(bool) } }
    - reproduction: patch_file -> { instance_id -> { test_name -> passed(bool) } }
    """

    def __init__(self, base_dir: Path, instance_id: str):
        self.base_dir = Path(base_dir)
        self.instance_id = instance_id
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / f"{instance_id}_inner_results.json"
        self.lock = threading.Lock()
        self.data = {"regression": {}, "reproduction": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass  # start clean if corrupted

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX

    def get(self, phase: str, patch_file: str, test_name: str) -> Optional[bool]:
        # returns True/False if present, else None
        with self.lock:
            return (
                self.data.get(phase, {})
                .get(patch_file, {})
                .get(self.instance_id, {})
                .get(test_name)
            )

    def set(self, phase: str, patch_file: str, test_name: str, passed: bool) -> None:
        with self.lock:
            self.data.setdefault(phase, {}).setdefault(patch_file, {}).setdefault(
                self.instance_id, {}
            )[test_name] = bool(passed)
            self._save()


DEFAULT_UNIQPATCH_TMPL = "all_patches/lite_gpt4o_all_unique_patches/{instance_id}/*.jsonl"
REPO_PATH_IN_CONTAINER = "/testbed"
OCCUR_COUNT_FILE = "regression_reproduction_data/per_instance_patch_counts.json"
RESULTS_DIR_DEFAULT = Path("test_logs_lite_gpt4o/results")


# Logs
DEFAULT_LOG_DIR = Path("logs")

SWEBENCH_CMD = (
    "python -m swebench.harness.run_evaluation "
    "--dataset_name princeton-nlp/SWE-bench_Lite "
    "--predictions_path gold "
    "--max_workers 5 "
    "--run_id {run_id} "
    "--instance_ids {instance_id} "
    "--test_methods '[]'"
)



def setup_root_logging(level: int = logging.WARNING, to_console: bool = False) -> None:
    root = logging.getLogger()
    # Remove any existing handlers (basicConfig adds one to root)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    if to_console:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(level)
        h.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(h)


def get_instance_logger(instance_id: str, log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(f"instance.{instance_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"{instance_id}.log"
    if not any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "_instance_file", None) == logfile
        for h in logger.handlers
    ):
        fh = logging.FileHandler(logfile, mode="a", encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh._instance_file = logfile
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger




def sh(
    cmd: List[str],
    check: bool = True,
    capture_output: bool = False,
    logger: Optional[logging.Logger] = None,
) -> subprocess.CompletedProcess:
    if logger:
        logger.info("RUN: %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True)


def docker_exec(
    container: str,
    *args: str,
    check: bool = True,
    capture_output: bool = False,
    logger: Optional[logging.Logger] = None,
) -> subprocess.CompletedProcess:
    return sh(
        ["docker", "exec", container, *args],
        check=check,
        capture_output=capture_output,
        logger=logger,
    )


def docker_cp(
    src: str, dest: str, check: bool = True, logger: Optional[logging.Logger] = None
) -> subprocess.CompletedProcess:
    return sh(["docker", "cp", src, dest], check=check, logger=logger)


def docker_stop_rm(container_id: str, logger: Optional[logging.Logger]) -> None:
    try:
        sh(["docker", "stop", container_id], check=False, logger=logger)
    finally:
        sh(["docker", "rm", container_id], check=False, logger=logger)


def git_reset_clean(
    container: str, repo_path: str, logger: Optional[logging.Logger]
) -> None:
    docker_exec(
        container, "git", "-C", repo_path, "reset", "--hard", check=True, logger=logger
    )
    docker_exec(
        container, "git", "-C", repo_path, "clean", "-fd", check=True, logger=logger
    )


def apply_patch_in_container(
    container: str,
    repo_path: str,
    host_patch: Path,
    remote_path: str,
    logger: Optional[logging.Logger],
) -> None:
    docker_cp(str(host_patch), f"{container}:{remote_path}", logger=logger)
    docker_exec(
        container,
        "git",
        "-C",
        repo_path,
        "apply",
        "--whitespace=fix",
        remote_path,
        check=True,
        logger=logger,
    )


import shlex


def run_tests(
    container: str, test_name: str, instance_id: str, logger: Optional[logging.Logger]
) -> int:
    if "django" in instance_id:
        return run_django_test(container, test_name, logger)
    else:
        return run_pytest(container, test_name, logger)


def run_pytest(container: str, test_name: str, logger: Optional[logging.Logger]) -> int:
    quoted = shlex.quote(test_name)
    cmd = (
        "bash",
        "-c",
        "set -euo pipefail; "
        r"grep -Evi 'pip[[:space:]]+install|(^|[[:space:]])git[[:space:]]+' /eval.sh > /eval_test.sh; "
        f"printf '\\npytest -vv {quoted}\\n' >> /eval_test.sh; "
        "chmod +x /eval_test.sh; "
        # "cat /eval_test.sh; "
        "/eval_test.sh",
    )
    res = docker_exec(container, *cmd, check=False, capture_output=True, logger=logger)
    if logger:
        if res.stdout:
            logger.info("[TEST STDOUT]\n%s", res.stdout)
        if res.stderr:
            logger.info("[TEST STDERR]\n%s", res.stderr)
        # logger.info(res)
        logger.info("Test %s -> returncode %s", test_name, res.returncode)
    return res.returncode


def run_django_test(
    container: str, test_name: str, logger: Optional[logging.Logger]
) -> int:
    quoted = shlex.quote(test_name)
    cmd = (
        "bash",
        "-c",
        "set -euo pipefail; "
        r"grep -Evi 'pip[[:space:]]+install|(^|[[:space:]])git[[:space:]]+' /eval.sh > /eval_test.sh; "
        f"printf '\\n./tests/runtests.py --verbosity=2 --settings=test_sqlite --parallel 1 {quoted}\\n' >> /eval_test.sh; "
        "chmod +x /eval_test.sh; "
        # "cat /eval_test.sh; "
        "/eval_test.sh",
        # f"./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 {test_name}"
    )
    res = docker_exec(container, *cmd, check=False, capture_output=True, logger=logger)
    if logger:
        if res.stdout:
            logger.info("[TEST STDOUT]\n%s", res.stdout)
        if res.stderr:
            logger.info("[TEST STDERR]\n%s", res.stderr)
        logger.info("Test %s -> returncode %s", test_name, res.returncode)
    return res.returncode


def iter_jsonl_entries(jsonl_path: Path) -> Iterable[dict]:
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)




def evaluate_patches(
    container: str,
    instance_id: str,
    jsonl_glob: str,
    tests: List[str],
    repo_path: str,
    logger: logging.Logger,
    store: Optional[ResultStore] = None,
) -> Tuple[List[PatchRunResult], List[PatchRunResult]]:
    """
    Apply each model_patch across all JSONL files, run selected tests, and return results and top subset.
    """
    jsonl_files = sorted(glob.glob(jsonl_glob))
    logger.info("Found %d JSONL files", len(jsonl_files))
    results: List[PatchRunResult] = []

    for jsonl_path in map(Path, jsonl_files):
        logger.info("=== Processing %s ===", jsonl_path)
        for entry in iter_jsonl_entries(jsonl_path):
            inst = entry.get("instance_id")
            model_patch = entry.get("model_patch")
            if not inst or not model_patch:
                logger.info("Skipping entry without valid instance_id or model_patch")
                continue

            logger.info("==> Running patch for: %s", inst)
            temp_patch_path = Path(f"temp_patch_{inst}_{jsonl_path.stem}.patch")

            try:
                temp_patch_path.write_text(model_patch, encoding="utf-8")

                git_reset_clean(container, repo_path, logger)
                remote_patch = f"/tmp/patch_{inst}_{jsonl_path.stem}.patch"
                try:
                    apply_patch_in_container(
                        container, repo_path, temp_patch_path, remote_patch, logger
                    )
                except subprocess.CalledProcessError as e:
                    logger.info(
                        "Patch failed to apply for %s (%s). Skipping. Error: %s",
                        inst,
                        jsonl_path,
                        e,
                    )
                    continue

                # Run tests (with caching)
                passed = failed = 0
                per_test_outcomes = {}  # collect for summary
                for test in tests:
                    cached = (
                        store.get("regression", str(jsonl_path), test)
                        if store
                        else None
                    )
                    if cached is not None:
                        rc_passed = cached
                        logger.info(
                            "   → [cache] %s -> %s",
                            test,
                            "PASS" if rc_passed else "FAIL",
                        )
                    else:
                        logger.info("   → Running test: %s", test)
                        rc = run_tests(container, test, inst, logger)
                        rc_passed = rc == 0
                        if store:
                            store.set("regression", str(jsonl_path), test, rc_passed)

                    per_test_outcomes[test] = rc_passed
                    if rc_passed:
                        passed += 1
                    else:
                        failed += 1

                results.append(PatchRunResult(str(jsonl_path), inst, passed, failed))

            finally:
                # cleanup per entry
                try:
                    if temp_patch_path.exists():
                        temp_patch_path.unlink()
                except Exception as _e:
                    logger.debug("Temp cleanup failed for %s: %s", temp_patch_path, _e)

    # sort highest passed first, tie-breaker lowest failed
    results.sort(key=lambda r: (-r.passed, r.failed))
    max_passed = max((r.passed for r in results), default=0)
    top = [r for r in results if r.passed == max_passed] if results else []
    logger.info("Computed top subset: %d entries (max passed=%d)", len(top), max_passed)
    logger.info(top)
    return results, top



def verify_repro_fails_on_clean_repo(
    container: str,
    repo_path: str,
    test_patch: str,
    test_name: str,
    logger: logging.Logger,
    instance_id: str,
    store: Optional[ResultStore] = None,
    test_idx: int = 0,
) -> bool:
    """
    Return True if the repro test FAILS on the clean repo (as expected).
    Also stores result in the store under "fail_on_original_repo".
    """
    label = f"{test_name}#{test_idx}"
    
    if store:
        cached = store.get("reproduction", "fail_on_original_repo", label)
        if cached is not None:
            logger.info("[cache] Original repo test %s -> %s", label, "FAIL" if cached else "PASS")
            return bool(cached)

    logger.info("Verifying repro test fails on clean repo: %s", test_name)
    git_reset_clean(container, repo_path, logger)

    TEMP_PATCH_PATH = Path(
        f"temp_repro_patch_{instance_id}_{test_name.split('/')[-1].split('::')[-1]}.patch"
    )
    TEMP_PATCH_PATH.write_text(test_patch, encoding="utf-8")
    remote_candidate = f"/tmp/repro_patch_{instance_id}.patch"
    
    apply_patch_in_container(
        container, repo_path, TEMP_PATCH_PATH, remote_candidate, logger
    )

    try:
        TEMP_PATCH_PATH.unlink()
    except Exception as e:
        logger.debug("Failed to cleanup temp patch file %s: %s", TEMP_PATCH_PATH, e)

    rc = run_tests(container, test_name, instance_id, logger)
    passed = rc == 0
    fails_on_clean_repo = not passed

    if store:
        store.set("reproduction", "fail_on_original_repo", label, fails_on_clean_repo)

    if passed:
        logger.warning(
            "Reproduction test unexpectedly PASSED on clean repo: %s", test_name
        )
        return False

    logger.info("Reproduction test correctly FAILS on clean repo: %s", test_name)
    return True


def evaluate_top_patches_with_repro(
    container: str,
    repo_path: str,
    top: List[PatchRunResult],
    test_patch: str,
    test_name: str,
    logger: logging.Logger,
    test_idx: int,
    store: Optional[ResultStore] = None,
) -> List[ReproOutcome]:
    """
    For each top patch, apply it to a clean repo and run the reproduction test.
    """
    outcomes: List[ReproOutcome] = []

    for pr in top:
        model_patch = None
        for entry in iter_jsonl_entries(Path(pr.patch_file)):
            if entry.get("instance_id") == pr.instance_id:
                model_patch = entry.get("model_patch")
                break
        if not model_patch:
            logger.info(
                "Skipping %s (no model_patch for %s)", pr.patch_file, pr.instance_id
            )
            continue

        # Apply candidate patch on clean repo
        try:
            git_reset_clean(container, repo_path, logger)
            TMP_TEST_ATCH_PATH = Path(
                f"temp_test_patch_{pr.instance_id}_{test_name.split('/')[-1].split('::')[-1]}.patch"
            )
            TMP_TEST_ATCH_PATH.write_text(test_patch, encoding="utf-8")
            remote_test_candidate = f"/tmp/test_patch_{pr.instance_id}.patch"
            apply_patch_in_container(
                container, repo_path, TMP_TEST_ATCH_PATH, remote_test_candidate, logger
            )

            TEMP_PATCH_PATH = Path(f"temp_patch_{pr.instance_id}.patch")
            TEMP_PATCH_PATH.write_text(model_patch, encoding="utf-8")
            remote_candidate = f"/tmp/patch_{pr.instance_id}.patch"
            apply_patch_in_container(
                container, repo_path, TEMP_PATCH_PATH, remote_candidate, logger
            )
        finally:
            if TEMP_PATCH_PATH.exists():
                TEMP_PATCH_PATH.unlink()
            if TMP_TEST_ATCH_PATH.exists():
                TMP_TEST_ATCH_PATH.unlink()

        logger.info(
            "Running reproduction test on patch from %s (%s)",
            pr.patch_file,
            pr.instance_id,
        )
        # rc = run_tests(container, test_name, pr.instance_id, logger)
        label = test_name  # already transformed
        cached = (
            store.get("reproduction", pr.patch_file, f"{label}#{test_idx}")
            if store
            else None
        )
        if cached is not None:
            outcomes.append(
                ReproOutcome(
                    pr.patch_file, pr.instance_id, f"{label}#{test_idx}", cached
                )
            )
            logger.info(
                "[cache] reproduction %s -> %s (patch: %s)",
                f"{label}#{test_idx}",
                "PASS" if cached else "FAIL",
                pr.patch_file,
            )
            continue

        # (apply test patch + candidate patch like you do now)
        rc = run_tests(container, label, pr.instance_id, logger)
        passed = rc == 0
        outcomes.append(
            ReproOutcome(pr.patch_file, pr.instance_id, f"{label}#{test_idx}", passed)
        )
        if store:
            store.set("reproduction", pr.patch_file, f"{label}#{test_idx}", passed)
    return outcomes




def run_swebench_and_get_container(
    instance_id: str, run_id: str, logger: logging.Logger
) -> str:
    """
    Run the SWE-bench harness command for a single instance and return its container id.
    """
    cmd = SWEBENCH_CMD.format(run_id=run_id, instance_id=instance_id)
    logger.info("Launching SWE-bench harness to create container: %s", cmd)
    res = sh(shlex.split(cmd), check=False, capture_output=True, logger=logger)

    if res.stdout:
        logger.info("[SWEBENCH STDOUT]\n%s", res.stdout)
    if res.stderr:
        logger.info("[SWEBENCH STDERR]\n%s", res.stderr)

    out = (res.stdout or "") + "\n" + (res.stderr or "")

    cid = None
    for line in out.splitlines():
        if f"Container for {instance_id}" in line and "started" in line:
            cid = line.split("started: ")[-1].strip()
            if cid:
                logger.info("Container started for %s: %s", instance_id, cid)
                return cid

    if not cid:
        raise RuntimeError(
            "Could not determine container id for instance: " + instance_id
        )




def _to_module_from_tests_py(path_b: str) -> str:
    """
    Convert e.g. 'tests/expressions_case/tests.py' -> 'tests.expressions_case.tests'
    """
    p = PurePosixPath(path_b)
    parts = list(p.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def extract_testname_from_patch(patch_text: str) -> List[str]:
    file_path, class_name, func_name = None, None, None
    for line in patch_text.splitlines():
        if line.startswith("--- a/"):
            file_path = line.split("a/")[-1].strip()

        if class_name == None and "@@ class " in line:
            class_name = line.split("@@ class ")[-1].split(":")[0].split("(")[0].strip()

        if class_name == None and " class " in line:
            class_name = line.split(" class ")[-1].split(":")[0].split("(")[0].strip()

        if class_name != None:
            if (
                func_name == None
                and line.startswith("+ ")
                and "def " in line
                and "test" in line
            ):
                func_name = line.split("def ")[-1].split("(")[0].strip()
                # print("func_name", func_name)
                return f"{file_path}::{class_name}::{func_name}"
                break
            if func_name == None and "def " in line and "test" in line:
                func_name = line.split("def ")[-1].split("(")[0].strip()
                # print("func_name", func_name)
                return f"{file_path}::{class_name}::{func_name}"
                break
        if class_name == None:
            if "@@ def " in line and "test" in line:
                func_name = line.split("@@ def ")[-1].split("(")[0].strip()
                return f"{file_path}::{func_name}"
    raise ValueError("Could not extract test name from patch text")


def transform_testname(test_name: str, instance_id: str) -> str:
    # tests/expressions_case/tests.py::CaseWhenTests::test_negated_empty_q_object_in_when
    runname = None
    if "django" in instance_id:
        runname = (
            test_name.replace("tests/", "", 1)
            .replace(".py::", "::")
            .replace("::", ".")
            .replace("/", ".")
        )
        return runname
    elif "matplotlib" in instance_id:
        if not test_name.startswith("lib/"):
            runname = "lib/" + test_name
        else:
            runname = test_name
        return runname
    elif "pylint" in instance_id or "sphinx" in instance_id:
        if not test_name.startswith("tests/"):
            runname = "tests/" + test_name
        else:
            runname = test_name
        return runname
    elif "pytest" in instance_id:
        if not test_name.startswith("testing/"):
            runname = "testing/" + test_name
        else:
            runname = test_name
        return runname
    else:
        return test_name


def fqn_to_pytest(fqn: str) -> str:
    """
    Convert a Python fully qualified name to a pytest-compatible node ID.
    - astropy.modeling.tests.test_separable.test_separable
      => astropy/modeling/tests/test_separable.py::test_separable
    - astropy.modeling.tests.test_separable.TestClass.test_method
      => astropy/modeling/tests/test_separable.py::TestClass::test_method
    """
    parts = fqn.strip().split(".")

    if len(parts) < 2:
        raise ValueError(f"Invalid FQN: {fqn}")

    # if last two elements are class + method
    if len(parts) >= 3 and parts[-2][0].isupper():
        file_path = os.path.join(*parts[:-2]) + ".py"
        suffix = f"::{parts[-2]}::{parts[-1]}"
    else:
        file_path = os.path.join(*parts[:-1]) + ".py"
        suffix = f"::{parts[-1]}"

    return file_path + suffix


def transform_fq_name(regression_tests, instance_id):
    if "django" in instance_id:
        return regression_tests
    fq_regression_tests = [fqn_to_pytest(test) for test in regression_tests]
    if "matplotlib" in instance_id:
        fq_regression_tests = [
            test if test.startswith("lib/") else f"lib/{test}"
            for test in fq_regression_tests
        ]
    elif "pylint" in instance_id or "sphinx" in instance_id:
        fq_regression_tests = [
            test if test.startswith("tests/") else f"tests/{test}"
            for test in fq_regression_tests
        ]
    return fq_regression_tests


def process_instance(
    instance_id: str,
    all_uniq_patch_jsonl: str,
    regression_tests: List[str],
    repro_tests_for_instance: List[str],
    repo_path: str,
    log_dir: Path,
    run_id_prefix: str,
    **kwargs,
) -> Dict[str, object]:
    """
    Pipeline for a single instance:
      1) Launch SWE-bench, capture container id.
      2) evaluate patches against regression tests -> results + top subset.
      3) For each reproduction test (from JSON):
           - verify it fails on clean repo
           - evaluate only the TOP subset against that repro test
      4) Stop & remove container.
    """
    results_dir = kwargs.get("results_dir", RESULTS_DIR_DEFAULT)
    store = ResultStore(Path(results_dir), instance_id)
    logger = get_instance_logger(instance_id, log_dir)
    summary: Dict[str, object] = {
        "instance_id": instance_id,
        "container_id": None,
        "regression": {},
        "reproduction": {},
    }

    # Start container via SWE-bench
    run_id = f"{run_id_prefix}"
    container_id = run_swebench_and_get_container(instance_id, run_id, logger)
    summary["container_id"] = container_id

    regression_tests = transform_fq_name(regression_tests, instance_id)

    try:
        # run regression tests
        jsonl_glob = all_uniq_patch_jsonl.format(instance_id=instance_id)
        # """
        all_results, top = evaluate_patches(
            container=container_id,
            instance_id=instance_id,
            jsonl_glob=jsonl_glob,
            tests=regression_tests,
            repo_path=repo_path,
            logger=logger,
            store=store,
        )
        summary["regression"] = {
            "results": [
                {
                    "patch_file": r.patch_file,
                    "inst": r.instance_id,
                    "passed": r.passed,
                    "failed": r.failed,
                }
                for r in all_results
            ],
            "top_passed": max((r.passed for r in all_results), default=0),
            "top_subset_size": len(top),
        }
        # """

        # run reproduction tests
        test_idx = 0
        for test_patch in repro_tests_for_instance:
            # apply the test_patch to the clean repo and run the repro test
            # print(test_patch)
            test_idx += 1
            if "reproduce_bug" in test_patch:
                patched_test_name = "reproduce_bug_new.py"
            else:
                patched_test_name = extract_testname_from_patch(test_patch)
            test_name = transform_testname(patched_test_name, instance_id)
            # print(f"testname: {test_name}")
            logger.info(f"Reproduction test {test_idx}")
            logger.info(f"testname: {test_name}")
            logger.info(test_patch)

            label = f"{test_name}#{test_idx}"
            repro_fails = verify_repro_fails_on_clean_repo(
                container=container_id,
                repo_path=repo_path,
                test_patch=test_patch,
                test_name=test_name,
                logger=logger,
                instance_id=instance_id,
                store=store,
                test_idx=test_idx,
            )
            
            summary["reproduction"][label] = {
                "repro_fails_on_clean": repro_fails, #repro_fails_on_clean
                "outcomes": [],
            }

            if not repro_fails:
                logger.warning(
                    "Skipping repro evaluation for %s (test %s does not fail on clean repo).",
                    instance_id,
                    test_name,
                )
                continue

            outcomes = evaluate_top_patches_with_repro(
                container=container_id,
                repo_path=repo_path,
                top=top,
                test_patch=test_patch,
                test_name=test_name,
                logger=logger,
                test_idx=test_idx,
                store=store,
            )

            summary["reproduction"][label]["outcomes"] = [
                {
                    "patch_file": o.patch_file,
                    "inst": o.instance_id,
                    "passed": o.passed,
                }
                for o in outcomes
            ]


            logger.info(
                "Reproduction test %s for instance %s: %d outcomes",
                f"{test_name}_{test_idx}",
                instance_id,
                len(outcomes),
            )

    finally:
        logger.info("Cleaning up container %s", container_id)
        docker_stop_rm(container_id, logger)

    # final = select_final_patches([summary], instance_id, top)
    # summary["final_patches"] = final

    logger.info(
        "Summary for instance %s: %s", instance_id, json.dumps(summary, indent=2)
    )
    summary_file = log_dir / f"{instance_id}_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Summary written to %s", summary_file)

    return summary


def normalize_patch(text: str) -> str:
    _whitespace_re = re.compile(r"\s+")
    """Canonicalize a patch so differences in spaces/newlines don't matter."""
    if text is None:
        return ""
    # Strip ends, unify all whitespace runs (spaces, tabs, newlines) to a single space
    return _whitespace_re.sub(" ", text.strip())


def best_patch_by_occurrence(
    instance_id: str, candidate_patches: List[str], counts_path: str = OCCUR_COUNT_FILE
) -> Tuple[Optional[str], int]:
    with open(counts_path, encoding="utf-8") as f:
        data = json.load(f)

    if len(candidate_patches) == 0:
        raise ValueError("No candidate_patches!")

    inst_map = data[instance_id]

    norm_counts = {}
    for raw_patch, cnt in inst_map.items():
        norm = normalize_patch(raw_patch)
        norm_counts[norm] = int(cnt)

    best_idx = -1
    best_count = -1

    for i, cand in enumerate(candidate_patches):
        norm = normalize_patch(cand)
        cnt = norm_counts[norm]
        if cnt > best_count:
            best_idx, best_count = i, cnt
            print("Current Best:", best_count, "\n", candidate_patches[best_idx])

    if best_idx == -1:
        return None, 0
    return candidate_patches[best_idx], best_count


def select_final_patches(
    summaries: List[Dict[str, object]], instance_id: str, top: List
) -> List[Dict[str, object]]:
    # gd_patch_file = "SWE-bench/resolved_instance_merged_patches.json"
    # if not Path(gd_patch_file).exists():
    #     raise FileNotFoundError(f"Ground truth patch file not found: {gd_patch_file}")
    # gd_patches = json.loads(Path(gd_patch_file).read_text(encoding="utf-8"))
    # if instance_id not in gd_patches:
    #     raise ValueError(
    #         f"Instance ID {instance_id} not found in ground truth patches."
    #     )
    # gd_patch = gd_patches[instance_id]
    # gd_patch_norm = [normalize_patch(patch) for patch in gd_patch]

    final_patches = []
    for summary in summaries:
        instance_id = summary["instance_id"]
        for test_name, repro_data in summary["reproduction"].items():
            if not repro_data["repro_fails_on_clean"]:
                continue
            for outcome in repro_data["outcomes"]:
                if outcome["passed"] and outcome["inst"] == instance_id:
                    patch_content = json.loads(
                        Path(outcome["patch_file"]).read_text(encoding="utf-8")
                    )
                    res = {
                        "instance_id": instance_id,
                        "test_name": test_name,
                        "patch_file": outcome["patch_file"],
                        "patch_content": patch_content,
                        # "if_ground_truth_match": normalize_patch(
                        #     patch_content["model_patch"]
                        # )
                        # in gd_patch_norm,
                        "if_by_counts": False,
                    }
                    if res not in final_patches:
                        final_patches.append(res)
                    # check if patch content match ground truth

    if len(final_patches) == 0:
        candidate_patches = []
        for outcome in repro_data["outcomes"]:
            if outcome["inst"] == instance_id:
                patch_content = json.loads(
                    Path(outcome["patch_file"]).read_text(encoding="utf-8")
                )
                candidate_patches.append(patch_content["model_patch"])
        if len(candidate_patches) == 0:
            for p in top:
                patch_content = json.loads(
                    Path(p.patch_file).read_text(encoding="utf-8")
                )
                candidate_patches.append(patch_content["model_patch"])
        if len(candidate_patches) == 0:
            for p in summary["regression"]["results"]:
                patch_content = json.loads(
                    Path(p["patch_file"]).read_text(encoding="utf-8")
                )
                candidate_patches.append(patch_content["model_patch"])
        print(len(candidate_patches))
        best_patch, cnt = best_patch_by_occurrence(
            instance_id, list(set(candidate_patches))
        )
        print(best_patch, cnt)
        if best_patch:
            res = {
                "instance_id": instance_id,
                "patch_content": best_patch,
                "freq": cnt,
                # "if_ground_truth_match": normalize_patch(best_patch) in gd_patch_norm,
                "if_by_counts": True,
            }
            if res not in final_patches:
                final_patches.append(res)

    return final_patches


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="patch selection")
    p.add_argument(
        "--instances-file",
        required=True,
        help='Path to JSON list of instance ids, e.g., ["django__django-15930"]',
    )
    p.add_argument(
        "--reproduction-tests-json",
        required=True,
        help="Path to JSON mapping instance_id -> list of reproduction test labels.",
    )
    p.add_argument(
        "--tests-file",
        required=False,
        help="JSON file with a list of regression tests.",
    )
    p.add_argument(
        "--all_uniq_patch_jsonl",
        default=DEFAULT_UNIQPATCH_TMPL,
        help=f"Patches in JSONL. Default: {DEFAULT_UNIQPATCH_TMPL}",
    )
    p.add_argument(
        "--repo-path",
        default=REPO_PATH_IN_CONTAINER,
        help="Path to the repo inside the container. Default: /testbed",
    )
    p.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help="Directory to write per-instance logs. Default: logs/",
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="Thread pool size. Default: number of CPUs.",
    )
    p.add_argument(
        "--run-id-prefix", default="test00", help="Prefix for SWE-bench --run_id."
    )
    p.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    p.add_argument(
        "--results-dir",
        default=str(RESULTS_DIR_DEFAULT),
        help="Directory to persist per-test results to avoid re-running.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=1800,  # 1800 seconds = 30 min
        help="Timeout per instance in seconds (default: 1800)",
    )

    return p.parse_args()


def load_tests_file(path: Optional[str]) -> List[str]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def load_instances(instances_file: str) -> List[str]:
    data = json.loads(Path(instances_file).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("--instances-file must be a JSON list of instance ids")
    return data


def load_repro_tests(reproduction_tests_json: str) -> Dict[str, List[str]]:
    data = json.loads(Path(reproduction_tests_json).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            "--reproduction-tests-json must be a JSON object mapping instance_id -> list[str]"
        )
    normalized: Dict[str, List[str]] = {}
    for k, v in data.items():
        if isinstance(v, list):
            normalized[k] = [str(x) for x in v]
        elif isinstance(v, str):
            normalized[k] = [v]
        else:
            raise ValueError(f"Invalid reproduction test list for {k}: {v!r}")
    return normalized


def main():
    args = parse_args()
    # setup_root_logging(getattr(logging, args.log_level))
    setup_root_logging(to_console=False)
    log = logging.getLogger("main")

    instances = load_instances(args.instances_file)
    repro_tests_map = load_repro_tests(args.reproduction_tests_json)
    regression_tests = load_tests_file(args.tests_file)
    log_dir = Path(args.log_dir)

    print(
        f"Starting evaluation for {len(instances)} instance(s) with up to {args.max_workers} worker(s). "
        f"Regression tests loaded: {len(regression_tests)}"
    )

    summaries: List[Dict[str, object]] = []
    future_to_inst = {}

    with ProcessPoolExecutor(max_workers=args.max_workers) as ex:
        futures = []

        for inst in tqdm(instances, desc="Scheduling", unit="inst"):
            # if not repro_tests_map.get(inst):
            #     log.warning("No reproduction tests provided for %s; skipping.", inst)
            #     continue
            # if inst in skip:
            #     print(f"Skipping {inst} (in skip list)")
            #     continue
            # if inst != "django__django-11333":
            #     continue

            summary_result = Path(log_dir) / f"{inst}_summary.json"
            if summary_result.exists():
                print(f"Skipping {inst} (summary exists: {summary_result})")
                continue

            future = ex.submit(
                process_instance,
                inst,
                args.all_uniq_patch_jsonl,
                regression_tests.get(inst, []),
                repro_tests_map.get(inst, []),
                args.repo_path,
                log_dir,
                args.run_id_prefix,
                results_dir=Path(args.results_dir),
            )
            future_to_inst[future] = inst
            futures.append(future)

        for fut in tqdm(
            as_completed(futures), total=len(futures), desc="Processing", unit="inst"
        ):
            inst = future_to_inst[fut]
            try:
                summaries.append(fut.result(timeout=args.timeout))
            except TimeoutError:
                print(f"Timeout: instance {inst} exceeded {args.timeout} seconds")
            except Exception as e:
                print(f"Instance {inst} failed with error: {e}")


if __name__ == "__main__":
    main()
