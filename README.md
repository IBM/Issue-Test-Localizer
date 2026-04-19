# TestLoc

This is the artifact for paper *Can Old Tests do New Tricks for Resolving SWE Issues?* accepted at FSE 2026.

## Artifact Badges

We apply for the **Functional**, **Reusable**, and **Available** badges.

- **Functional**: Reviewers can run the complete TestLoc pipeline end-to-end on a demo of SWE-bench instance (e.g., `django__django-16145`) in approximately 3-5 minutes by following the Quick Start instructions below (this does not include swebench image building time). The pipeline produces all expected outputs: suspicious functions, selected test files, coverage artifacts, and the final `minimized_tests.json`. An example output is provided in `example_output/`.
- **Reusable**: We support multiple LLMs and works on both SWE-bench Verified and Lite datasets. The minimized regression tests can be integrated into any coding agent framework through the provided tool bundle (`tools/regression_tests/`), with SWE-agent as a documented example and instructions for adapting to other agents. The code includes multiple parameters (minimization strategy, top-k test files, parallel workers) to support different experimental setups.
- **Available**: The artifact is publicly available on GitHub at https://github.com/IBM/Issue-Test-Localizer. Pre-computed results for all experiments in the paper are included in the repository (`tests/`, `results_logs/`), with additional detailed logs available via Google Drive.

## Overview

TestLoc takes a GitHub issue and its codebase as input, and produces a minimized set of regression tests that cover the buggy code. The pipeline has 4 steps:

1. **Suspicious Function Localization** (`testloc.py`): An LLM identifies the most relevant source files and functions related to the issue description.
2. **Test File Retrieval** (`test_file_selection.py`): The LLM selects the top-k (default 10) test files most likely to exercise the suspicious functions.
3. **Coverage Generation** (`generate_cov_batch.py`): The selected tests are executed inside SWE-bench Docker containers with `coverage run` and dynamic contexts, producing per-line coverage data that maps each source line to the test(s) that exercise it.
4. **Test Minimization** (`test_minimization.py`): Greedy algorithm select the minimal set of tests that covers all lines of the suspicious functions, with LLM-based tie-breaking when multiple tests have equal coverage.

The final output is `minimized_tests.json`: a small set of regression tests reduced from thousands in the original test suite.

## Quick Start: Run One Instance

Since running the full pipeline on an entire dataset (500 instances for SWE-bench Verified) can take approximately **two days**, here we provide instructions to demo the complete pipeline on a single instance (e.g., `django__django-16145`). Reviewers can follow these exact commands to reproduce a complete run in about **3-5 minutes**.

### Prerequisites

- Python 3.10+
- Docker
- An Anthropic API key (or OpenAI API key)

### 1. Setup

```bash
git clone https://github.com/IBM/Issue-Test-Localizer.git
cd Issue-Test-Localizer

python3 -m venv testloc_venv
source testloc_venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=your_anthropic_api_key
```

### 2. Run the Full Pipeline

```bash
python3 src/run_pipeline.py \
    --instance_id django__django-16145 \
    --model claude-sonnet-4-20250514 \
    --dataset princeton-nlp/SWE-bench_Verified \
    --log_dir output
```

This single command runs all 4 steps sequentially. It takes approximately **3-5 minutes** (most time is spent on Docker container setup and coverage collection in Step 3).

The pipeline script supports skip flags for incremental runs:
- `--skip_step1`: skip Step 1 if `suspicious_funcs.json` already exists
- `--skip_step2`: skip Step 2 if `test_file_selection.json` already exists
- `--skip_coverage`: skip Step 3 if coverage data already exists

### 3. Expected Output

An example output from running on `django__django-16145` is provided in `example_output/`. After the pipeline completes, you will see:

```
output/
  suspicious_funcs.json                             Step 1 result: suspicious functions per instance

  princeton-nlp/SWE-bench_Verified/                 Step 1 logs
    <model>/
      inspector_[instance_id].log                     detailed localization log
      <model>_results/
        all_results_..._[instance_id].json            full localization output

  model_test_files/SWE-bench_Verified/              Step 2 results
    <model>/
      test_file_selection.json                        selected test files (top-10)
      model_selected.json                             full LLM prompt + response
      confirmed_suspicious_funcs.json                 confirmed functions

  coverage/SWE-bench_Verified_<model>/              Steps 3+4 results
    [instance_id]/                                    per-instance coverage artifacts
      [instance_id]_coverage                            .coverage SQLite DB
      .coveragerc                                       coverage config used
      eval.sh                                           exact script that ran in Docker
      test_output.txt                                   full container stdout
      instance.log                                      coverage generation log
    minimization_logs/
      [instance_id].log                               minimization decision log
    minimized_tests.json                            FINAL OUTPUT
```

