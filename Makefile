PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: setup lint test inventory gis features dataset train evaluate report

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"

lint:
	$(VENV_PYTHON) -m ruff check --no-cache .

test:
	$(VENV_PYTHON) -m pytest

inventory:
	$(VENV_PYTHON) -m quebradas_limaeste.inventory.cli inventory \
		--source tests/fixtures/synthetic_indeci_page.html \
		--source-name "Synthetic COEN fixture" \
		--source-url "https://coen.example.test/reportes" \
		--output-dir outputs/inventory/synthetic

gis:
	@echo "GIS pipeline is not implemented in this phase."

features:
	@echo "Feature pipeline is not implemented in this phase."

dataset:
	@echo "Dataset pipeline is not implemented in this phase."

train:
	@echo "Training pipeline is not implemented in this phase."

evaluate:
	@echo "Evaluation pipeline is not implemented in this phase."

report:
	@echo "Report pipeline is not implemented in this phase."
