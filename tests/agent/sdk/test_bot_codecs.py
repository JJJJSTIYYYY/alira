"""Application messages extend middleware without rebuilding provider codecs."""

from my_llm import (
    AssistantMessage,
    ItemIdentity,
    ReasoningContent,
    TextContent,
    ToolCall,
)

from alira.shared.message import AliraAssistantMessage


def test_completed_library_message_keeps_ordered_middleware_content():
    value = AssistantMessage(
        role="assistant",
        protocol="openai_responses",
        item=ItemIdentity("m1"),
        content=[
            ReasoningContent("why", feedback_item={"encrypted_content": "opaque"}),
            TextContent("answer"),
            ToolCall("c1", "search", {"q": "alira"}),
        ],
    )
    message = AliraAssistantMessage.from_middleware(value, extensions={"todo_list": []})
    assert message.to_wire() == value.to_wire()
    assert message.content is value.content
    assert message.content[0].feedback_item == {"encrypted_content": "opaque"}
    assert "extensions" not in message.to_wire()
