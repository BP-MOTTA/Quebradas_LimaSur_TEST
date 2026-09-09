PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python

.PHONY: setup lint test inventory indeci-live-smoke indeci-live-smoke-emergency indeci-ingest-seeds-live gis features dataset train evaluate report

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

indeci-live-smoke:
	$(VENV_PYTHON) -m quebradas indeci live-smoke \
		--config configs/sources/indeci_cusipata.yaml

indeci-live-smoke-emergency:
	$(VENV_PYTHON) -m quebradas indeci live-smoke \
		--config configs/sources/indeci_emergency_1496.yaml

indeci-ingest-seeds-live:
	@test "$$GITHUB_ACTIONS" != "true" || \
		(echo "INDECI ingestion is disabled in GitHub Actions" >&2; exit 2)
	$(VENV_PYTHON) -m quebradas indeci ingest-seeds \
		--config configs/sources/indeci_cusipata.yaml \
		--seeds configs/sources/indeci_seed_documents.yaml

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
