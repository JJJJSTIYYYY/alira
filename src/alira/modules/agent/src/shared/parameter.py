"""Agent loop configuration shared with modules integrating the agent runtime."""

from typing import NotRequired, TypedDict

from alira.modules.agent.src.shared.identity import AgentIdentity


class LoopParameter(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    models_provider: str
    model_name: str
    api_key: str
    enable_think: bool
    compress_trigger: float  # 0 ~ 1.0, If zero, not compress or truncate.
    compress_rate: float  # 0 ~ 1.0, If zero, not compress or truncate.


class RoleDefinition(TypedDict):
    name: str
    definition: str
    tag: NotRequired[str | None]
