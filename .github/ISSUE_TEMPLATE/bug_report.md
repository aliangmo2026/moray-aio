---
name: Bug report
about: Something is not working as expected
labels: bug
---

## Description

A clear and concise description of the bug.

## Steps to reproduce

1. ...
2. ...

## Expected behavior

What you expected to happen.

## Actual behavior

What actually happened (logs, HTTP responses, terminal output).

## Environment

- moray-aio version: (`python cli.py health` or check the /health endpoint)
- OS:
- Python version:
- Ollama reachable? (yes/no, model name)
- Storage backend: SQLite (default) / Postgres

## Safety checklist (please confirm)

- [ ] I am running moray-aio in read-only mode against whitelisted log paths
- [ ] The watched log directory is mounted/allowed via MORAY_AIO_READONLY_ROOTS
