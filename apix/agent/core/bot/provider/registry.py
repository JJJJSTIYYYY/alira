"""Initial catalogue; public access is through BaseProvider."""

from apix.agent.core.bot.provider.anthropic import AnthropicProvider
from apix.agent.core.bot.provider.custom import CustomProvider
from apix.agent.core.bot.provider.deepseek import DeepseekProvider
from apix.agent.core.bot.provider.google import GoogleProvider
from apix.agent.core.bot.provider.moonshot import MoonShotProvider
from apix.agent.core.bot.provider.openai import OpenAIPorvider
from apix.agent.core.bot.provider.xiaomi_mimo import XiaomiMiMOProvider

PROVIDERS = {
    provider.name: provider
    for provider in (
        OpenAIPorvider(),
        DeepseekProvider(),
        MoonShotProvider(),
        XiaomiMiMOProvider(),
        AnthropicProvider(),
        GoogleProvider(),
        CustomProvider(),
    )
}
