.PHONY: check-privacy check-no-jpeg check-no-resize check-all test lint

check-privacy:
	python scripts/check_no_private_data.py

check-no-jpeg:
	python scripts/check_no_jpeg.py

check-no-resize:
	python scripts/check_no_image_resize.py

check-all: check-privacy check-no-jpeg check-no-resize

test:
	python -m pytest tests/ -x --tb=short

lint:
	ruff check .
