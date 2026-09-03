import secrets

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
