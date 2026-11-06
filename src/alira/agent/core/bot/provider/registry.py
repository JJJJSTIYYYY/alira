"""Initial catalogue; public access is through BaseProvider."""

from alira.agent.core.bot.provider.anthropic import AnthropicProvider
from alira.agent.core.bot.provider.custom import CustomProvider
from alira.agent.core.bot.provider.deepseek import DeepseekProvider
from alira.agent.core.bot.provider.moonshot import MoonShotProvider
from alira.agent.core.bot.provider.openai import OpenAIPorvider
from alira.agent.core.bot.provider.xiaomi_mimo import XiaomiMiMOProvider

PROVIDERS = {
    provider.name: provider
    for provider in (
        OpenAIPorvider(),
        DeepseekProvider(),
        MoonShotProvider(),
        XiaomiMiMOProvider(),
        AnthropicProvider(),
        CustomProvider(),
    )
}
