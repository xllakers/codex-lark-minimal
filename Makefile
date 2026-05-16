PYTHON ?= python3
VENV ?= .venv

.PHONY: setup test doctor

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -e .

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

doctor:
	PYTHONPATH=src $(PYTHON) -m codex_lark_minimal.cli doctor
