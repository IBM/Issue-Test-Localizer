import json
import re
import subprocess
import os
import shlex
from pathlib import Path
from typing import Optional, Tuple, Dict, List


# Default test commands for different project types
TEST_COMMANDS = {
    "pytest": "python -m pytest -xvs",
    "django": "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
    "sympy": "./bin/test -C --verbose",
    "sphinx": "tox --current-env -epy39 -v --",
}


def parse_sympy_test_name(test_name):
    # type: (str) -> Tuple[str, str]
    """Parse a sympy test name into file path and function name.

    Args:
        test_name: Test name in dotted format, e.g.,
                   'sympy.matrices.tests.test_matmul.test_func'

    Returns:
        Tuple of (file_path, function_name)
    """
    parts = test_name.split(".")

    # Find where the test file ends and the test function begins
    file_parts = []
    func_parts = []
    found_test_file = False

    for i, part in enumerate(parts):
        if not found_test_file:
            file_parts.append(part)
            if part.startswith("test_") and i < len(parts) - 1:
                found_test_file = True
        else:
            func_parts.append(part)

    file_path = "/".join(file_parts) + ".py"
    func_name = func_parts[0] if func_parts else ""
    return file_path, func_name


def convert_test_name_for_framework(test_name: str, framework: str) -> str:
    """Convert a dotted test name to the format expected by the test framework.

    Args:
        test_name: Test name in dotted format, e.g.,
                   'sympy.matrices.tests.test_matmul.test_func'
        framework: The test framework being used ('sympy', 'django', 'pytest', etc.)

    Returns:
        Test name in the format expected by the framework.
    """
    if framework == "sympy":
        # Sympy's ./bin/test accepts dotted paths directly
        # e.g., ./bin/test sympy.matrices.expressions.tests.test_matmul.test_func
        return test_name

    elif framework == "django":
        # Django test runner accepts dotted paths directly
        # e.g., auth_tests.test_views.ChangelistTests.test_user_change_password
        return test_name

    elif framework == "pytest":
        # Pytest can accept dotted paths but prefers file::function format
        # For now, just return as-is since pytest is flexible
        return test_name

    elif framework == "sphinx":
        # Sphinx uses pytest, convert to file::function format
        parts = test_name.split(".")
        file_parts = []
        func_parts = []
        found_test_file = False

        for i, part in enumerate(parts):
            if not found_test_file:
                file_parts.append(part)
                if part.startswith("test_") and i < len(parts) - 1:
                    found_test_file = True
            else:
                func_parts.append(part)

        if func_parts:
            file_path = "/".join(file_parts) + ".py"
            test_func = "::".join(func_parts)
            return f"{file_path}::{test_func}"
        else:
            return "/".join(parts) + ".py"

    else:
        # Default: return as-is
        return test_name


def build_sympy_test_command(base_cmd, test_names):
    # type: (List[str], List[str]) -> List[str]
    """Build sympy test command with proper -k filtering.

    Sympy's test runner uses glob patterns for files and -k for function filtering.

    Args:
        base_cmd: Base command like ['./bin/test', '-C', '--verbose']
        test_names: Original dotted test names

    Returns:
        Complete command with file paths and -k filter
    """
    # Group tests by file
    file_to_funcs = {}  # type: Dict[str, List[str]]
    for test_name in test_names:
        file_path, func_name = parse_sympy_test_name(test_name)
        if file_path not in file_to_funcs:
            file_to_funcs[file_path] = []
        if func_name:
            file_to_funcs[file_path].append(func_name)

    # Build command with unique file paths
    cmd = list(base_cmd)
    file_paths = list(file_to_funcs.keys())
    cmd.extend(file_paths)

    # Add -k filter for all function names
    all_funcs = []
    for funcs in file_to_funcs.values():
        all_funcs.extend(funcs)

    if all_funcs:
        # Use "or" to match any of the functions
        k_filter = " or ".join(all_funcs)
        cmd.extend(["-k", k_filter])

    return cmd


def get_instance_id_from_registry() -> str:
    """Read instance ID from the SWE-agent registry."""
    registry_file = Path("/root/.swe-agent-env")
    if registry_file.exists():
        try:
            data = json.loads(registry_file.read_text())
            return data.get("INSTANCE_ID", "")
        except (json.JSONDecodeError, KeyError):
            pass
    return ""


def detect_framework(instance_id: str) -> str:
    """Detect the test framework based on instance ID."""
    instance_lower = instance_id.lower()
    if instance_lower.startswith("django"):
        return "django"
    elif instance_lower.startswith("sympy"):
        return "sympy"
    elif instance_lower.startswith("sphinx"):
        return "sphinx"
    elif instance_lower.startswith("scikit-learn") or instance_lower.startswith("sklearn"):
        return "pytest"
    elif instance_lower.startswith("matplotlib"):
        return "pytest"
    elif instance_lower.startswith("pylint"):
        return "pytest"
    else:
        return "pytest"


def detect_test_command(instance_id):
    # type: (str) -> List[str]
    """Detect the appropriate test command based on instance ID."""
    # Check environment variable first
    env_cmd = os.environ.get("SWE_TEST_CMD", "")
    if env_cmd:
        return shlex.split(env_cmd)

    framework = detect_framework(instance_id)
    return shlex.split(TEST_COMMANDS.get(framework, TEST_COMMANDS["pytest"]))


