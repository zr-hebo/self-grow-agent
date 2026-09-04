import secrets
import tempfile
from pathlib import Path

import pytest

from config import Settings, load_settings

MANAGEMENT_KEY = secrets.token_urlsafe(32)
LLM_API_KEY = secrets.token_urlsafe(32)
SHORT_MANAGEMENT_KEY = secrets.token_urlsafe(6)

SETTING_ENV_VARS = (
    "HOST",
    "PORT",
    "MANAGEMENT_API_KEY",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "GENERATED_DIR",
    "METADATA_DB_PATH",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_HANDLER_RESULT_BYTES",
    "HANDLER_TIMEOUT_SECONDS",
    "HANDLER_MEMORY_LIMIT_MB",
    "HANDLER_CPU_LIMIT_SECONDS",
    "MAX_CONCURRENT_HANDLERS",
    "HANDLER_ADMISSION_TIMEOUT_SECONDS",
    "GENERATION_BACKEND",
    "PI_EXECUTABLE",
    "PI_PROVIDER",
    "PI_MODEL",
    "PI_THINKING_LEVEL",
    "PI_TIMEOUT_SECONDS",
    "PI_MAX_EVENT_STREAM_BYTES",
    "PI_MAX_CONCURRENT_RUNS",
    "PI_ADMISSION_TIMEOUT_SECONDS",
    "PI_WORKSPACE_ROOT",
    "PI_PROVIDER_ENV_NAME",
    "PLUGIN_WORKSPACE_ROOT",
    "PLUGIN_ARTIFACT_ROOT",
    "PLUGIN_ALLOWED_DEPENDENCIES",
    "PLUGIN_PROJECT_ENV_ALLOWLIST",
    "PLUGIN_MAX_FILES",
    "PLUGIN_MAX_FILE_BYTES",
    "PLUGIN_MAX_TOTAL_BYTES",
    "PLUGIN_KEEP_FAILED_WORKSPACES",
    "PLUGIN_EXECUTION_BACKEND",
    "PLUGIN_CONTAINER_RUNTIME",
    "PLUGIN_CONTAINER_IMAGE",
    "PLUGIN_PROJECT_CONTAINER_NETWORKS",
)


