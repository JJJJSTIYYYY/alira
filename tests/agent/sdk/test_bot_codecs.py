"""No application codec or custom stream accumulator is needed."""
from alira.agent.core.utils.message import AliraAiMessage


def test_completed_library_message_keeps_chat_protocol_fields():
    value = {"role": "assistant", "content": None, "refusal": "no",
             "audio": {"id": "a1"}, "annotations": [],
             "reasoning_content": "why", "tool_calls": []}
    message = AliraAiMessage.from_chat(value, extensions={"todo_list": []})
    assert message.to_chat() == value
    value["audio"]["id"] = "changed"
    assert message.audio == {"id": "a1"}
