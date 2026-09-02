"""Regression checks that keep concrete credentials out of the repository."""

from __future__ import annotations

import ast
import ipaddress
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

_DYNAMIC = object()
_SENSITIVE_WORDS = {
    "authorization",
    "credential",
    "credentials",
    "passphrase",
    "passwd",
    "password",
    "secret",
    "token",
}
_SENSITIVE_PAIRS = {
    ("access", "key"),
    ("api", "key"),
    ("auth", "key"),
    ("client", "key"),
    ("management", "key"),
    ("private", "key"),
    ("signing", "key"),
}
_IDENTIFIER_SUFFIXES = {
    "env",
    "environment",
    "field",
    "header",
    "label",
    "name",
    "variable",
    "var",
}
_FIXTURE_WORDS = {"code", "fixture", "source"}
_FORBIDDEN_TEST_WORDS = {"blocked", "forbidden", "invalid", "malicious", "reject", "unsafe"}

# This file is deliberately excluded from the repository token scan so the signatures below
# cannot report their own regex source as a credential.
_PROVIDER_PATTERNS = (
    ("OpenAI API token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
)
_CREDENTIAL_URL = re.compile(
    r"https?://[^\s/:@\"'<>]+:[^\s/@\"'<>]+@[^\s\"'<>]+",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Finding:
    path: Path
    line: int
    reason: str

    def display(self) -> str:
        return f"{self.path.relative_to(PROJECT_ROOT)}:{self.line}: {self.reason}"


def _normalize_name(name: str) -> list[str]:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [part for part in re.sub(r"[^A-Za-z0-9]+", "_", snake_case).lower().split("_") if part]


def _is_sensitive_name(name: str) -> bool:
    parts = _normalize_name(name)
    if any(part in _SENSITIVE_WORDS for part in parts):
        return True
    if "apikey" in parts:
        return True
    return any(pair in zip(parts, parts[1:]) for pair in _SENSITIVE_PAIRS)


def _literal_scalar(node: ast.AST) -> object:
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, bytes, int, float, complex, bool, type(None))
    ):
        return node.value
    return _DYNAMIC


def _static_text(node: ast.AST | None) -> str | bytes | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left)
        right = _static_text(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, bytes) and isinstance(right, bytes):
            return left + right
        return None
    if not isinstance(node, ast.JoinedStr):
        return None

    pieces: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            pieces.append(part.value)
            continue
        if not isinstance(part, ast.FormattedValue):
            return None
        value = _literal_scalar(part.value)
        if value is _DYNAMIC:
            return None
        format_spec = _static_text(part.format_spec)
        if part.format_spec is not None and not isinstance(format_spec, str):
            return None
        try:
            if part.conversion == ord("r"):
                value = repr(value)
            elif part.conversion == ord("s"):
                value = str(value)
            elif part.conversion == ord("a"):
                value = ascii(value)
            elif part.conversion != -1:
                return None
            pieces.append(format(value, format_spec or ""))
        except (TypeError, ValueError):
            return None
    return "".join(pieces)


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, ast.Subscript):
        key = _static_text(target.slice)
        return [key] if isinstance(key, str) else []
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return [name for item in target.elts for name in _target_names(item)]
    return []


def _is_noncredential_label(name: str, value: str | bytes) -> bool:
    if not isinstance(value, str):
        return False
    parts = set(_normalize_name(name))
    if not parts.intersection(_IDENTIFIER_SUFFIXES):
        return False
    return re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) is not None


def _is_redaction_message(name: str, value: str | bytes) -> bool:
    if not isinstance(value, str):
        return False
    parts = set(_normalize_name(name))
    return bool(parts.intersection({"error", "message", "redaction"})) and any(
        marker in value.lower() for marker in ("redact", "must not", "not available")
    )


def _is_forbidden_code_fixture(path: Path, name: str, value: str | bytes) -> bool:
    if not isinstance(value, str) or "tests" not in path.parts:
        return False
    parts = set(_normalize_name(name))
    if not parts.intersection(_FIXTURE_WORDS):
        return False
    try:
        parsed = ast.parse(value)
    except SyntaxError:
        return False
    return bool(parsed.body) and not (
        len(parsed.body) == 1
        and isinstance(parsed.body[0], ast.Expr)
        and isinstance(parsed.body[0].value, ast.Constant)
    )


