#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p logs state
exec .venv/bin/python admin.py --config config.yaml >> logs/admin.log 2>&1
