"""Named provider and model preset catalogues."""

from apix.agent.core.bot.provider.anthropic import AnthropicProvider
from apix.agent.core.bot.provider.base import BaseProvider
from apix.agent.core.bot.provider.custom import CustomProvider
from apix.agent.core.bot.provider.deepseek import DeepseekProvider
from apix.agent.core.bot.provider.google import GoogleProvider
from apix.agent.core.bot.provider.moonshot import MoonShotProvider
from apix.agent.core.bot.provider.openai import OpenAIPorvider
from apix.agent.core.bot.provider.xiaomi_mimo import XiaomiMiMOProvider

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
