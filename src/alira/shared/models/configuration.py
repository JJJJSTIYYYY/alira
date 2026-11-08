"""Per-user agent configuration, separate from shared.application.config settings."""

from typing import TypedDict

from alira.shared.models.identity import AliraIdentity


class Configuration(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    identity: AliraIdentity
