.PHONY: check-privacy test lint

check-privacy:
	python scripts/check_no_private_data.py

test:
	python -m pytest tests/ -x --tb=short

lint:
	ruff check .
