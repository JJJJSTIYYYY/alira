"""OpenAI SDK message/tool types validate the application wire format."""
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import TypeAdapter

from alira.agent.core.utils.message import AliraAiMessage, AliraSystemMessage, AliraToolMessage, AliraUserMessage
from tests.agent.sdk.test_bot import weather


def test_messages_and_tools_use_official_chat_shapes():
    call = {"id": "c1", "type": "function", "function": {
        "name": "weather", "arguments": '{"city":"Tokyo"}'}}
    messages = [AliraSystemMessage(content="rules"), AliraUserMessage(content=[
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}}]),
        AliraAiMessage(tool_calls=[call]), AliraToolMessage(tool_call_id="c1", content="")]
    adapter = TypeAdapter(list[ChatCompletionMessageParam])
    assert adapter.validate_python([message.to_chat() for message in messages])
    assert TypeAdapter(ChatCompletionToolParam).validate_python(weather.get_schema())
