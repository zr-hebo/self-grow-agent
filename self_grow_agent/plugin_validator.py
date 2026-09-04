"""Static validation gate for generated multi-file Python plugins."""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass

from self_grow_agent.plugin_models import GeneratedPlugin, PluginPolicy

_FORBIDDEN_MODULES = frozenset(
    {
        "ctypes",
        "importlib",
        "multiprocessing",
        "os",
        "pathlib",
        "pickle",
        "resource",
        "shutil",
        "socket",
        "subprocess",
        "tempfile",
    }
)
_FORBIDDEN_CALLS = frozenset({"__import__", "compile", "eval", "exec", "open"})
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:api_?key|password|passwd|secret|token|cookie)(?:$|_)", re.IGNORECASE
)
_DISTRIBUTION_IMPORT_ROOTS = {
    "mysql-connector-python": frozenset({"mysql"}),
}


class PluginValidationError(ValueError):
    """A plugin is syntactically valid JSON but unsafe or not executable."""


@dataclass(frozen=True, slots=True)
class PluginValidationResult:
    """Safe metadata produced by static validation."""

    imported_modules: tuple[str, ...]


class PluginValidator:
    """Validate syntax, entrypoint, imports, calls, and credential literals."""

    def __init__(self, policy: PluginPolicy) -> None:
        if not isinstance(policy, PluginPolicy):
            raise TypeError("policy must be a PluginPolicy")
        self._policy = policy

    def validate(self, plugin: GeneratedPlugin) -> PluginValidationResult:
        """Validate one complete plugin without importing or executing it."""

        plugin = self._policy.validate(plugin)
        declared_modules = _declared_import_roots(plugin.dependencies) | _local_import_roots(
            plugin
        )
        imported_modules: set[str] = set()
        handler_tree: ast.Module | None = None

        for plugin_file in plugin.files:
            try:
                tree = ast.parse(plugin_file.content, filename=plugin_file.path)
            except (SyntaxError, ValueError, TypeError):
                raise PluginValidationError(
                    f"plugin file {plugin_file.path!r} has a syntax error"
                ) from None
            if plugin_file.path == "handler.py":
                handler_tree = tree
            imported_modules.update(
                _validate_tree(tree, plugin_file.path, declared_modules)
            )

        if handler_tree is None:  # also enforced by PluginPolicy; keep this gate explicit
            raise PluginValidationError("plugin entrypoint handler.py is missing")
        _validate_entrypoint(handler_tree)
        return PluginValidationResult(imported_modules=tuple(sorted(imported_modules)))


def _validate_tree(
    tree: ast.Module,
    filename: str,
    declared_modules: frozenset[str],
) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                _validate_import(root, filename, declared_modules)
                imports.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise PluginValidationError("plugin relative imports are not allowed")
            if not node.module:
                raise PluginValidationError("plugin import is invalid")
            root = node.module.partition(".")[0]
            _validate_import(root, filename, declared_modules)
            imports.add(root)
        elif isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in _FORBIDDEN_CALLS:
                raise PluginValidationError(
                    f"plugin file {filename!r} uses forbidden call {call_name!r}"
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            _validate_assignment_credentials(node, filename)
        elif isinstance(node, ast.Dict):
            _validate_mapping_credentials(node, filename)
    return imports


def _validate_import(
    root: str,
    filename: str,
    declared_modules: frozenset[str],
) -> None:
    if root in _FORBIDDEN_MODULES:
        raise PluginValidationError(
            f"plugin file {filename!r} imports forbidden module {root!r}"
        )
    if root in sys.stdlib_module_names or root in declared_modules:
        return
    raise PluginValidationError(
        f"plugin file {filename!r} imports undeclared dependency {root!r}"
    )


def _declared_import_roots(dependencies: tuple[str, ...]) -> frozenset[str]:
    roots = set()
    for dependency in dependencies:
        name = dependency.partition("==")[0]
        roots.update(
            _DISTRIBUTION_IMPORT_ROOTS.get(
                name,
                frozenset({re.sub(r"[-.]+", "_", name)}),
            )
        )
    return frozenset(roots)


def _local_import_roots(plugin: GeneratedPlugin) -> frozenset[str]:
    roots = set()
    for plugin_file in plugin.files:
        first = plugin_file.path.partition("/")[0]
        roots.add(first.removesuffix(".py"))
    return frozenset(roots)


def _call_name(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    return None


def _validate_assignment_credentials(
    node: ast.Assign | ast.AnnAssign | ast.NamedExpr,
    filename: str,
) -> None:
    if isinstance(node, ast.Assign):
        names = [name for target in node.targets for name in _target_names(target)]
        value = node.value
    else:
        names = _target_names(node.target)
        value = node.value
    if any(_CREDENTIAL_NAME.search(name) for name in names) and _is_nonempty_literal(value):
        raise PluginValidationError(
            f"plugin file {filename!r} contains a hardcoded credential literal"
        )


def _validate_mapping_credentials(node: ast.Dict, filename: str) -> None:
    for key, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and _CREDENTIAL_NAME.search(key.value)
            and _is_nonempty_literal(value)
        ):
            raise PluginValidationError(
                f"plugin file {filename!r} contains a hardcoded credential literal"
            )


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _target_names(item)]
    return []


def _is_nonempty_literal(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)) and bool(
        value.value
    )


def _validate_entrypoint(tree: ast.Module) -> None:
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle"
    ]
    if len(functions) != 1:
        raise PluginValidationError("plugin entrypoint must define exactly one handle function")
    function = functions[0]
    if isinstance(function, ast.AsyncFunctionDef):
        raise PluginValidationError("plugin entrypoint handle must be synchronous")
    arguments = function.args
    positional_arguments = [*arguments.posonlyargs, *arguments.args]
    if (
        len(positional_arguments) != 1
        or positional_arguments[0].arg != "request"
        or arguments.posonlyargs
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or arguments.kwonlyargs
        or arguments.defaults
        or arguments.kw_defaults
        or function.decorator_list
    ):
        raise PluginValidationError("plugin entrypoint must use signature handle(request)")


__all__ = ["PluginValidationError", "PluginValidationResult", "PluginValidator"]
