"""Application-owned fields extend the library's typed middleware messages."""

from my_llm import (
    AssistantMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolMessage,
)

from alira.modules.agent.src.shared.message import AliraAssistantMessage, AliraToolMessage


def test_message_object_keeps_application_fields_out_of_protocol():
    message = AliraAssistantMessage(
        message_uid="message-1",
        name="assistant",
        generation_id="g1",
        content=[
            ReasoningContent("because"),
            TextContent("answer"),
            ToolCall("call-1", "search", {"query": "alira"}),
        ],
        metadata={"usage": {"input_tokens": 2}},
        extensions={"uploaded_files": ["brief.pdf"]},
    )
    assert isinstance(message, AssistantMessage)
    assert message.uploaded_files == ["brief.pdf"]
    assert message.content[2].call_id == "call-1"
    assert message.to_record()["generation_id"] == "g1"
    assert "generation_id" not in message.to_wire()
    assert not hasattr(message, "id")
    assert not hasattr(message, "timestamp")


def test_extension_properties_update_the_underlying_dictionary():
    message = AliraToolMessage(
        message_uid="message-2",
        name="search",
        content=[TextContent("result")],
        call_id="call-1",
    )
    assert isinstance(message, ToolMessage)
    assert message.role == "tool"
    message.active_file = "notes.md"
    message.referenced_message = {"role": "user"}
    message.task = {"type": "automated_task"}
    message.todo_list = [{"content": "read", "status": "pending"}]
    message.system_instruction = ["cite sources"]
    assert message.extensions == {
        "active_file": "notes.md",
        "referenced_message": {"role": "user"},
        "task": {"type": "automated_task"},
        "todo_list": [{"content": "read", "status": "pending"}],
        "system_instruction": ["cite sources"],
    }
    message.active_file = message.referenced_message = message.task = None
    assert not {"active_file", "referenced_message", "task"} & message.extensions.keys()
