from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace

import pytest

from self_grow_agent.llm import (
    GenerationError,
    OpenAIFeatureGenerator,
    parse_generated_handler,
)
from self_grow_agent.models import GeneratedHandler

SOURCE = 'def handle(request):\n    return {"message": "hello"}\n'
LLM_API_KEY = secrets.token_urlsafe(32)


class FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeOpenAIClient:
    def __init__(self, output_text: str) -> None:
        self.responses = FakeResponses(output_text)


@pytest.mark.parametrize(
    "text",
    [
        '{"source":"def handle(request):\\n    return {}\\n"}',
        '```json\n{"source":"def handle(request):\\n    return {}\\n",'
        '"description":"empty"}\n```',
        '```\n{"source":"def handle(request):\\n    return {}\\n"}\n```',
    ],
)
def test_parse_generated_handler_accepts_fenced_or_unfenced_json(text: str) -> None:
    result = parse_generated_handler(text)

    assert result.source == "def handle(request):\n    return {}\n"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"source": 42}',
        '{"source":"def handle(request):\\n    return {}","extra":true}',
        'Here you go: {"source":"def handle(request):\\n    return {}"}',
        "```json\n{}\n``` trailing text",
    ],
)
def test_parse_generated_handler_rejects_non_strict_output(text: str) -> None:
    with pytest.raises(GenerationError):
        parse_generated_handler(text)


def test_generator_uses_responses_api_and_strict_json_schema() -> None:
    client = FakeOpenAIClient(
        '{"source":"def handle(request):\\n    return {\\"message\\": '
        '\\"hello\\"}\\n","description":"greeting"}'
    )
    generator = OpenAIFeatureGenerator(
        api_key=LLM_API_KEY,
        base_url="https://llm.example/v1",
        model="test-model",
        timeout_seconds=5,
        client=client,
    )

    result = asyncio.run(
        generator.generate(
            instruction="Say hello",
            path="/hello",
            method="GET",
        )
    )

    assert result == GeneratedHandler(source=SOURCE, description="greeting")
    call = client.responses.calls[0]
    assert call["model"] == "test-model"
    assert call["store"] is False
    assert call["text"]["format"]["type"] == "json_schema"  # type: ignore[index]
    assert call["text"]["format"]["strict"] is True  # type: ignore[index]
    assert "GET /hello" in str(call["input"])


def test_generator_includes_current_source_for_updates() -> None:
    client = FakeOpenAIClient('{"source":"def handle(request):\\n    return {}\\n"}')
    generator = OpenAIFeatureGenerator(
        api_key=LLM_API_KEY,
        base_url="https://llm.example/v1",
        model="test-model",
        client=client,
    )

    asyncio.run(
        generator.generate(
            instruction="Change greeting",
            path="/hello",
            method="PUT",
            current_source=SOURCE,
        )
    )

    assert SOURCE in str(client.responses.calls[0]["input"])


def test_generator_wraps_provider_failures() -> None:
    class FailingResponses:
        async def create(self, **kwargs: object) -> None:
            raise RuntimeError("secret provider details")

    client = SimpleNamespace(responses=FailingResponses())
    generator = OpenAIFeatureGenerator(
        api_key=LLM_API_KEY,
        base_url="https://llm.example/v1",
        model="test-model",
        client=client,
    )

    with pytest.raises(GenerationError, match="request failed") as exc_info:
        asyncio.run(
            generator.generate(
                instruction="Say hello",
                path="/hello",
                method="GET",
            )
        )

    assert "secret provider details" not in str(exc_info.value)
