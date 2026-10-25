#!/usr/bin/env bash
# moray-aio one-command installer (Linux/macOS).
# Creates .venv, installs dependencies and initializes .env from the example file.
# Usage:  bash install.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "== moray-aio installer =="

# 1) virtual environment
if [ ! -d ".venv" ]; then
  echo "[1/3] creating virtual environment .venv ..."
  python3 -m venv .venv
else
  echo "[1/3] .venv already exists, reusing"
fi

# 2) dependencies
echo "[2/3] installing dependencies ..."
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install -r requirements-dev.txt --quiet

# 3) local config (secrets come from environment variables - never committed)
if [ ! -f ".env" ]; then
  echo "[3/3] initializing .env from .env.example"
  cp .env.example .env
else
  echo "[3/3] .env already exists, keeping it"
fi

echo ""
echo "done. next steps:"
echo "  source .venv/bin/activate"
echo "  make dev            # or: python -m uvicorn app.main:app --port 8020"
echo "  python cli.py health"
