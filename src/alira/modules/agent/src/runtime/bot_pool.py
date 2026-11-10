from typing import ClassVar, Self

from alira.modules.agent.src.core.bot.alira_bot import AliraBot


class BotPool:
    """Process-wide singleton holding user bots."""

    _instance: ClassVar[Self | None] = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        self.bots_pool: dict[str, AliraBot] = {}
        self._initialized = True

    def get_bot(self, provider: str, model: str, api_key: str | None = None) -> AliraBot:
        pass