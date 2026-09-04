UV ?= uv
CICD_LOG ?= cicd-logs/run_tests.log

.PHONY: test cicd cicd-infra plugin-runtime-image

test:
	$(UV) run pytest -q tests

cicd:
	$(UV) run python -m cicd_case.run_tests --log-file "$(CICD_LOG)"

plugin-runtime-image:
	docker build -f docker/plugin-runtime.Dockerfile \
		-t self-grow-agent-plugin-runtime:cicd .

cicd-infra: plugin-runtime-image
	RUN_DOCKER_CICD=true $(UV) run python -m cicd_case.run_tests \
		--log-file "$(CICD_LOG)" container mysql