The **final output** is `minimized_tests.json`:

```json
{
    "[instance_id]": [
        "test_module1.TestClass.test_method1",
        "test_module2.TestClass.test_method2",
        ...
    ]
}
```
Note that due to LLM nondeterminism, running the pipeline multiple times may produce slightly different results.

### Verifying the Coverage Data

You can inspect the `.coverage` file to confirm it contains per-test dynamic contexts:

```python
import coverage
c = coverage.Coverage(data_file="output/coverage/.../django__django-16145/django__django-16145_coverage")
c.load()
data = c.get_data()
print(f"Measured files: {len(data.measured_files())}")

target = "/testbed/django/core/management/commands/runserver.py"
contexts = data.contexts_by_lineno(target)
for lineno, tests in sorted(contexts.items()):
    tests = [t for t in tests if t]
    if tests:
        print(f"  Line {lineno}: {tests}")
```

This shows which test functions exercise which lines of the suspicious file, which is the basis for the greedy minimization in Step 4.


## Running All Instances

To run the full pipeline on all 500 SWE-bench Verified instances (or 300 Lite instances):

```bash
python3 src/run_pipeline.py \
    --model claude-sonnet-4-20250514 \
    --dataset princeton-nlp/SWE-bench_Verified \
    --log_dir output \
    --max_workers 4
```

Or use the shell wrappers for more control:

```bash
# Step 1: Suspicious function localization (all instances)
bash src/get_suspicious_funcs.sh princeton-nlp/SWE-bench_Verified output claude-sonnet-4-20250514

# Steps 2-4: Test selection, coverage, minimization
bash src/minimization.sh \
    output/suspicious_funcs.json \
    output/minimized_tests.json \
    princeton-nlp/SWE-bench_Verified \
    output \
    claude-sonnet-4-20250514 \
    greedy_additional
```


## Reusing TestLoc in Code Agents

After TestLoc produces `minimized_tests.json`, the minimized regression tests can be integrated into coding agents as a tool that agents can call during issue resolution. This enables the agent to run regression tests before and after making changes, ensuring no existing functionality is broken.

