"""Application-owned message fields and business extension properties."""

from alira.agent.core.utils.message import AliraAiMessage, AliraToolMessage


def test_message_object_keeps_only_application_owned_fields():
    message = AliraAiMessage(
        message_uid="message-1",
        name="assistant",
        content="answer",
        metadata={"model": "gpt-test", "usage": {"input_tokens": 2}},
        reasoning_content="because",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"query":"alira"}'},
            }
        ],
        extensions={"uploaded_files": ["brief.pdf"]},
    )

    assert message.reasoning_content == "because"
    assert message.tool_calls[0]["id"] == "call-1"
    assert message.uploaded_files == ["brief.pdf"]
    assert not hasattr(message, "id")
    assert not hasattr(message, "timestamp")


def test_extension_properties_update_the_underlying_dictionary():
    message = AliraToolMessage(
        message_uid="message-2",
        name="search",
        content="result",
        tool_call_id="call-1",
    )

    message.active_file = "notes.md"
    message.referenced_message = {"role": "user", "content": "find this"}
    message.task = {"type": "automated_task", "prompt": "summarize"}
    message.todo_list = [{"content": "read", "status": "pending"}]
    message.system_instruction = ["cite sources"]

    assert message.extensions == {
        "active_file": "notes.md",
        "referenced_message": {"role": "user", "content": "find this"},
        "task": {"type": "automated_task", "prompt": "summarize"},
        "todo_list": [{"content": "read", "status": "pending"}],
        "system_instruction": ["cite sources"],
    }