class _CredentialVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: set[Finding] = set()

    def _check(self, name: str, value_node: ast.AST | None, line: int) -> None:
        if not _is_sensitive_name(name):
            return
        value = _static_text(value_node)
        if value in (None, "", b""):
            return
        if (
            _is_noncredential_label(name, value)
            or _is_redaction_message(name, value)
            or _is_forbidden_code_fixture(self.path, name, value)
        ):
            return
        self.findings.add(Finding(self.path, line, f"hardcoded value assigned to {name!r}"))

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name in _target_names(target):
                self._check(name, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        for name in _target_names(node.target):
            self._check(name, node.value, node.lineno)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        for name in _target_names(node.target):
            self._check(name, node.value, node.lineno)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key_node, value_node in zip(node.keys, node.values):
            key = _static_text(key_node)
            if isinstance(key, str):
                self._check(key, value_node, getattr(value_node, "lineno", node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg is not None:
                self._check(keyword.arg, keyword.value, keyword.value.lineno)
        self._check_environment_default(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function_defaults(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function_defaults(node.args)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._check_function_defaults(node.args)
        self.generic_visit(node)

    def _check_function_defaults(self, arguments: ast.arguments) -> None:
        positional = [*arguments.posonlyargs, *arguments.args]
        for argument, default in zip(positional[-len(arguments.defaults) :], arguments.defaults):
            self._check(argument.arg, default, default.lineno)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
            if default is not None:
                self._check(argument.arg, default, default.lineno)

    def _check_environment_default(self, node: ast.Call) -> None:
        call_path = _call_path(node.func)
        if call_path[-1:] == ["getenv"]:
            pass
        elif call_path[-2:] not in (["environ", "get"], ["environ", "setdefault"]):
            return

        key_node = node.args[0] if node.args else _keyword_value(node, "key")
        default_node = node.args[1] if len(node.args) > 1 else _keyword_value(node, "default")
        key = _static_text(key_node)
        if isinstance(key, str) and _is_sensitive_name(key):
            self._check(key, default_node, getattr(default_node, "lineno", node.lineno))


def _call_path(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_call_path(node.value), node.attr]
    return []


def _keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    return next((keyword.value for keyword in node.keywords if keyword.arg == name), None)


def _python_paths() -> list[Path]:
    candidates = [PROJECT_ROOT / "config.py", PROJECT_ROOT / "main.py"]
    for directory in ("self_grow_agent", "cicd_case", "tests"):
        candidates.extend((PROJECT_ROOT / directory).rglob("*.py"))
    return sorted(path for path in set(candidates) if path.is_file() and path.resolve() != THIS_FILE)


def _scan_python(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, f"could not parse Python source: {exc.msg}")]
    visitor = _CredentialVisitor(path)
    visitor.visit(tree)
    return sorted(visitor.findings)


def _tracked_security_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    relevant_suffixes = {".cfg", ".conf", ".ini", ".json", ".py", ".sh", ".toml", ".yaml", ".yml"}
    relevant_names = {"Dockerfile", "Makefile", ".env.example"}
    paths: list[Path] = []
    for raw_path in result.stdout.decode("utf-8").split("\0"):
        if not raw_path:
            continue
        path = PROJECT_ROOT / raw_path
        if path.resolve() == THIS_FILE:
            continue
        if path.name in relevant_names or path.suffix.lower() in relevant_suffixes:
            paths.append(path)
    return sorted(paths)


def _is_obvious_placeholder(value: str) -> bool:
    lowered = value.lower()
    markers = ("<", "${", "example", "placeholder", "redacted", "replace", "your-")
    if any(marker in lowered for marker in markers):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", value)
    return len(compact) >= 8 and len(set(compact.lower())) == 1


def _is_safe_example_url(value: str) -> bool:
    try:
        hostname = urlsplit(value.rstrip(".,);]")).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    return hostname in {"example", "example.com", "example.net", "example.org"} or hostname.endswith(
        (".example", ".example.com", ".example.net", ".example.org", ".invalid", ".test")
    )


def _python_fixture_lines(path: Path, source: str) -> set[int]:
    if "tests" not in path.parts:
        return set()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set()

    fixture_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            words = set(_normalize_name(node.name))
            if words.intersection(_FORBIDDEN_TEST_WORDS):
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        fixture_lines.update(range(child.lineno, (child.end_lineno or child.lineno) + 1))
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        value = _static_text(value_node)
        names = [name for target in targets for name in _target_names(target)]
        if value_node is None or not isinstance(value, str):
            continue
        if any(set(_normalize_name(name)).intersection(_FIXTURE_WORDS) for name in names):
            try:
                parsed = ast.parse(value)
            except SyntaxError:
                continue
            if parsed.body and not (
                len(parsed.body) == 1
                and isinstance(parsed.body[0], ast.Expr)
                and isinstance(parsed.body[0].value, ast.Constant)
            ):
                fixture_lines.update(
                    range(value_node.lineno, (value_node.end_lineno or value_node.lineno) + 1)
                )
    return fixture_lines


def _scan_provider_tokens(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8", errors="replace")
    fixture_lines = _python_fixture_lines(path, source)
    findings: list[Finding] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if line_number in fixture_lines:
            continue
        for label, pattern in _PROVIDER_PATTERNS:
            for match in pattern.finditer(line):
                if not _is_obvious_placeholder(match.group(0)):
                    findings.append(Finding(path, line_number, f"concrete {label} detected"))
        for match in _CREDENTIAL_URL.finditer(line):
            value = match.group(0)
            if not _is_obvious_placeholder(value) and not _is_safe_example_url(value):
                findings.append(Finding(path, line_number, "credentials embedded in URL"))
    return findings


def _env_example_findings() -> list[Finding]:
    path = PROJECT_ROOT / ".env.example"
    findings: list[Finding] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if not _is_sensitive_name(key.strip()):
            continue
        normalized_value = value.strip()
        if normalized_value.startswith("#"):
            normalized_value = ""
        if normalized_value not in {"", "''", '\"\"'}:
            findings.append(Finding(path, line_number, f"{key.strip()} must have an empty example value"))
    return findings


def _assert_no_findings(findings: list[Finding]) -> None:
    assert not findings, "Hardcoded credentials found:\n" + "\n".join(
        finding.display() for finding in sorted(set(findings))
    )


def test_static_credential_detector_covers_supported_ast_shapes() -> None:
    string_values = [secrets.token_urlsafe(24) for _ in range(10)]
    bytes_values = [secrets.token_bytes(24) for _ in range(3)]
    source = f"""
password = {string_values[0]!r}
api_key: str = {string_values[1]!r}
if access_token := {string_values[2]!r}:
    pass
settings.client_secret = {bytes_values[0]!r}
settings["private_key"] = {string_values[3]!r} + {string_values[4]!r}
payload = {{"credential": f"{{{string_values[5]!r}}}"}}
connect(auth_token={string_values[6]!r})
def function(management_key={string_values[7]!r}, *, signing_key={bytes_values[1]!r}):
    pass
os.getenv("PASSWORD", {string_values[8]!r})
environ.get("API_KEY", {bytes_values[2]!r})
os.environ.setdefault("CLIENT_SECRET", {string_values[9]!r})
"""
    visitor = _CredentialVisitor(PROJECT_ROOT / "tests" / "detector_fixture.py")
    visitor.visit(ast.parse(source))

    assert {finding.line for finding in visitor.findings} == {2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14}


def test_static_credential_detector_allows_runtime_and_empty_values() -> None:
    source = """
api_key = secrets.token_urlsafe(32)
password = ""
client_secret = None
authorization_header = "Authorization"
management_key_env_var = "MANAGEMENT_API_KEY"
redaction_message = "secret value was redacted"
authorization = f"Bearer {LLM_API_KEY}"
os.getenv("PASSWORD")
environ.get("API_KEY", "")
os.environ.setdefault("CLIENT_SECRET", None)
secrets.compare_digest(provided_key, expected_key)
"""
    visitor = _CredentialVisitor(PROJECT_ROOT / "tests" / "detector_fixture.py")
    visitor.visit(ast.parse(source))

    assert not visitor.findings


def test_python_sources_do_not_contain_static_credentials() -> None:
    _assert_no_findings([finding for path in _python_paths() for finding in _scan_python(path)])


def test_env_example_keeps_sensitive_values_empty() -> None:
    _assert_no_findings(_env_example_findings())


def test_tracked_sources_do_not_contain_provider_tokens() -> None:
    _assert_no_findings(
        [finding for path in _tracked_security_paths() for finding in _scan_provider_tokens(path)]
    )
