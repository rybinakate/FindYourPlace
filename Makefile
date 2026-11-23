# ============================================================
# Makefile for the Hello GIS project
# Usage: make <target> (for example: make run, make check)
# ============================================================

# --- VARIABLES ---
PYTHON := uv run python
SRC := src
PACKAGE := $(SRC)/hello_gis
DATA_RAW := data/raw
DATA_PROCESSED := data/processed
OUTPUTS := outputs

# All Python files in the hello_gis/
SOURCES := $(wildcard $(PACKAGE)/*.py)
CHECK_PATHS := src scripts tests generate_readme.py
DEV := uv run --locked --group dev

# --- PHONY TARGETS ---
.PHONY: all help install check lint format-check type-check format run test clean data viz network

# --- DEFAULT TARGET (runs when invoking `make`) ---
all: check run
	@echo "✅ All tasks complete!"

# --- HELP ---
help:
	@echo "Available commands:"
	@echo "  make help          - Show this help"
	@echo "  make all           - Check the code and run the application (default)"
	@echo "  make install       - Install dependencies"
	@echo "  make check         - Run lint, format checks, and offline unit tests"
	@echo "  make lint          - Lint maintained Python files with Ruff"
	@echo "  make format-check  - Check Python formatting without editing files"
	@echo "  make type-check    - Run mypy on the application package"
	@echo "  make format        - Format the code"
	@echo "  make run           - Run the main script (main.py)"
	@echo "  make data          - Download OSM data"
	@echo "  make viz           - Create visualizations"
	@echo "  make test          - Run local tests"
	@echo "  make clean         - Clear caches, outputs, and temporary files"

# --- DEPENDENCY INSTALLATION ---
install:
	uv sync --locked
	@echo "✅ Dependencies installed"

# --- DATA DOWNLOAD ---
data:
	@echo "📥 Downloading OSM data..."
	$(PYTHON) -m hello_gis.fetch_data
	@echo "✅ Data ready!"

# --- VISUALIZATION ---
viz:
	@echo "🎨 Creating visualizations..."
	$(PYTHON) -m hello_gis.visualize
	@echo "✅ Visualizations ready!"

	# --- NETWORK ANALYSIS ---
network:
	@echo "🕸️  Running park accessibility network analysis..."
	$(PYTHON) -m hello_gis.network_analysis
	@echo "✅ Analysis complete!"

# --- CODE CHECKS ---
check: lint format-check test

lint:
	$(DEV) ruff check $(CHECK_PATHS)

format-check:
	$(DEV) ruff format --check $(CHECK_PATHS)

type-check:
	$(DEV) mypy $(PACKAGE)/

# --- CODE FORMATTING ---
format:
	$(DEV) ruff format $(CHECK_PATHS)
	$(DEV) ruff check --fix $(CHECK_PATHS)
	@echo "✅ Code formatted!"

# --- RUN THE APPLICATION ---
run: $(SOURCES)
	@echo "🚀 Starting the application..."
	$(PYTHON) -m hello_gis.main

# --- TESTS ---
test:
	PROJ_NETWORK=OFF MPLBACKEND=Agg $(DEV) python -m pytest tests

# --- CLEANUP ---
clean:
	@echo "🧹 Removing caches and temporary files..."
	rm -rf .mypy_cache .ruff_cache __pycache__
	rm -rf .pytest_cache
	rm -rf $(PACKAGE)/__pycache__
	rm -rf cache/
	rm -rf $(OUTPUTS)/*
	@echo "✅ Cleanup complete!"