Below we use [SWE-agent](https://github.com/princeton-nlp/SWE-agent) as an example. To reuse in other agents, follow similar instructions: provide the `minimized_tests.json` to the agent environment, and add `run_tests` / `list_tests` tool commands using the tool bundle and system prompt provided here.

### Tool Bundle

The `tools/regression_tests/` directory contains a ready-to-use tool bundle that can be plugged into any agent framework:

```
tools/
  regression_tests/             # Tool bundle
    config.yaml                 #   Tool definitions (run_tests, list_tests)
    bin/run_tests               #   Runs regression tests for the current instance
    bin/list_tests              #   Lists available regression tests
    lib/test_runner.py          #   Core test runner with per-framework support
  swe_agent_config.yaml         # SWE-agent config with testing workflow instructions
```

The test runner automatically detects the correct test framework based on the specific project.

### Agent Tool Commands

The tool bundle provides two commands for the agent:

- **`run_tests`**: Run related regression tests for the current instance. Returns pass/fail status and detailed output. Can also accept specific test names: `run_tests test1 test2`.
- **`list_tests`**: List available related regression tests for the current instance.

### Adapting for Other Agents

To integrate TestLoc's minimized tests into a different agent framework:

1. **Provide the test data**: Make `minimized_tests.json` available inside the agent's environment (e.g., copy to the Docker container at `/root/regression_tests.json`).
2. **Set environment variables**: `SWE_INSTANCE_ID` (current instance) and `SWE_REPO_DIR` (repo path, typically `/testbed`).
3. **Register the tools**: Add `run_tests` and `list_tests` as callable tools. The scripts in `tools/regression_tests/bin/` can be invoked directly: they read from the environment variables and JSON file set up in steps 1-2.
4. **Update the system prompt**: Instruct the agent to run `run_tests` before and after making changes. See `tools/swe_agent_config.yaml` for the exact prompt wording used in our experiments.


## Repository Structure

```
testloc/
|-- src/
|   |-- run_pipeline.py           # End-to-end pipeline runner (recommended entry point)
|   |-- testloc.py                
|   |-- localization.py           # Step 1 Inspector for suspicious functions, call graph, LLM prompting
|   |-- test_file_selection.py    # Step 2: LLM test file retrieval (top-k)
|   |-- generate_cov_batch.py     # Step 3: coverage generation
|   |-- test_minimization.py      # Step 4: Greedy alg.
|   |-- get_suspicious_funcs.sh   # bash script for step 1
|   |-- minimization.sh           # bash script for steps 2-4
|   |-- call_graph_generation.py  # Tree-sitter call graph construction + traversal
|   |-- model_config.py           
|   |-- prompts.py                # prompt templates
|   |-- query_cov.py              # Coverage data utility
|   |-- utils.py                  
|   |-- locate_tests.py           
|   |-- bm25.py                   # BM25 baseline
|
|-- tools/
|   |-- regression_tests/         # Tool bundle for running minimized tests in agents
|   |   |-- config.yaml           #   Tool definitions (run_tests, list_tests)
|   |   |-- bin/run_tests         #   Run regression tests for current instance
|   |   |-- bin/list_tests        
|   |   |-- lib/test_runner.py    # test runner (adjust different test cmds for different projects)
|   |-- swe_agent_config.yaml     # SWE-agent config with testing workflow and prompts
|
|-- tests/                        # regression tests and reproduction tests
|   |-- regression_tests/         
|   |-- reproduction_test/       
|
|-- results_logs/
|   |-- addtional_results/        # Evaluation results from other agents
|   |   |-- sweagent/             #   SWE-agent baseline 
|   |   |-- trae_agent/           #   Trae agent baseline 
|   |-- patch_ranking_logs/       # Patch ranking and selection logs for agentless
|       |-- all_logs/             
|       |-- all_patches/          
|       |-- evaluation_logs/      
|       |-- ranks/                
|-- requirements.txt             
```

## Minimization Algorithms

TestLoc implements two greedy set-cover variants:

### Greedy-Additional (Algorithm 1, default)

Each iteration re-ranks tests by how many *remaining uncovered* lines they cover:

```
while uncovered lines remain:
    select test(s) covering the most uncovered lines
    if tie (>3 candidates): use LLM to break tie
    remove covered lines from uncovered set
```

### Greedy-Total (Algorithm 2)

Tests are ranked once by *total* coverage across all suspicious function lines:

```
sort tests by total coverage (descending, fixed)
while tests remain and no_improve < 3:
    select test with highest total coverage
    if cumulative coverage improves: reset no_improve counter
    else: increment no_improve
```

Select via `--strategy greedy_additional` or `--strategy greedy_total`.

## Supported Models

| Model | Environment Variable | Example Model Name |
|-------|---------------------|--------------------|
| Claude (Anthropic) | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` |
| GPT-4o (OpenAI) | `OPENAI_API_KEY` | `gpt-4o` |
| Custom endpoint | `OPENAI_API_KEY` + `MODEL_SERVING_URL` | Any model name |

## Supported Datasets

- `princeton-nlp/SWE-bench_Verified`
- `princeton-nlp/SWE-bench_Lite`

## Results and Logs

Pre-computed results from the paper's experiments are included in the repository:

- **Regression tests**: `tests/regression_tests/`: Minimized regression test results for Claude and GPT-4o on both SWE-bench Verified and Lite datasets.
- **Reproduction tests from Otter**: `tests/reproduction_test/`: Reproduction test generation results across different strategies (full, none, patchLoc, planner, regression, testLoc), organized by model and dataset.
- **Agent results**: `results_logs/addtional_results/`: Evaluation results from SWE-agent and Trae agent baselines, including generated patches, execution logs, and evaluation reports.
- **Agentless results**: `results_logs/patch_ranking_logs/`: Patch ranking and selection logs, including generated patches, test execution results, and final rankings per model/strategy.
- **Detailed logs**: [Google Drive](https://drive.google.com/drive/folders/1IQA67tSZUSx1FqG_IinijBWx1TpbNVIW?usp=share_link): Full execution logs and intermediate data for all experiments.
