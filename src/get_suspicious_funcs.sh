# bash src/get_suspicious_funcs.sh <dataset> <log_dir> <model>
# bash src/get_suspicious_funcs.sh princeton-nlp/SWE-bench_Verified logs claude-3-7-sonnet

set -euo pipefail

DATASET=${1:?" $0 <dataset> <log_dir> <model>"}
LOG_DIR=${2:?" $0 <dataset> <log_dir> <model>"}
MODEL=${3:?" $0 <dataset> <log_dir> <model>"}

timestamp=$(date +%Y%m%d_%H%M%S)
MODEL_SAFE=$(echo "${MODEL}" | tr '/' '_')
OUT_DIR="${LOG_DIR}/${DATASET}/${MODEL_SAFE}"
mkdir -p "${OUT_DIR}"

echo "START $(date)"
echo "Dataset: ${DATASET}"
echo "Model: ${MODEL}"
echo "Log dir: ${LOG_DIR}"

python3 -u src/testloc.py --model "${MODEL}" \
                        --dataset "${DATASET}" \
                        --log_dir "${LOG_DIR}" |& tee "${OUT_DIR}/suspicious_funcs_${timestamp}.log"

echo "END $(date)"