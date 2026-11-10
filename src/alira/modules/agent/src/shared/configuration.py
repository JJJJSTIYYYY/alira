"""Agent loop configuration shared with modules integrating the agent runtime."""

from typing import TypedDict

from alira.modules.agent.src.shared.identity import AliraIdentity


class Configuration(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    identity: AliraIdentity
