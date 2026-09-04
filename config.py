"""Application configuration loaded from environment variables."""

import re
from dataclasses import dataclass
from os import environ
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_PI_PROVIDER_ENV_NAME_PATTERN = re.compile(
    r"(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|TOKEN)"
)
_RESERVED_PI_PROVIDER_ENV_NAMES = frozenset(
    {
        "HOME",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "PI_CODING_AGENT_DIR",
        "PI_SKIP_VERSION_CHECK",
        "PI_TELEMETRY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings for the service and its LLM client."""

    host: str
    port: int
    management_api_key: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    generated_dir: Path
    metadata_db_path: Path
    max_request_body_bytes: int = 1_048_576
    max_handler_result_bytes: int = 1_048_576
    handler_timeout_seconds: float = 2.0
    handler_memory_limit_mb: int = 256
    handler_cpu_limit_seconds: int = 1
    max_concurrent_handlers: int = 4
    handler_admission_timeout_seconds: float = 0.1
    generation_backend: str = "direct"
    pi_executable: str = "pi"
    pi_provider: str = "deepseek"
    pi_model: str = "deepseek-v4-pro"
    pi_timeout_seconds: float = 600.0
    pi_max_event_stream_bytes: int = 67_108_864
    pi_max_concurrent_runs: int = 1
    pi_admission_timeout_seconds: float = 1.0
    pi_workspace_root: Path = Path("generated/pi-workspaces")
    pi_provider_env_name: str = "DEEPSEEK_API_KEY"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("HOST must not be empty")
        if not 1 <= self.port <= 65_535:
            raise ValueError("PORT must be between 1 and 65535")
        if self.management_api_key and len(self.management_api_key) < 16:
            raise ValueError("MANAGEMENT_API_KEY must be empty or at least 16 characters")
        if self.generation_backend not in {"direct", "pi"}:
            raise ValueError("GENERATION_BACKEND must be 'direct' or 'pi'")
        required_pi_text = {
            "PI_EXECUTABLE": self.pi_executable,
            "PI_PROVIDER": self.pi_provider,
            "PI_MODEL": self.pi_model,
        }
        for name, value in required_pi_text.items():
            if not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty string without outer whitespace")
        if (
            _PI_PROVIDER_ENV_NAME_PATTERN.fullmatch(self.pi_provider_env_name) is None
            or self.pi_provider_env_name in _RESERVED_PI_PROVIDER_ENV_NAMES
        ):
            raise ValueError(
                "PI_PROVIDER_ENV_NAME must end with API_KEY or TOKEN"
            )
        positive_values = {
            "LLM_TIMEOUT_SECONDS": self.llm_timeout_seconds,
            "MAX_REQUEST_BODY_BYTES": self.max_request_body_bytes,
            "MAX_HANDLER_RESULT_BYTES": self.max_handler_result_bytes,
            "HANDLER_TIMEOUT_SECONDS": self.handler_timeout_seconds,
            "HANDLER_MEMORY_LIMIT_MB": self.handler_memory_limit_mb,
            "HANDLER_CPU_LIMIT_SECONDS": self.handler_cpu_limit_seconds,
            "MAX_CONCURRENT_HANDLERS": self.max_concurrent_handlers,
            "HANDLER_ADMISSION_TIMEOUT_SECONDS": self.handler_admission_timeout_seconds,
            "PI_TIMEOUT_SECONDS": self.pi_timeout_seconds,
            "PI_MAX_EVENT_STREAM_BYTES": self.pi_max_event_stream_bytes,
            "PI_MAX_CONCURRENT_RUNS": self.pi_max_concurrent_runs,
            "PI_ADMISSION_TIMEOUT_SECONDS": self.pi_admission_timeout_seconds,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")


def load_settings() -> Settings:
    """Build a fresh settings snapshot from the current process environment."""

    generated_dir = Path(environ.get("GENERATED_DIR", str(_PROJECT_ROOT / "generated")))
    pi_workspace_root = Path(
        environ.get("PI_WORKSPACE_ROOT", str(generated_dir / "pi-workspaces"))
    )
    return Settings(
        host=environ.get("HOST", "127.0.0.1"),
        port=int(environ.get("PORT", "8000")),
        management_api_key=environ.get("MANAGEMENT_API_KEY", ""),
        llm_api_key=environ.get("LLM_API_KEY", ""),
        llm_base_url=environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=environ.get("LLM_MODEL", "deepseek-v4-flash"),
        llm_timeout_seconds=float(environ.get("LLM_TIMEOUT_SECONDS", "30")),
        generated_dir=generated_dir,
        metadata_db_path=Path(
            environ.get(
                "METADATA_DB_PATH",
                str(generated_dir / "runtime-metadata.sqlite3"),
            )
        ),
        max_request_body_bytes=int(environ.get("MAX_REQUEST_BODY_BYTES", "1048576")),
        max_handler_result_bytes=int(environ.get("MAX_HANDLER_RESULT_BYTES", "1048576")),
        handler_timeout_seconds=float(environ.get("HANDLER_TIMEOUT_SECONDS", "2")),
        handler_memory_limit_mb=int(environ.get("HANDLER_MEMORY_LIMIT_MB", "256")),
        handler_cpu_limit_seconds=int(environ.get("HANDLER_CPU_LIMIT_SECONDS", "1")),
        max_concurrent_handlers=int(environ.get("MAX_CONCURRENT_HANDLERS", "4")),
        handler_admission_timeout_seconds=float(
            environ.get("HANDLER_ADMISSION_TIMEOUT_SECONDS", "0.1")
        ),
        generation_backend=environ.get("GENERATION_BACKEND", "direct"),
        pi_executable=environ.get("PI_EXECUTABLE", "pi"),
        pi_provider=environ.get("PI_PROVIDER", "deepseek"),
        pi_model=environ.get("PI_MODEL", "deepseek-v4-pro"),
        pi_timeout_seconds=float(environ.get("PI_TIMEOUT_SECONDS", "600")),
        pi_max_event_stream_bytes=int(
            environ.get("PI_MAX_EVENT_STREAM_BYTES", "67108864")
        ),
        pi_max_concurrent_runs=int(environ.get("PI_MAX_CONCURRENT_RUNS", "1")),
        pi_admission_timeout_seconds=float(
            environ.get("PI_ADMISSION_TIMEOUT_SECONDS", "1")
        ),
        pi_workspace_root=pi_workspace_root,
        pi_provider_env_name=environ.get("PI_PROVIDER_ENV_NAME", "DEEPSEEK_API_KEY"),
    )
