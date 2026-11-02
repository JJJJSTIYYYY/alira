from alira.common.type.configuration import Configuration
from alira.common.type.identiity import AliraIdentity


class ConfigCenter:
    """
    Centralized configuration management for the Alira agent loop.
    """

    def __init__(self):
        self._config: dict[AliraIdentity, Configuration] = {}

    def set_config(self, identity: AliraIdentity, value: Configuration):
        """
        Set a configuration value.

        :param identity: The Alira identity.
        :param value: The configuration value.
        """
        self._config[identity] = value

    def get_config(self, identity: AliraIdentity) -> Configuration | None:
        """
        Get a configuration value.

        :param identity: The Alira identity.
        :return: The configuration value or None if the identity does not exist.
        """
        return self._config.get(identity)