SYSTEM_PYTHON ?= python3
PYTHON ?= .venv/bin/python
export PYTHONPATH := src

.PHONY: setup lint test catalog-offline catalog

.venv/bin/python:
	$(SYSTEM_PYTHON) -m venv .venv

setup: .venv/bin/python
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest

catalog-offline:
	$(PYTHON) -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml --offline-fixtures

catalog:
	$(PYTHON) -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml
