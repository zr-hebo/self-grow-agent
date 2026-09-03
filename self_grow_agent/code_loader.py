"""Validation and isolated loading for LLM-generated request handlers."""

from __future__ import annotations

import ast
import math
import re
from types import MappingProxyType, ModuleType
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], Any]


class CodeValidationError(ValueError):
    """Generated source does not satisfy the constrained handler contract."""


class HandlerExecutionError(RuntimeError):
    """A generated handler failed without exposing its implementation traceback."""


class HandlerContractError(HandlerExecutionError):
    """A generated handler input or output is not strict JSON-compatible data."""


_MODULE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_SAFE_CALLABLES: dict[str, Callable[..., Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
}
_SAFE_CALL_NAMES = frozenset((*_SAFE_CALLABLES, "get"))
_SAFE_BUILTINS = MappingProxyType(_SAFE_CALLABLES)

_ALLOWED_AST_NODES = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Assign,
    ast.Return,
    ast.If,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Dict,
    ast.List,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.keyword,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.IfExp,
)


def _safe_get(mapping: object, key: object, default: Any = None) -> Any:
    """Read an exact built-in dict without dispatching user-defined methods."""

    if type(mapping) is not dict:
        return default
    try:
        return mapping[key]  # type: ignore[index]
    except (KeyError, TypeError):
        return default


class _HandlerAstValidator(ast.NodeVisitor):
    def __init__(self, handler: ast.FunctionDef) -> None:
        self._handler = handler

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_AST_NODES):
            node_name = type(node).__name__.lower()
            raise CodeValidationError(f"Generated code uses disallowed {node_name} syntax")
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is not self._handler:
            raise CodeValidationError("Nested function definitions are not allowed")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        _validate_identifier(node.id)
        super().generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        _validate_identifier(node.arg)
        super().generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not node.targets or any(not isinstance(target, ast.Name) for target in node.targets):
            raise CodeValidationError("Assignments may target local names only")
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            raise CodeValidationError("Attribute access is not allowed")
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_CALL_NAMES:
            raise CodeValidationError("Generated code calls a function that is not allowed")
        if any(keyword.arg is None for keyword in node.keywords):
            raise CodeValidationError("Expanded call arguments are not allowed")
        super().generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if type(node.value) not in {str, int, float, bool, type(None)}:
            raise CodeValidationError("Generated code contains a non-JSON literal")
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise CodeValidationError("Generated code contains a non-finite number")
        super().generic_visit(node)


class GeneratedCodeLoader:
    """Compile a narrowly constrained handler under restricted global names."""

    def __init__(self, max_source_chars: int = 16_000) -> None:
        if max_source_chars <= 0:
            raise ValueError("max_source_chars must be positive")
        self._max_source_chars = max_source_chars

    def validate(self, source: str) -> None:
        """Validate source without executing it."""

        self._parse_and_validate(source)

    def load(self, source: str, module_name: str) -> Handler:
        """Validate, compile, and return a JSON-contract-checking handler."""

        if not isinstance(module_name, str) or _MODULE_NAME_PATTERN.fullmatch(module_name) is None:
            raise CodeValidationError("Invalid generated module name")

        tree = self._parse_and_validate(source)
        try:
            compiled = compile(tree, f"<generated:{module_name}>", "exec", dont_inherit=True)
            module = ModuleType(module_name)
            module.__dict__.update(
                {
                    "__builtins__": _SAFE_BUILTINS,
                    "get": _safe_get,
                }
            )
            exec(compiled, module.__dict__, module.__dict__)
        except Exception:
            raise CodeValidationError("Generated source could not be loaded") from None

        raw_handler = module.__dict__.get("handle")
        if not callable(raw_handler):
            raise CodeValidationError("Generated source did not define a callable handle")
        return _checked_handler(raw_handler)

    def _parse_and_validate(self, source: str) -> ast.Module:
        if not isinstance(source, str):
            raise CodeValidationError("Generated source must be text")
        if len(source) > self._max_source_chars:
            raise CodeValidationError("Generated source is too large")
        try:
            tree = ast.parse(source, mode="exec")
        except (SyntaxError, ValueError, TypeError):
            raise CodeValidationError("Generated source has invalid syntax") from None

        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise CodeValidationError(
                "Generated source must contain exactly one top-level statement: def handle(request)"
            )
        handler = tree.body[0]
        _validate_handler_signature(handler)
        if not any(isinstance(node, ast.Return) for node in ast.walk(handler)):
            raise CodeValidationError("Generated handler must return JSON-compatible data")
        _HandlerAstValidator(handler).visit(tree)
        return tree


def _validate_identifier(identifier: str) -> None:
    if identifier.startswith("_"):
        raise CodeValidationError("Private identifiers are not allowed")


def _validate_handler_signature(handler: ast.FunctionDef) -> None:
    args = handler.args
    correct_args = (
        not args.posonlyargs
        and len(args.args) == 1
        and args.args[0].arg == "request"
        and args.args[0].annotation is None
        and args.vararg is None
        and not args.kwonlyargs
        and args.kwarg is None
        and not args.defaults
        and not args.kw_defaults
    )
    if (
        handler.name != "handle"
        or not correct_args
        or handler.decorator_list
        or handler.returns is not None
    ):
        raise CodeValidationError("Generated handler signature must be exactly def handle(request)")


def _checked_handler(raw_handler: Callable[[dict[str, Any]], Any]) -> Handler:
    def handle(request: dict[str, Any]) -> Any:
        _ensure_json_compatible(request, label="Handler request")
        try:
            result = raw_handler(request)
        except Exception as exc:
            raise HandlerExecutionError(
                f"generated handler raised {type(exc).__name__}"
            ) from None
        _ensure_json_compatible(result, label="Handler result")
        return result

    handle.__name__ = "handle"
    handle.__module__ = raw_handler.__module__
    return handle


def _ensure_json_compatible(value: Any, *, label: str) -> None:
    try:
        _walk_json_value(value, seen=set(), depth=0)
    except (TypeError, ValueError, RecursionError):
        raise HandlerContractError(f"{label} must be JSON-compatible") from None


def _walk_json_value(value: Any, *, seen: set[int], depth: int) -> None:
    if depth > 64:
        raise ValueError("JSON value is nested too deeply")
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if type(value) is list:
        value_id = id(value)
        if value_id in seen:
            raise ValueError("JSON values cannot contain cycles")
        seen.add(value_id)
        try:
            for item in value:
                _walk_json_value(item, seen=seen, depth=depth + 1)
        finally:
            seen.remove(value_id)
        return
    if type(value) is dict:
        value_id = id(value)
        if value_id in seen:
            raise ValueError("JSON values cannot contain cycles")
        seen.add(value_id)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("JSON object keys must be strings")
                _walk_json_value(item, seen=seen, depth=depth + 1)
        finally:
            seen.remove(value_id)
        return
    raise TypeError("Unsupported JSON value")
