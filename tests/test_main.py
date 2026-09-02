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
