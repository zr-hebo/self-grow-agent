UV ?= uv
CICD_LOG ?= cicd-logs/run_tests.log

.PHONY: test cicd

test:
	$(UV) run pytest -q tests

cicd:
	$(UV) run python -m cicd_case.run_tests --log-file "$(CICD_LOG)"
