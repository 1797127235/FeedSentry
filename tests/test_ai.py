import httpx
import pytest
import respx
from pydantic import ValidationError

from feedsentry.ai import AIClient
from feedsentry.domain import DecisionAction


@respx.mock
async def test_screen_parses_accept_decision() -> None:
    route = respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"accept","reason":"major release",'
                                '"title":"V2","summary":"Adds durable workflows"}'
                            )
                        }
                    }
                ]
            },
        )
    )

    async with httpx.AsyncClient() as http:
        client = AIClient(http, "http://llm/v1", "key", "model")
        decision = await client.screen("Watch major releases", "V2", "Release notes")

    assert decision.action is DecisionAction.ACCEPT
    assert route.calls[0].request.headers["authorization"] == "Bearer key"


@respx.mock
async def test_screen_rejects_invalid_model_output() -> None:
    respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
    )

    async with httpx.AsyncClient() as http:
        client = AIClient(http, "http://llm/v1", "key", "model")
        with pytest.raises((ValueError, ValidationError)):
            await client.screen("goal", "title", "summary")


@respx.mock
async def test_summarize_rejects_fetch_decision() -> None:
    respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"action":"fetch","reason":"more detail needed"}'}}
                ]
            },
        )
    )

    async with httpx.AsyncClient() as http:
        client = AIClient(http, "http://llm/v1/", "key", "model")
        with pytest.raises(ValueError, match="fetch"):
            await client.summarize("goal", "title", "markdown")
