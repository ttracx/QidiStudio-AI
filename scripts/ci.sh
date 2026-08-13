#!/usr/bin/env bash
# CI local-green gate — runs the real test suite.
# Must exit 0 for a push to be allowed.
set -euo pipefail

echo "=== QidiStudio-AI CI ==="

# AI sidecar tests
echo "--- AI sidecar ---"
cd ai_pipeline
python -m pip install --requirement requirements-ci.txt -q
python -m compileall -q server.py test_server_api.py test_pipeline.py
python -m unittest -v test_server_api.py
python test_pipeline.py
cd ..

echo "=== CI GREEN ==="
