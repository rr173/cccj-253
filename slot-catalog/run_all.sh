#!/bin/sh
# Run the full test suite and the acceptance harness. Re-runnable.
set -e
cd "$(dirname "$0")"
echo "== unit tests =="
python3 -m unittest discover -s tests -t . -v
echo "== acceptance =="
python3 acceptance/run_acceptance.py
echo "== merge example =="
python3 acceptance/merge_example/run_merge_example.py
echo "== sync example =="
python3 acceptance/sync_example/run_sync_example.py
echo "== access example =="
python3 acceptance/access_example/run_access_example.py
