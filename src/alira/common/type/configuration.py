from typing import TypedDict

from alira.common.type.identiity import AliraIdentity


class Configuration(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    identity: AliraIdentity
    # Add other configuration fields as needed