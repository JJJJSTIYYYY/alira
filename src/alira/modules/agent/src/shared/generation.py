"""Agent generation state and context binding available to dependent modules."""

from functools import cached_property
import time
from dataclasses import dataclass, field
from typing import Literal
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from alira.modules.agent.src.shared.identity import ConversationIdentity


@dataclass
class Generation:
    """
    State for a single AI generation.
    """

    identity: ConversationIdentity
    generation_id: str = field(default_factory=lambda: uuid4().hex)

    @cached_property
    def user_uid(self):
        return self.identity["id"]

    @cached_property
    def conversation_uid(self):
        return self.identity["conversation_uid"]

    @cached_property
    def platform(self):
        return self.identity["platform"]

    # running / finished / aborted
    status: Literal["running", "finished", "aborted"] = "running"

    cached_tokens: dict = field(default_factory=dict)
    parent_node_id: str = field(default='-')

    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.identity:
            raise ValueError("Can not initialize an unbounded Generation.")

    def get_node_id(self, role: Literal["user", "assistant", "tool"]):
        """Get the node ID bound with this generation for a given role."""

        if role == "user":
            suffix = "-user"
        elif role == "assistant" or role == "tool":
            suffix = "-alira"
        else:
            raise ValueError(
                f"Invalid role: {role}. Must be one of 'user', 'assistant', 'tool'."
            )

        if isinstance(self.generation_id, str):
            return self.generation_id + suffix

        return type(self.generation_id)((gid + suffix) for gid in self.generation_id)

    def get_parent_node_id(self):
        """Get the parent node ID for this generation."""
        return self.parent_node_id

_current_generation: ContextVar["Generation | None"] = ContextVar(
    "agent_generation",
    default=None,
)


def get_generation() -> Generation:
    """Return the generation bound to the current execution context.

    Raises:
        RuntimeError: If called outside a generation context.
    """
    generation = _current_generation.get()
    if generation is None:
        raise RuntimeError("get_generation() is only available inside generation_context().")
    return generation


@contextmanager
def generation_context(generation: Generation) -> Generator[None]:
    """Bind a generation for this context and restore the previous one on exit."""
    token = _current_generation.set(generation)
    try:
        yield
    finally:
        _current_generation.reset(token)