class RegressionTestRunner:
    """Runner for regression tests stored in a JSON file."""

    def __init__(
        self,
        test_file: str = "/root/regression_tests.json",
        instance_id: Optional[str] = None
    ):
        self.test_file = Path(test_file)
        # Try multiple sources for instance ID
        self.instance_id = (
            instance_id
            or os.environ.get("SWE_INSTANCE_ID", "")
            or get_instance_id_from_registry()
        )
        self.all_tests = self._load_tests()
        self.framework = detect_framework(self.instance_id)
        self.test_cmd = detect_test_command(self.instance_id)

    def convert_test_names(self, test_names):
        # type: (List[str]) -> List[str]
        """Convert test names to the format expected by the test framework."""
        return [convert_test_name_for_framework(name, self.framework) for name in test_names]

    def _load_tests(self) -> dict:
        """Load tests from JSON file."""
        if not self.test_file.exists():
            return {}
        try:
            return json.loads(self.test_file.read_text())
        except json.JSONDecodeError:
            return {}

    def get_instance_tests(self):
        # type: () -> List[str]
        """Get tests for the current instance."""
        return self.all_tests.get(self.instance_id, [])

    def _validate_test_output(self, stdout, stderr, returncode):
        # type: (str, str, int) -> Tuple[bool, str]
        """Validate test output to detect false positives.

        Returns:
            Tuple of (passed, reason) where reason explains any failure.
        """
        combined_output = stdout + stderr

        # Check for sympy-specific patterns
        if self.framework == "sympy":
            # Sympy outputs "tests finished: X passed" - check for 0 passed
            match = re.search(r"tests finished:\s*(\d+)\s*passed", combined_output)
            if match and int(match.group(1)) == 0:
                return False, "No tests were actually executed (0 passed)"

            # Also check for "no tests ran" type messages
            if "collected 0 items" in combined_output.lower():
                return False, "No tests were collected"

        # Check for pytest patterns
        if "collected 0 items" in combined_output or "no tests ran" in combined_output.lower():
            return False, "No tests were collected or ran"

        # Check for common test framework failure patterns
        if returncode != 0:
            return False, f"Non-zero return code: {returncode}"

        return True, ""

    def run_single_test(self, test_name: str, timeout: int = 300) -> dict:
        """Run a single test and return result."""
        # Convert test name to framework-specific format
        converted_name = convert_test_name_for_framework(test_name, self.framework)

        try:
            result = subprocess.run(
                self.test_cmd + [converted_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
                cwd=os.environ.get("SWE_REPO_DIR", "/testbed")
            )

            passed, reason = self._validate_test_output(
                result.stdout, result.stderr, result.returncode
            )

            return {
                "test": test_name,
                "converted_name": converted_name,
                "passed": passed,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "failure_reason": reason if not passed else "",
            }
        except subprocess.TimeoutExpired:
            return {
                "test": test_name,
                "converted_name": converted_name,
                "passed": False,
                "returncode": -1,
                "error": f"Test timed out after {timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except Exception as e:
            return {
                "test": test_name,
                "converted_name": converted_name,
                "passed": False,
                "returncode": -1,
                "error": str(e),
                "stdout": "",
                "stderr": "",
            }

    def run_tests(self, test_names=None, timeout=300):
        # type: (Optional[List[str]], int) -> List[dict]
        """Run multiple tests and return results."""
        if test_names is None:
            test_names = self.get_instance_tests()

        if not test_names:
            return []

        results = []
        for test_name in test_names:
            result = self.run_single_test(test_name, timeout=timeout)
            results.append(result)

        return results

    def run_all_tests_batch(self, test_names=None, timeout=600):
        # type: (Optional[List[str]], int) -> dict
        """Run all tests in a single invocation (faster)."""
        if test_names is None:
            test_names = self.get_instance_tests()

        if not test_names:
            return {
                "passed": True,
                "total": 0,
                "passed_count": 0,
                "failed_count": 0,
                "tests": [],
                "converted_tests": [],
                "stdout": "",
                "stderr": "No tests found for this instance.",
            }

        # Convert test names to framework-specific format
        converted_names = self.convert_test_names(test_names)
        cmd = self.test_cmd + converted_names

        try:
            # Run all tests in one call
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
                cwd=os.environ.get("SWE_REPO_DIR", "/testbed")
            )

            passed, reason = self._validate_test_output(
                result.stdout, result.stderr, result.returncode
            )

            return {
                "passed": passed,
                "returncode": result.returncode,
                "total": len(test_names),
                "tests": test_names,
                "converted_tests": converted_names,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "failure_reason": reason if not passed else "",
            }
        except subprocess.TimeoutExpired:
            return {
                "passed": False,
                "returncode": -1,
                "total": len(test_names),
                "tests": test_names,
                "converted_tests": converted_names,
                "error": f"Tests timed out after {timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except Exception as e:
            return {
                "passed": False,
                "returncode": -1,
                "total": len(test_names),
                "tests": test_names,
                "converted_tests": converted_names,
                "error": str(e),
                "stdout": "",
                "stderr": "",
            }
