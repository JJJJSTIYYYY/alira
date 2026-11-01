from typing import TypedDict

from alira.interface.utils.identiity import AliraIdentity


class Config(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    identity: AliraIdentity
    # Add other configuration fields as needed


class ConfigCenter:
    """
    Centralized configuration management for the Alira agent loop.
    """

    def __init__(self):
        self._config: dict[AliraIdentity, Config] = {}

    def set_config(self, identity: AliraIdentity, value: Config):
        """
        Set a configuration value.

        :param identity: The Alira identity.
        :param value: The configuration value.
        """
        self._config[identity] = value

    def get_config(self, identity: AliraIdentity) -> Config | None:
        """
        Get a configuration value.

        :param identity: The Alira identity.
        :return: The configuration value or None if the identity does not exist.
        """
        return self._config.get(identity)