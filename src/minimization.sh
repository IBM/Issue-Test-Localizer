#!/usr/bin/env bash
# bash src/minimization.sh <suspicious_json> <result_json> <dataset> <log_dir> <model> [strategy]
# bash src/minimization.sh logs/claude_combined.json results.json princeton-nlp/SWE-bench_Verified logs claude-3-7-sonnet greedy_additional
set -euo pipefail

SUSPICIOUS_JSON=${1:?"Usage: $0 <suspicious_json> <result_json> <dataset> <log_dir> <model> [strategy]"}
RESULT_JSON=${2:?"Missing result_json argument"}
DATASET=${3:?"Missing dataset argument"}
LOG_DIR=${4:?"Missing log_dir argument"}
MODEL=${5:?"Missing model argument"}
STRATEGY=${6:-greedy_additional}

timestamp=$(date +%Y%m%d_%H%M%S)
MODEL_SAFE=$(echo "${MODEL}" | tr '/' '_')
DATASET_SHORT=$(echo "${DATASET}" | awk -F'/' '{print $NF}')
OUT_DIR="${LOG_DIR}/${DATASET}/${MODEL_SAFE}"
mkdir -p "${OUT_DIR}"

# Path conventions used by the pipeline
# All outputs are anchored under LOG_DIR:
#   {LOG_DIR}/model_test_files/...     ← test_file_selection.py output
#   {LOG_DIR}/coverage/...             ← coverage artifacts (.coverage, .coveragerc, eval.sh, etc.)
#   {LOG_DIR}/{DATASET}/{MODEL}/...    ← per-step logs, minimized_tests.json
SELECTED_TESTS_JSON="${LOG_DIR}/model_test_files/${DATASET_SHORT}/${MODEL}/test_file_selection.json"
COV_DIR="${LOG_DIR}/coverage/${DATASET_SHORT}_${MODEL_SAFE}"

echo "START $(date)"
echo "Suspicious JSON: ${SUSPICIOUS_JSON}"
echo "Result JSON: ${RESULT_JSON}"
echo "Dataset: ${DATASET}"
echo "Model: ${MODEL}"
echo "Strategy: ${STRATEGY}"
echo "Coverage dir: ${COV_DIR}"

# Step 1: Test File Selection (LLM-based top-k retrieval)
echo "=== Step 1: Test File Selection ==="
python3 -u src/test_file_selection.py  \
    --suspicious_func_json "${SUSPICIOUS_JSON}" \
    --output_dir_base "${LOG_DIR}" \
    --dataset_name "${DATASET}" \
    --model_name "${MODEL}" |& tee "${OUT_DIR}/test_file_selection_${timestamp}.log"

# Step 2: Coverage Generation (run selected tests in Docker, collect line-level coverage)
# Requires: SWE-bench harness installed, Docker running.
# Skip with SKIP_COVERAGE=1 if coverage data already exists in ${COV_DIR}.
if [[ "${SKIP_COVERAGE:-0}" != "1" ]]; then
    echo "=== Step 2: Coverage Generation ==="
    python3 -u src/generate_cov_batch.py \
        --dataset_name "${DATASET}" \
        --suspicious_funcs_file "${SUSPICIOUS_JSON}" \
        --selected_tests_file "${SELECTED_TESTS_JSON}" \
        --model_name "${MODEL}" \
        --coverage_dir "${COV_DIR}" |& tee "${OUT_DIR}/generate_cov_${timestamp}.log"
else
    echo "=== Step 2: Coverage Generation (SKIPPED via SKIP_COVERAGE=1) ==="
fi

# Step 3: Test Minimization (greedy set-cover)
# Output: ${COV_DIR}/minimized_tests.json
echo "=== Step 3: Test Minimization ==="
python3 -u src/test_minimization.py \
    --coverage_dir "${COV_DIR}" \
    --test_log_dir "${COV_DIR}" \
    --suspicious_json "${SUSPICIOUS_JSON}" \
    --datasetname "${DATASET}" \
    --log_dir "${COV_DIR}" \
    --model_name "${MODEL}" \
    --strategy "${STRATEGY}" |& tee "${OUT_DIR}/test_minimization_${timestamp}.log"

echo ""
echo "=== Output ==="
echo "  Coverage artifacts: ${COV_DIR}/<instance_id>/"
echo "  Minimized tests:   ${COV_DIR}/minimized_tests.json"

# Step 4: Patch Selection (optional, requires Docker + SWE-bench setup)
# Uncomment to run patch validation against reproduction tests:
# echo "=== Step 4: Patch Selection ==="
# python3 -u src/patch_selection_multi.py \
#   --reproduction-tests-json tests/reproduction_test/ \
#   --tests-file "${LOG_DIR}/${DATASET}/coverage_results_${DATASET}_${MODEL_SAFE}/minimized_tests.json" \
#   --repo-path /testbed \
#   --log-dir test_logs \
#   --max-workers 10 \
#   --run-id-prefix test0

echo "END $(date)"