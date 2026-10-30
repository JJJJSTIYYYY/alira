"""Named provider and model preset catalogues."""

from alira.agent.core.bot.provider.anthropic import AnthropicProvider
from alira.agent.core.bot.provider.base import BaseProvider
from alira.agent.core.bot.provider.custom import CustomProvider
from alira.agent.core.bot.provider.deepseek import DeepseekProvider
from alira.agent.core.bot.provider.google import GoogleProvider
from alira.agent.core.bot.provider.moonshot import MoonShotProvider
from alira.agent.core.bot.provider.openai import OpenAIPorvider
from alira.agent.core.bot.provider.xiaomi_mimo import XiaomiMiMOProvider

__all__ = [
    "BaseProvider",
    "OpenAIPorvider",
    "DeepseekProvider",
    "MoonShotProvider",
    "XiaomiMiMOProvider",
    "AnthropicProvider",
    "GoogleProvider",
    "CustomProvider",
]
