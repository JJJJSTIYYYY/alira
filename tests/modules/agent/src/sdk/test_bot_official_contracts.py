"""Library middleware models define the application's inference boundary."""

from my_llm import FunctionTool, Message, ProtocolMiddleWare, TextContent, ToolCall
from my_llm.middleware.protocol import generation_input

from alira.modules.agent.src.shared.message import (
    AliraAssistantMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from tests.modules.agent.src.sdk.test_bot import weather


def test_messages_and_tools_use_library_middleware_contracts():
    messages = [
        AliraSystemMessage(content=[TextContent("rules")]),
        AliraUserMessage(content=[TextContent("look")]),
        AliraAssistantMessage(content=[ToolCall("c1", "weather", {"city": "Tokyo"})]),
        AliraToolMessage(call_id="c1", content=[TextContent("")]),
    ]
    assert all(isinstance(message, Message) for message in messages)
    assert isinstance(weather.get_schema(), FunctionTool)
    middleware = generation_input(messages, [weather.get_schema()])
    assert isinstance(middleware, ProtocolMiddleWare)
    assert middleware.messages[-1].call_id == "c1"
    assert middleware.tools[0].name == "weather"
