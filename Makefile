# moray-aio standard entry points.
# Windows users: run through Git Bash/WSL, or use the equivalent commands below.

# PY selects the interpreter: .venv when present, plain python otherwise.
PY := $(shell if [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"; elif [ -x ".venv/Scripts/python.exe" ]; then echo ".venv/Scripts/python.exe"; else echo "python3"; fi)

.PHONY: dev test lint bench watch run health build clean help

# Start the API service (dev mode)
dev:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8020

# One full analysis straight in the terminal (no HTTP)
run:
	$(PY) cli.py run --trigger recent_errors --auto-approve

# Event-driven watch mode (Ctrl+C to stop)
watch:
	$(PY) cli.py watch

# Check the running service
health:
	$(PY) cli.py health

# Full test suite
test:
	$(PY) -m pytest -q

# Ruff lint (0 warnings required by CI)
lint:
	$(PY) -m ruff check .

# Performance benchmark (100MB log; writes docs/benchmark.md)
bench:
	$(PY) scripts/benchmark.py

# Build the Docker image (skipped gracefully when Docker is unavailable)
build:
	-docker build -t moray-aio:local .

# Remove generated artifacts (caches, benchmark data); keeps .venv and .env
clean:
	$(PY) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in [*pathlib.Path('.').glob('**/__pycache__'), pathlib.Path('.pytest_cache'), pathlib.Path('data/benchmark')]]"

help:
	@echo "interpreter: $(PY)"
	@echo "targets: dev run watch health test lint bench build clean"
