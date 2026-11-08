from typing import Literal


def convert_generation_id_to_message_node_id(
    generation_id: str | list[str] | set[str],
    role: Literal["user", "assistant", "tool"],
) -> str | list[str] | set[str]:
    """Append a role suffix to each complete generation ID."""

    if role == "user":
        suffix = "-user"
    elif role == "assistant" or role == "tool":
        suffix = "-alira"
    else:
        raise ValueError(
            f"Invalid role: {role}. Must be one of 'user', 'assistant', 'tool'."
        )

    if isinstance(generation_id, str):
        return generation_id + suffix

    return type(generation_id)((gid + suffix) for gid in generation_id)
