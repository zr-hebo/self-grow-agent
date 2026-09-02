from __future__ import annotations

import pytest

from self_grow_agent.code_loader import (
    CodeValidationError,
    GeneratedCodeLoader,
    HandlerContractError,
)

VALID_SOURCE = '''\
def handle(request):
    name = get(request["query"], "name", "world")
    return {"message": "hello " + str(name)}
'''


def test_loads_handler_with_the_exact_contract() -> None:
    handler = GeneratedCodeLoader().load(VALID_SOURCE, "hello_v1")

    assert handler({"query": {"name": "Tom"}}) == {"message": "hello Tom"}
    assert handler({"query": {}}) == {"message": "hello world"}


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def handle(request)\n    return {}", "syntax"),
        ("import os\n\ndef handle(request):\n    return {}", "top-level"),
        (
            "def handle(request):\n    return request.get(\"query\")",
            "Attribute",
        ),
        (
            "def handle(request):\n    while True:\n        return {}",
            "while",
        ),
        (
            "VALUE = 1\n\ndef handle(request):\n    return {}",
            "top-level",
        ),
        ("def handle():\n    return {}", "signature"),
        ("def handle(payload):\n    return {}", "signature"),
        ("async def handle(request):\n    return {}", "top-level"),
        (
            "def handle(request):\n    return open(\"secret\")",
            "not allowed",
        ),
    ],
)
def test_rejects_unsafe_or_invalid_source(source: str, message: str) -> None:
    with pytest.raises(CodeValidationError, match=message):
        GeneratedCodeLoader().load(source, "bad_v1")


def test_rejects_oversized_source() -> None:
    loader = GeneratedCodeLoader(max_source_chars=64)

    with pytest.raises(CodeValidationError, match="too large"):
        loader.load(VALID_SOURCE, "hello_v1")


def test_rejects_private_identifiers() -> None:
    source = '''\
def handle(request):
    _secret = "hidden"
    return {"value": _secret}
'''

    with pytest.raises(CodeValidationError, match="Private identifiers"):
        GeneratedCodeLoader().load(source, "bad_v1")


def test_rejects_non_json_handler_results() -> None:
    source = '''\
def handle(request):
    return float("nan")
'''
    handler = GeneratedCodeLoader().load(source, "bad_result_v1")

    with pytest.raises(HandlerContractError, match="JSON-compatible"):
        handler({})


def test_generated_code_has_only_restricted_builtins() -> None:
    source = '''\
def handle(request):
    return globals()
'''

    with pytest.raises(CodeValidationError, match="not allowed"):
        GeneratedCodeLoader().load(source, "bad_v1")
