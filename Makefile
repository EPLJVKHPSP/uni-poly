.PHONY: test test-unit test-integration test-slow coverage

test:
	pytest tests/ -v --tb=short

test-unit:
	pytest tests/ -m unit -v --tb=short

test-integration:
	pytest tests/ -m integration -v --tb=short

test-slow:
	pytest tests/ -m slow -v --tb=short

coverage:
	pytest tests/ --cov --cov-report=term-missing --cov-report=html
