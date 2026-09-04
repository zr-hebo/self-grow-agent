import secrets
from dataclasses import replace

import main


def test_run_uses_host_and_port_loaded_from_config(monkeypatch) -> None:
    call: dict[str, object] = {}

    def fake_run(app, *, host: str, port: int) -> None:
        call.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(main.uvicorn, "run", fake_run)

    main.run()

    assert call == {
        "app": main.app,
        "host": main.settings.host,
        "port": main.settings.port,
    }


def test_management_key_log_context_is_redacted_and_stable() -> None:
    management_key = secrets.token_urlsafe(32)

    context = main._management_key_log_context(management_key)

    assert management_key not in context
    assert context.startswith("configured (fingerprint=sha256:")
    assert context.endswith(f"suffix=***{management_key[-4:]})")
    assert main._management_key_log_context("") == "not configured"


def test_runtime_configuration_log_context_has_effective_limits_without_key() -> None:
    secret = secrets.token_urlsafe(32)
    runtime_settings = replace(
        main.settings,
        llm_api_key=secret,
        generation_backend="pi",
        pi_provider="deepseek",
        pi_model="deepseek-v4-pro",
        pi_timeout_seconds=321.0,
        pi_max_event_stream_bytes=33_554_432,
        pi_max_concurrent_runs=2,
    )

    context = main._runtime_configuration_log_context(runtime_settings)

    assert "generation_backend='pi'" in context
    assert "generation_key_configured=True" in context
    assert "pi_provider='deepseek'" in context
    assert "pi_model='deepseek-v4-pro'" in context
    assert "pi_timeout_seconds=321.000" in context
    assert "pi_max_event_stream_bytes=33554432" in context
    assert "pi_max_concurrent_runs=2" in context
    assert "handler_timeout_seconds=" in context
    assert "max_concurrent_handlers=" in context
    assert secret not in context
