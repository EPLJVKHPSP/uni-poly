.PHONY: test test-unit test-integration test-slow coverage

test:
	python3 -m pytest tests/ -v --tb=short

test-unit:
	python3 -m pytest tests/ -m unit -v --tb=short

test-integration:
	python3 -m pytest tests/ -m integration -v --tb=short

test-slow:
	python3 -m pytest tests/ -m slow -v --tb=short

coverage:
	python3 -m pytest tests/ --cov --cov-report=term-missing --cov-report=html