def test_load_settings_reads_environment(monkeypatch, tmp_path: Path) -> None:
    generated_dir = tmp_path / "runtime-handlers"
    metadata_db_path = tmp_path / "runtime-metadata.sqlite3"
    pi_workspace_root = tmp_path / "pi-workspaces"
    plugin_workspace_root = tmp_path / "external-plugin-workspaces"
    plugin_artifact_root = generated_dir / "plugins"
    values = {
        "HOST": "0.0.0.0",
        "PORT": "9100",
        "MANAGEMENT_API_KEY": MANAGEMENT_KEY,
        "LLM_API_KEY": LLM_API_KEY,
        "LLM_BASE_URL": "https://llm.example.test/v1",
        "LLM_MODEL": "example-model",
        "LLM_TIMEOUT_SECONDS": "12.5",
        "GENERATED_DIR": str(generated_dir),
        "METADATA_DB_PATH": str(metadata_db_path),
        "MAX_REQUEST_BODY_BYTES": "2048",
        "MAX_HANDLER_RESULT_BYTES": "4096",
        "HANDLER_TIMEOUT_SECONDS": "1.5",
        "HANDLER_MEMORY_LIMIT_MB": "128",
        "HANDLER_CPU_LIMIT_SECONDS": "2",
        "MAX_CONCURRENT_HANDLERS": "3",
        "HANDLER_ADMISSION_TIMEOUT_SECONDS": "0.25",
        "GENERATION_BACKEND": "pi",
        "PI_EXECUTABLE": "/opt/pi/bin/pi",
        "PI_PROVIDER": "deepseek",
        "PI_MODEL": "deepseek-v4-pro",
        "PI_THINKING_LEVEL": "high",
        "PI_TIMEOUT_SECONDS": "240",
        "PI_MAX_EVENT_STREAM_BYTES": "33554432",
        "PI_MAX_CONCURRENT_RUNS": "2",
        "PI_ADMISSION_TIMEOUT_SECONDS": "0.75",
        "PI_WORKSPACE_ROOT": str(pi_workspace_root),
        "PI_PROVIDER_ENV_NAME": "DEEPSEEK_API_KEY",
        "PLUGIN_WORKSPACE_ROOT": str(plugin_workspace_root),
        "PLUGIN_ARTIFACT_ROOT": str(plugin_artifact_root),
        "PLUGIN_ALLOWED_DEPENDENCIES": "PyMySQL==1.1.1,httpx==0.28.1",
        "PLUGIN_PROJECT_ENV_ALLOWLIST": "store:MYSQL_USER,store:MYSQL_PASSWORD",
        "PLUGIN_MAX_FILES": "24",
        "PLUGIN_MAX_FILE_BYTES": "131072",
        "PLUGIN_MAX_TOTAL_BYTES": "524288",
        "PLUGIN_KEEP_FAILED_WORKSPACES": "false",
        "PLUGIN_EXECUTION_BACKEND": "container",
        "PLUGIN_CONTAINER_RUNTIME": "/opt/bin/docker",
        "PLUGIN_CONTAINER_IMAGE": "registry.example.test/plugin-runtime:v1",
        "PLUGIN_PROJECT_CONTAINER_NETWORKS": "store:store-private,ops:ops-private",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = load_settings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 9100
    assert settings.management_api_key == MANAGEMENT_KEY
    assert settings.llm_api_key == LLM_API_KEY
    assert settings.llm_base_url == "https://llm.example.test/v1"
    assert settings.llm_model == "example-model"
    assert settings.llm_timeout_seconds == 12.5
    assert settings.generated_dir == generated_dir
    assert settings.metadata_db_path == metadata_db_path
    assert settings.max_request_body_bytes == 2048
    assert settings.max_handler_result_bytes == 4096
    assert settings.handler_timeout_seconds == 1.5
    assert settings.handler_memory_limit_mb == 128
    assert settings.handler_cpu_limit_seconds == 2
    assert settings.max_concurrent_handlers == 3
    assert settings.handler_admission_timeout_seconds == 0.25
    assert settings.generation_backend == "pi"
    assert settings.pi_executable == "/opt/pi/bin/pi"
    assert settings.pi_provider == "deepseek"
    assert settings.pi_model == "deepseek-v4-pro"
    assert settings.pi_thinking_level == "high"
    assert settings.pi_timeout_seconds == 240
    assert settings.pi_max_event_stream_bytes == 33_554_432
    assert settings.pi_max_concurrent_runs == 2
    assert settings.pi_admission_timeout_seconds == 0.75
    assert settings.pi_workspace_root == pi_workspace_root
    assert settings.pi_provider_env_name == "DEEPSEEK_API_KEY"
    assert settings.plugin_workspace_root == plugin_workspace_root
    assert settings.plugin_artifact_root == plugin_artifact_root
    assert settings.plugin_allowed_dependencies == (
        "PyMySQL==1.1.1",
        "httpx==0.28.1",
    )
    assert settings.plugin_project_env_allowlist == (
        "store:MYSQL_USER",
        "store:MYSQL_PASSWORD",
    )
    assert settings.plugin_max_files == 24
    assert settings.plugin_max_file_bytes == 131_072
    assert settings.plugin_max_total_bytes == 524_288
    assert settings.plugin_keep_failed_workspaces is False
    assert settings.plugin_execution_backend == "container"
    assert settings.plugin_container_runtime == "/opt/bin/docker"
    assert settings.plugin_container_image == "registry.example.test/plugin-runtime:v1"
    assert settings.plugin_project_container_networks == (
        "store:store-private",
        "ops:ops-private",
    )


def test_load_settings_does_not_require_llm_api_key(monkeypatch) -> None:
    for name in SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.llm_api_key == ""
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == "deepseek-v4-flash"
    assert settings.management_api_key == ""
    assert settings.host
    assert settings.port > 0
    assert settings.generated_dir == Path(__file__).parents[1] / "generated"
    assert settings.metadata_db_path == settings.generated_dir / "runtime-metadata.sqlite3"
    assert settings.generation_backend == "direct"
    assert settings.pi_executable == "pi"
    assert settings.pi_provider == "deepseek"
    assert settings.pi_model == "deepseek-v4-pro"
    assert settings.pi_thinking_level == "off"
    assert settings.pi_timeout_seconds == 600
    assert settings.pi_max_event_stream_bytes == 67_108_864
    assert settings.pi_max_concurrent_runs == 1
    assert settings.pi_admission_timeout_seconds == 1
    assert settings.pi_workspace_root == settings.generated_dir / "pi-workspaces"
    assert settings.pi_provider_env_name == "DEEPSEEK_API_KEY"
    assert settings.plugin_workspace_root == (
        Path(tempfile.gettempdir()) / "self-grow-agent-workspaces"
    )
    assert settings.plugin_artifact_root == settings.generated_dir / "plugins"
    assert settings.plugin_allowed_dependencies == ()
    assert settings.plugin_project_env_allowlist == ()
    assert settings.plugin_max_files == 32
    assert settings.plugin_max_file_bytes == 262_144
    assert settings.plugin_max_total_bytes == 1_048_576
    assert settings.plugin_keep_failed_workspaces is True
    assert settings.plugin_execution_backend == "process"
    assert settings.plugin_container_runtime == "docker"
    assert settings.plugin_container_image == "self-grow-agent-plugin-runtime:latest"
    assert settings.plugin_project_container_networks == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", 0),
        ("management_api_key", SHORT_MANAGEMENT_KEY),
        ("llm_timeout_seconds", 0),
        ("max_request_body_bytes", 0),
        ("max_handler_result_bytes", 0),
        ("handler_timeout_seconds", 0),
        ("handler_memory_limit_mb", 0),
        ("handler_cpu_limit_seconds", 0),
        ("max_concurrent_handlers", 0),
        ("handler_admission_timeout_seconds", 0),
        ("generation_backend", "unknown"),
        ("pi_executable", ""),
        ("pi_provider", " "),
        ("pi_model", ""),
        ("pi_thinking_level", "verbose"),
        ("pi_timeout_seconds", 0),
        ("pi_max_event_stream_bytes", 0),
        ("pi_max_concurrent_runs", 0),
        ("pi_admission_timeout_seconds", 0),
        ("pi_provider_env_name", "NOT-AN-ENV-NAME"),
        ("pi_provider_env_name", "PATH"),
        ("pi_provider_env_name", "TMPDIR"),
        ("pi_provider_env_name", "NODE_OPTIONS"),
        ("plugin_max_files", 0),
        ("plugin_max_file_bytes", 0),
        ("plugin_max_total_bytes", 0),
        ("plugin_project_env_allowlist", ("store:MANAGEMENT_API_KEY",)),
        ("plugin_project_env_allowlist", ("Invalid:MYSQL_PASSWORD",)),
        ("plugin_project_env_allowlist", ("store:MYSQL_USER", "store:MYSQL_USER")),
        ("plugin_execution_backend", "unknown"),
        ("plugin_container_runtime", ""),
        ("plugin_container_image", "bad image"),
        ("plugin_project_container_networks", ("Invalid:private",)),
        ("plugin_project_container_networks", ("store:private", "store:other")),
    ],
)
def test_settings_reject_unsafe_values(field: str, value: object) -> None:
    values = {
        "host": "127.0.0.1",
        "port": 8000,
        "management_api_key": MANAGEMENT_KEY,
        "llm_api_key": "",
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "test-model",
        "llm_timeout_seconds": 30.0,
        "generated_dir": Path("generated"),
        "metadata_db_path": Path("generated/runtime-metadata.sqlite3"),
        "max_request_body_bytes": 1_048_576,
        "max_handler_result_bytes": 1_048_576,
        "handler_timeout_seconds": 2.0,
        "handler_memory_limit_mb": 256,
        "handler_cpu_limit_seconds": 1,
        "max_concurrent_handlers": 4,
        "handler_admission_timeout_seconds": 0.1,
        "generation_backend": "direct",
        "pi_executable": "pi",
        "pi_provider": "deepseek",
        "pi_model": "deepseek-v4-pro",
        "pi_thinking_level": "off",
        "pi_timeout_seconds": 600.0,
        "pi_max_event_stream_bytes": 67_108_864,
        "pi_max_concurrent_runs": 1,
        "pi_admission_timeout_seconds": 1.0,
        "pi_workspace_root": Path("generated/pi-workspaces"),
        "pi_provider_env_name": "DEEPSEEK_API_KEY",
        "plugin_workspace_root": Path("/tmp/self-grow-agent-workspaces"),
        "plugin_artifact_root": Path("generated/plugins"),
        "plugin_allowed_dependencies": (),
        "plugin_project_env_allowlist": (),
        "plugin_max_files": 32,
        "plugin_max_file_bytes": 262_144,
        "plugin_max_total_bytes": 1_048_576,
        "plugin_keep_failed_workspaces": True,
        "plugin_execution_backend": "process",
        "plugin_container_runtime": "docker",
        "plugin_container_image": "self-grow-agent-plugin-runtime:latest",
        "plugin_project_container_networks": (),
    }
    values[field] = value

    with pytest.raises(ValueError):
        Settings(**values)  # type: ignore[arg-type]
