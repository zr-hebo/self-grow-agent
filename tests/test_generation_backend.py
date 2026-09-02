from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path

from config import Settings
from self_grow_agent import api as api_module


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        management_api_key=secrets.token_urlsafe(32),
        llm_api_key=secrets.token_urlsafe(32),
        llm_base_url="https://llm.example.test/v1",
        llm_model="direct-model",
        llm_timeout_seconds=13,
        generated_dir=tmp_path / "generated",
        metadata_db_path=tmp_path / "metadata.sqlite3",
        pi_executable="/opt/pi/bin/pi",
        pi_provider="deepseek",
        pi_model="deepseek-v4-pro",
        pi_timeout_seconds=241,
        pi_max_concurrent_runs=2,
        pi_admission_timeout_seconds=0.75,
        pi_workspace_root=tmp_path / "pi-workspaces",
        pi_provider_env_name="DEEPSEEK_API_KEY",
    )


def test_build_generator_keeps_direct_backend_as_default(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class RecordingDirectGenerator:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_module, "OpenAIFeatureGenerator", RecordingDirectGenerator)
    settings = _settings(tmp_path)

    result = api_module._build_generator(settings)

    assert isinstance(result, RecordingDirectGenerator)
    assert captured == {
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "timeout_seconds": settings.llm_timeout_seconds,
    }


def test_build_generator_wires_pi_without_putting_key_in_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class RecordingRpcClient:
        def __init__(self, **kwargs: object) -> None:
            captured["rpc_client"] = self
            captured["rpc_options"] = kwargs

    class RecordingPiGenerator:
        def __init__(self, **kwargs: object) -> None:
            captured["generator"] = kwargs

    monkeypatch.setattr(api_module, "PiRpcClient", RecordingRpcClient)
    monkeypatch.setattr(api_module, "PiFeatureGenerator", RecordingPiGenerator)
    settings = replace(_settings(tmp_path), generation_backend="pi")

    result = api_module._build_generator(settings)

    assert isinstance(result, RecordingPiGenerator)
    rpc_options = captured["rpc_options"]
    assert isinstance(rpc_options, dict)
    assert rpc_options == {
        "command": (settings.pi_executable,),
        "provider": settings.pi_provider,
        "model": settings.pi_model,
        "api_key": settings.llm_api_key,
        "provider_env_name": settings.pi_provider_env_name,
        "timeout_seconds": settings.pi_timeout_seconds,
        "workspace_root": settings.pi_workspace_root,
    }
    command = rpc_options["command"]
    assert isinstance(command, tuple)
    assert settings.llm_api_key not in command
    assert captured["generator"] == {
        "rpc_client": captured["rpc_client"],
        "max_concurrent_runs": settings.pi_max_concurrent_runs,
        "admission_timeout_seconds": settings.pi_admission_timeout_seconds,
    }


def test_build_generator_returns_none_without_llm_credential(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        generation_backend="pi",
        llm_api_key="",
    )

    assert api_module._build_generator(settings) is None
