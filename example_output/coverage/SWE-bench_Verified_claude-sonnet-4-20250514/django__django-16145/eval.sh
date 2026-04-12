#!/bin/bash
set -uxo pipefail
source /opt/miniconda3/bin/activate
conda activate testbed
cd /testbed
git config --global --add safe.directory /testbed
cd /testbed
git status
git show
git diff 93d4c9ea1de24eb391cb2b3561b6703fd46374df
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .

# === TestLoc: Coverage Generation ===
echo 'Current Git Commit:'
git rev-parse HEAD

python -m pip install pytest pytest-cov coverage

git diff
# Configure .coveragerc for per-test dynamic context tracking
echo "[run]" > .coveragerc
echo "dynamic_context = test_function" >> .coveragerc
echo "" >> .coveragerc
echo "[report]" >> .coveragerc
echo "show_missing = True" >> .coveragerc
echo "ignore_errors = True" >> .coveragerc
echo "omit =" >> .coveragerc
echo "    /testbed/generated/*" >> .coveragerc

start_time=$(date +%s)
echo "Start time: $(date -d @$start_time)"
coverage run ./tests/runtests.py servers.tests servers.test_basehttp servers.test_liveserverthread builtin_server.tests admin_scripts.tests utils_tests.test_autoreload staticfiles_tests.test_liveserver staticfiles_tests.test_handlers test_utils.tests runtests --verbosity 2 --settings=test_sqlite --parallel 1
end_time=$(date +%s)
echo "End time: $(date -d @$end_time)"
coverage report -m

chmod 777 /testbed/.coverage
chmod 777 /testbed/.coveragerc
