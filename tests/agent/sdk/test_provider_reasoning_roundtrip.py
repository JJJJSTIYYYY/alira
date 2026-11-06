"""Native Responses reasoning survives SDK streams, results and tool replay."""

import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from my_llm import InputParameter, MyLLM

from alira.agent.core.bot import BaseProvider
from tests.agent.sdk.test_bot import weather
from tests.agent.sdk.test_bot_protocols import install_sdk, wire

RAW_PRESETS = [
    (name, model.model.default_value)
    for name in ("deepseek", "mimo")
    for model in BaseProvider.get(name).list_models()
]


def responses_wire(item, *, incremental, tool_call):
    """Include repeated native snapshots, as in a complete Responses SSE turn."""
    output = [item]
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "response_1", "status": "in_progress", "output": []},
        }
    ]
    if incremental:
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "reasoning", "id": item["id"], "summary": []},
            }
        )
        for field, event_type, index_field in (
            ("summary", "response.reasoning_summary_text.delta", "summary_index"),
            ("content", "response.reasoning_text.delta", "content_index"),
        ):
            for index, part in enumerate(item.get(field, [])):
                text = part["text"]
                for delta in (text[:8], text[8:]):
                    events.append(
                        {
                            "type": event_type,
                            "item_id": item["id"],
                            "output_index": 0,
                            index_field: index,
                            "delta": delta,
                        }
                    )
                events.append(
                    {
                        "type": event_type.removesuffix("delta") + "done",
                        "item_id": item["id"],
                        "output_index": 0,
                        index_field: index,
                        "text": text,
                    }
                )
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": item,
            }
        )
    if tool_call:
        output.append(
            {
                "type": "function_call",
                "id": "fc1",
                "call_id": "call_weather",
                "name": "weather",
                "arguments": '{"city":"Tokyo"}',
                "status": "completed",
            }
        )
    else:
        output.append(
            {
                "type": "message",
                "id": "msg1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "hello", "annotations": []}
                ],
            }
        )
    events.append(
        {
            "type": "response.completed",
            "response": {"id": "response_1", "status": "completed", "output": output},
        }
    )
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    )


@pytest.mark.parametrize("provider,model", RAW_PRESETS)
@pytest.mark.parametrize(
    "kind", ["raw", "raw_encrypted", "summary_encrypted", "encrypted"]
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("incremental", [False, True])
async def test_native_reasoning_tool_roundtrip(
    monkeypatch, provider, model, kind, streaming, incremental
):
    item = {"type": "reasoning", "id": "rs1", "summary": [], "status": "completed"}
    text = "visible reasoning"
    if kind.startswith("raw"):
        item["content"] = [{"type": "reasoning_text", "text": text}]
    elif kind == "summary_encrypted":
        item["summary"] = [{"type": "summary_text", "text": text}]
    if "encrypted" in kind:
        item["encrypted_content"] = "opaque-feedback-value"
    original = deepcopy(item)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = (
            responses_wire(item, incremental=incremental, tool_call=True)
            if len(requests) == 1
            else wire("openai_responses")
        )
        return httpx.Response(
            200, text=response, headers={"content-type": "text/event-stream"}
        )

    clients = install_sdk(monkeypatch, "openai_responses", handler)
    preset = BaseProvider.get(provider)
    profile = replace(
        preset.get_profile(),
        api_key=InputParameter(path="$.api_key", default_value="test"),
    )
    tools = [
        {
            "type": "function",
            **weather.get_schema().to_wire()["functions"][0],
            "strict": False,
        }
    ]
    initial = {"role": "user", "content": "Check the weather in Tokyo."}
    async with MyLLM(
        provider=profile,
        model=preset.get_model(model),
        stdio_protocol="openai_responses",
    ) as llm:
        if streaming:
            stream = await llm.generate(messages=[initial], tools=tools, stream=True)
            async with stream:
                events = [event async for event in stream]
            result = stream.result
            visible = "".join(
                event["delta"]
                for event in events
                if event["type"]
                in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }
            )
            assert visible == ("" if kind == "encrypted" else text)
        else:
            result = await llm.generate(messages=[initial], tools=tools)
        assert (
            next(entry for entry in result if entry["type"] == "reasoning") == original
        )
        call = next(entry for entry in result if entry["type"] == "function_call")
        assert call["call_id"] == "call_weather"
        history = [
            initial,
            *result,
            {
                "type": "function_call_output",
                "call_id": "call_weather",
                "output": "sunny",
            },
        ]
        saved_history = deepcopy(history)
        await llm.generate(messages=history, tools=tools)
        replay = next(
            entry for entry in requests[1]["input"] if entry["type"] == "reasoning"
        )
        assert replay == original
        output = next(
            entry
            for entry in requests[1]["input"]
            if entry["type"] == "function_call_output"
        )
        assert output["call_id"] == call["call_id"]
        assert history == saved_history
    assert item == original
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize("provider,model", RAW_PRESETS)
async def test_native_reasoning_feedback_without_tools_follows_profile(
    monkeypatch, provider, model
):
    item = {
        "type": "reasoning",
        "id": "rs1",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "visible reasoning"}],
    }
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=responses_wire(item, incremental=False, tool_call=False),
            headers={"content-type": "text/event-stream"},
        )

    clients = install_sdk(monkeypatch, "openai_responses", handler)
    preset = BaseProvider.get(provider)
    async with MyLLM(
        provider=replace(
            preset.get_profile(),
            api_key=InputParameter(path="$.api_key", default_value="test"),
        ),
        model=preset.get_model(model),
        stdio_protocol="openai_responses",
    ) as llm:
        initial = {"role": "user", "content": "hi"}
        result = await llm.generate(messages=[initial])
        assert next(entry for entry in result if entry["type"] == "reasoning") == item
        await llm.generate(
            messages=[
                initial,
                *result,
                {"role": "user", "content": "continue"},
            ]
        )
    replay = [entry for entry in requests[1]["input"] if entry["type"] == "reasoning"]
    assert replay == ([item] if provider == "deepseek" else [])
    assert all(client.is_closed for client in clients)
