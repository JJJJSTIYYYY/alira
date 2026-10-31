# Provider / Model 预设及配置依据

核对日期：2026-10-18。预设不是在线模型发现；型号可用性和接口规则发生变化时，应重新核对下列官方资料。
本次提供 6 家官方供应商的 27 个模型，以及由调用方声明配置的 CustomProvider。
不预设未找到官方依据的采样参数、超时、重试次数或输出上限。

## 使用与管理

```python
from alira.agent.core.bot import AliraBot, BaseProvider

bot = AliraBot(provider="moonshot", model="kimi-k2.6", api_key="YOUR_API_KEY")

async def ask():
    async with bot:
        # ModelProfile.max_tokens 的 Parameter.path 将此上限交给 SDK 的 max_tokens 参数。
        return await bot.invoke([{"role": "user", "content": "你好"}], max_tokens=1024)

providers = BaseProvider.list_providers()
models = BaseProvider.get("moonshot").list_models()  # list[my_llm.ModelProfile]
profile = BaseProvider.get("moonshot").get_profile()  # my_llm.ProviderProfile
```

构造参数 `provider`、`model` 只接受字符串。provider 名称忽略大小写及首尾空格，
model ID 精确匹配，不猜测型号、不匹配通配符。未知名称在网络请求前报错。
`bot.provider` 是选中的 BaseProvider 子类实例，`bot.model` 是模型 ID；
原生配置可通过 `bot.llm.provider`、`bot.llm.model`、`bot.llm.resolved_profile` 查看。
接入已按 MyLLM 1.4.2 验证（2026-10-31）：模型 ID、上下文窗口和模型目录使用 `Attribute`，
协议与行为字段使用 `Config`，参数使用 `Parameter`。下表的数据与配置值指 wrapper 的
`default_value`，参数位置使用 `path`，支持状态使用 `supports`。`name` / `source` 由
Profile 自动绑定，不能手动传入；请求参数的 source 为 input，响应参数为 output。
`resolved_profile.get(...)` 返回解析后的配置值；Profile 的 `get_attributes()` /
`get_configs()` / `get_parameters()` 按类型枚举原生声明，不解析继承。
读取目录项的型号与窗口使用 `model.model.default_value` / `model.max_context_window.default_value`。

| 名称 | 实现 | 模型 |
| --- | --- | --- |
| `openai` | `OpenAIPorvider` | `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.4`、`gpt-5.4-pro`、`gpt-5.4-mini` |
| `deepseek` | `DeepseekProvider` | `deepseek-flash`、`deepseek-v4-pro` |
| `moonshot` | `MoonShotProvider` | `kimi-k3`、`kimi-k2.6`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed` |
| `mimo` | `XiaomiMiMOProvider` | `mimo-v2.6-pro`、`mimo-v2.6-pro-ultraspeed`、`mimo-v2.6-flash` |
| `anthropic` | `AnthropicProvider` | `claude-fable-5-1`、`claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-5-5` |
| `google` | `GoogleProvider` | `gemini-3.8-flash`、`gemini-3.6-flash`、`gemini-3.5-flash-lite`、`gemini-3.1-flash-lite`、`gemini-3.1-pro-preview`、`gemini-3.1-pro-preview-customtools`、`gemini-3-flash-preview` |
| `custom` | `CustomProvider` | 初始为空，由调用方注册 |

`OpenAIPorvider` 按本项目要求的公开类名保留这一拼写。
`BaseProvider.get()`、`register()`、`get_profile()` 和 `list_models()` 都隔离嵌套配置，避免修改一个 bot 的配置影响其他 bot。
`get_profile()` 返回包含模型目录的原生配置副本，不解析环境变量或应用调用时的凭据覆盖。
模型增删复用原生 `ProviderProfile.add_models()` / `delete_model()`，配置合并、能力验证、
请求构造、工具关联、SDK 客户端与流状态仍由 MyLLM 负责。
Provider 只负责配置目录和创建 MyLLM；AliraBot 直接调用 `MyLLM.generate()`。
token 字段通过原生 `max_tokens=Parameter(...)` 声明，推理参数按 profile 验证和映射。

### 通用 effort 与上游档位

本地 `low → medium → high → xhigh → max` 按上游支持的强度顺序依次映射。
有效映射必须从 low 开始连续排列；None 只能出现在末尾，不能在更高档位恢复有效值。
`none` 不属于这五个强度档位，按 MyLLM 的不注入绑定语义独立处理。

| 预设 | low | medium | high | xhigh | max |
| --- | --- | --- | --- | --- | --- |
| GPT-6 / 当前 Claude 系列 | low | medium | high | xhigh | max |
| GPT-5.4 / Mini | low | medium | high | xhigh | None |
| GPT-5.4 Pro | medium | high | xhigh | None | None |
| DeepSeek / Kimi K3 | low | high | max | None | None |
| Gemini | low | medium | high | None | None |

表头是调用方的通用档位，表内值是实际发送给上游的档位。
本地 high 可以映射为上游 max；不能通过上游同名参数推断本地档位。
Kimi K2.x / MiMo 没有强度控制，只使用其 profile 中的思考开关绑定。

## 官方依据：每项显式预设

以下列出代码中每项显式赋值的依据。`protocol`、`usage_mode`、`omit` 和
`supports_*` 是 MyLLM 的本地配置名称，不是提交给厂商的新参数；
它们对应的上游 API/能力见各表。
`max_context_window` 只是展示元数据，不计数、不截断，也不作为请求参数。
未显式设置的字段保持 MyLLM / 官方 SDK 的默认值。

### OpenAI

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` / `api_key_env` | `openai_responses` / `https://api.openai.com/v1` / `OPENAI_API_KEY`；[官方 quickstart](https://developers.openai.com/api/docs/quickstart) 展示 Responses 地址与该环境变量。输入输出仍为 Chat，由 MyLLM 转换。 |
| `usage_mode` | `native`：使用 Responses 的标准 usage，经 MyLLM 的原生用量策略输出；[Responses 对象](https://developers.openai.com/api/reference/resources/responses/methods/create) 定义 usage。 |
| GPT-6 `model` / `max_context_window` | 四型号均为 `1_050_000`，见 [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、[6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)、[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)。 |
| GPT-5.4 `model` / `max_context_window` | `gpt-5.4`、`gpt-5.4-pro` 为 `1_050_000`，Mini 为 `400_000`；见 [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4)、[Pro](https://developers.openai.com/api/docs/models/gpt-5.4-pro)、[Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini)。 |
| 推理能力 / `supports_function_calling` | 七型号均为 True；各型号页列出 Reasoning 和 Function calling。使用 Responses 可避免 6.1 Sol 的 Chat 工具限制，并支持 Responses-only 的 5.4 Pro。 |
| `reasoning_effort_mapping` | GPT-6 五档原值，Astra 与 6.1 Sol 不支持上游 none；5.4 / Mini 的本地 max 禁用；5.4 Pro 上游仅 medium/high/xhigh，因此依次映射到本地 low/medium/high，本地 xhigh/max 禁用。原生档位依据为上述各型号页，位置映射遵循本项目的 MyLLM 调用约束。 |

MyLLM 的 `reasoning_effort="none"` 表示不注入 effort 绑定，`reasoning=True` 时则使用 medium；
它不会向上游发送字符串 none，因此不能用它关闭 Astra、6.1 Sol 或 Pro 的思考。
未加入 GPT-5.4 Nano、GPT-5.1 和 GPT-4 系列等已进入[官方弃用计划](https://developers.openai.com/api/docs/deprecations)的旧型号。

### DeepSeek

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` | `openai_chat` / `https://api.deepseek.com`；[模型与接口](https://api-docs.deepseek.com/quick_start/pricing/) 列出 OpenAI 格式地址。 |
| `api_key_env` | `DEEPSEEK_API_KEY`；[官方思考模式示例](https://api-docs.deepseek.com/guides/thinking_mode) 读取该变量。 |
| `model` / `max_context_window` | `deepseek-flash`、`deepseek-v4-pro` 均为 `1_048_576`；[官方模型列表示例](https://api-docs.deepseek.com/zh-cn/api/list-models/) 给出精确整数。 |
| 推理能力 / `supports_function_calling` | 两型号均为 True；[模型与接口](https://api-docs.deepseek.com/quick_start/pricing/) 明确支持思考和工具调用。 |
| `reasoning_effort.supports` / `reasoning_effort` | True / 顶层 `reasoning_effort`；[Chat API](https://api-docs.deepseek.com/api/create-chat-completion/) 支持 low/high/max。 |
| `reasoning_effort_mapping` | 上游原生 low/high/max 依次映射到本地 low/medium/high，本地 xhigh/max 禁用。上述 Chat API 也接受 medium/xhigh 作为 high 的别名；本地预设使用三个原生档位，不额外占用别名档位。 |
| `reasoning_enable` | `thinking.type=enabled`，以 Parameter 替换库继承的默认 medium 绑定；[思考模式](https://api-docs.deepseek.com/guides/thinking_mode) 展示 OpenAI SDK extra_body 写法。 |
| `feedback_reasoning_content` | `when_tool_calls`；[思考模式工具回合](https://api-docs.deepseek.com/guides/thinking_mode#tool-calls) 要求携带 tools 的后续请求保留历史 reasoning_content。 |
| `max_tokens` Parameter 的 path | ProviderProfile 设置 `$.max_tokens`；[Chat API](https://api-docs.deepseek.com/api/create-chat-completion/) 定义 max_tokens。由 MyLLM 写入该 SDK 参数，替代 Chat 默认的 max_completion_tokens。 |

当前目录已覆盖[官方模型表](https://api-docs.deepseek.com/quick_start/pricing/)的两个型号。
`deepseek-v4-flash` 等旧 ID 已转发至 `deepseek-flash`，不作为额外模型重复预设。

### MoonShot / Kimi

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` / `api_key_env` | `openai_chat` / `https://api.moonshot.ai/v1` / `MOONSHOT_API_KEY`；[官方 quickstart](https://platform.kimi.ai/docs/overview) 的 OpenAI SDK 示例。 |
| `model` | K3、K2.6、K2.7 Code 与 highspeed 四个 ID；[当前型号列表](https://platform.kimi.ai/docs/models)。 |
| `max_context_window` | K3 为 `1_048_576`，见 [Moonshot 官方模型卡](https://huggingface.co/moonshotai/Kimi-K3)；K2.6 / K2.7 Code 为 `262_144`，见官方 [K2.6 配置](https://huggingface.co/moonshotai/Kimi-K2.6/raw/main/config.json)、[K2.7 配置](https://huggingface.co/moonshotai/Kimi-K2.7-Code/raw/main/config.json)。[型号参数说明](https://platform.kimi.ai/docs/api/models-overview) 确认 highspeed 与 Code 参数相同。 |
| 推理能力 / `supports_function_calling` | 四型号均 True；[官方参数与工具说明](https://platform.kimi.ai/docs/api/chat)。 |
| `feedback_reasoning_content` | `when_tool_calls`；同上文档要求多轮对话及工具回合保留历史 reasoning_content。 |
| K3 `reasoning_effort.supports` / `reasoning_effort` / `reasoning_effort_mapping` | True / 顶层 reasoning_effort；[官方参数表](https://platform.kimi.ai/docs/api/models-overview)允许上游 low/high/max，依次映射到本地 low/medium/high，本地 xhigh/max 禁用。位置映射遵循本项目的 MyLLM 调用约束，不表示厂商新增档位。 |
| K3 `reasoning_enable` | omit 去掉 Chat 默认的 medium 绑定，再由 MyLLM 根据映射生成合法的上游 effort。仅传 `reasoning=True` 时，库默认 medium 经 profile 映射为 high；未设置推理选项时不注入 effort，沿用[服务端默认 max](https://platform.kimi.ai/docs/guide/use-reasoning-effort)。 |
| K2.x `reasoning_enable` | Parameter 替换 OpenAI reasoning_effort 绑定，开启时发送 `thinking={type:enabled,keep:all}`；[参数对照表](https://platform.kimi.ai/docs/api/models-overview) 列出 K2.6 可选 preserved thinking，K2.7 必须保留且不可关闭。 |
| `max_tokens` Parameter 的 path | 三个 K2.x ModelProfile 设置 `$.max_tokens`，见 [K2.6 官方调用示例](https://platform.kimi.ai/docs/api/models-overview)；K3 继承库默认的 `$.max_completion_tokens`，见 [Chat API](https://platform.kimi.ai/docs/api/chat)。 |

K2.5、K2、moonshot-v1 已在[官方型号页](https://platform.kimi.ai/docs/models)标为停用，因此不预设。

### Xiaomi MiMo

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` / `api_key_env` | `openai_chat` / `https://api.xiaomimimo.com/v1` / `MIMO_API_KEY`；官方 [V2.6 Pro](https://mimo.mi.com/models/en-US/mimo-v2.6-pro)、[Pro UltraSpeed](https://mimo.mi.com/models/en-US/mimo-v2.6-pro-ultraspeed)、[V2.6 Flash](https://mimo.mi.com/models/en-US/mimo-v2.6-flash) 均展示 OpenAI SDK 调用示例。 |
| `model` / `max_context_window` | 三个 ID 同上；均为 1M。整数 `1_048_576` 沿用[官方上下文配置](https://mimo.mi.com/docs/zh-CN/tokenplan/integration/codex-configuration)的 1M 口径；UltraSpeed 规格声明保留 Pro 完整能力与相同窗口。 |
| 推理能力 / `supports_function_calling` | 三型号均 True；三份模型规格列出 Deep Thinking 与 Tool Call。 |
| `reasoning_enable` | `thinking.type=enabled`，Parameter 替换 OpenAI 默认 reasoning_effort 绑定；[思考模式](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/text-generation/deep-thinking) 展示该请求字段。 |
| `feedback_reasoning_content` | `when_tool_calls`；同上思考模式文档给出多轮工具调用时回传 reasoning_content 的示例。 |
| token 参数 | 使用库原生 max_completion_tokens；三份模型页示例均使用这一字段，不另做 max_tokens 改写。 |

MiMo 2.5 系列即将停用，见[官方型号说明](https://mimo.mi.com/docs/quick-start/model)，默认预设选择 2.6。

### Anthropic

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` / `api_key_env` | `anthropic` / `https://api.anthropic.com` / `ANTHROPIC_API_KEY`；[API overview](https://platform.claude.com/docs/en/api/overview) 定义 Messages API 地址，[SDK 示例](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python) 使用该环境变量。 |
| `usage_mode` | `native`：读取 Messages 的标准 usage；[Messages API](https://platform.claude.com/docs/en/api/messages/create)。 |
| `model` / `max_context_window` | 四个 ID 见[当前模型目录](https://platform.claude.com/docs/en/models/overview)，均为官方标示的 1M（本地元数据为 `1_000_000`）；详见 [Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/overview)、[Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview)、[Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview)、[Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)。 |
| 推理能力 / `supports_function_calling` | 四型号均 True；上述目录列出 adaptive thinking 与 tool use；[工具调用指南](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) 定义工具协议。 |
| `reasoning_effort.supports` | True；[effort 表](https://platform.claude.com/docs/en/build-with-claude/effort) 确认四型号支持 low/medium/high/xhigh/max，沿用 MyLLM 的原值映射。 |
| `extra_body` | `thinking={type:adaptive}`；[思考控制](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost) 定义 adaptive thinking。同时以 reasoning_enable=omit 禁用库旧式 enabled/budget_tokens 自动绑定，保持 adaptive 配置。 |
| `max_tokens` | 4096 是应用的初始输出预算，采用同上官方示例值；它不是型号最大输出能力，调用时可覆盖。Messages API 要求此参数，因此在 ModelProfile 配置，不复制原生参数转换逻辑。 |

Fable 5.1 与 Opus 5.5 为始终开启的 adaptive thinking；不为它们伪造关闭参数。
本次补齐官方主目录的当前系列，不加入受限的 specialized 型号或展开所有历史版本。

### Google

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `api_key_env` | `gemini` / `GEMINI_API_KEY`；[官方认证说明](https://ai.google.dev/gemini-api/docs/api-key) 支持该变量。保持 SDK 官方默认服务地址。 |
| `usage_mode` | `native`：读取 GenerateContent 的 usageMetadata；[GenerateContent API](https://ai.google.dev/api/generate-content)。 |
| 稳定版 `model` / `max_context_window` | 四型号均为 `1_048_576`；见 [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[3.6 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)、[3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)、[3.1 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite) 的 Input token limit。 |
| 预览版 `model` / `max_context_window` | 三型号均为 `1_048_576`；见 [3.1 Pro / customtools](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview)、[3 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview)。customtools 是官方为自定义工具工作流提供的独立端点。 |
| 推理能力 / `supports_function_calling` | 七型号均 True；上述模型页列出 Thinking 和 Function calling。 |
| `reasoning_effort.supports` / `reasoning_effort_mapping` | True / low、medium、high 原值，xhigh/max 禁用；见[当前 thinkingLevel 表](https://ai.google.dev/gemini-api/docs/generate-content/thinking)及包含 3.1 Pro 的[Gemini 3.x 参数表](https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.5#new-default-effort-level-medium)。 |

Gemini 的 thinking_config 和 max_output_tokens 由 MyLLM 原生 Gemini Decoder 生成，
项目不复制 GenerateContent 的参数映射。
预览状态保留在原始 ID 中；[生命周期表](https://ai.google.dev/gemini-api/docs/deprecations)尚未宣布这三个预览端点的停用日期。
不重复加入自动转发的 3.5 / 3.7 Flash，也不加入只向老用户开放的 2.5 系列。

## 应用约定与兼容边界

- 本地 provider 名称是 Alira 的注册键，不是新的厂商参数。
- 显式 api_key 优先于 profile 的 Parameter.default_value，再读取对应厂商环境变量；覆盖凭据时保留 Parameter 的认证路径。`api_key=omit` 禁用自动凭据读取，显式传入密钥时使用协议默认路径。Ollama 默认使用 Authorization 头，调用方需提供完整的 Bearer 前缀。内置 provider 缺少凭据时报错，避免兼容 OpenAI SDK 意外读取另一家厂商的 OPENAI_API_KEY。
- `usage_mode=Config(default_value="native", ...)`、能力标记、Parameter 路径和 omit 的语义来自 [my-llm-py 配置指南](https://github.com/JJJJSTIYYYY/my-llm/blob/main/docs/reference/configuration.md)。它们在本地配置，实际请求仍使用上表的官方原生字段。成功调用后通过 `bot.llm.get_last_usage()` 读取用量，公开事件和结果不含 usage。
- `max_tokens` Parameter 的 path 从 SDK 参数字典根部开始；`$.max_tokens` 直接传 SDK 参数，`$.extra_body.max_tokens` 经 extra_body 传入。路径由库解释，项目不维护转换层。不传调用参数时，仅在原生目标字段缺失时补 profile 默认预算；显式正整数覆盖目标值，`max_tokens=None` 写入实际空值，`max_tokens=omit` 删除本次目标键。Profile 的 `max_tokens=None` 继承整份 Parameter，`max_tokens=omit` 禁用自动写入并保留手动原生字段。输入参数不被修改。
- `reasoning=True` 或非 none 的强度会触发自动生成的思考参数；开启参数和映射后的 effort 优先于同路径的原生 extra_body 值。普通默认字段仍仅在缺失时补值。
- SDK 客户端在 bot 内复用；所有请求完成后 `await bot.aclose()`，或使用 `async with bot`。流自身的 `async with` 关闭本次响应，不关闭共享客户端。
- `reasoning=False` 按 MyLLM 语义表示本次不注入开启思考的绑定，不等于厂商一定关闭默认思考。需要显式控制时，使用该型号支持的原生 extra_body；K3 与 K2.7 不支持关闭思考，不能伪造关闭参数。
- 图片输入须在 ModelProfile 显式声明 `supports_vision=Config(default_value=True)`；默认不支持，当前内置预设尚未声明。跨协议无法表达的支持内容或禁用能力使用 `SemanticLossError`，保持库默认的 raise 策略。
- 当前内置型号经官方 SDK + 本地 HTTP transport 验证请求序列化与资源释放；没有使用付费 API 或验证账户权限。

## 自定义服务

CustomProvider 不猜测远端模型能力；由调用方提供有文档依据的原生配置，
并显式声明 `protocol=Config(default_value=...)`，ProviderProfile 没有默认协议。
`reasoning_enable=Parameter(..., supports=False)` 拒绝推理请求；
`reasoning_effort=Parameter(..., supports=False)` 禁用自动强度绑定。
`reasoning_enable=omit` 仅停止自动开启参数写入，仍可通过独立的强度参数或原生配置控制推理。

```python
from my_llm import Attribute, Config, ModelProfile, Parameter, ProviderProfile, omit
from alira.agent.core.bot import AliraBot, BaseProvider, CustomProvider

custom = CustomProvider(
    name="my-service",
    api_key_env="MY_SERVICE_API_KEY",
    profile=ProviderProfile(
        protocol=Config(default_value="openai_chat"),
        base_url=Config(default_value="https://your-service.example/v1"),
        max_tokens=Parameter(
            path="$.max_tokens", default_value=omit, comment="输出上限",
        ),  # 若服务使用 max_tokens；其他服务按文档设置。
        models=Attribute(default_value=[ModelProfile(
            model=Attribute(default_value="your-model"),
            supports_function_calling=Config(default_value=True),
        )]),
    ),
)
BaseProvider.register(custom)
bot = AliraBot(provider="my-service", model="your-model")
```

更改已有配置时，先获取隔离副本，使用库的模型目录方法，再显式替换注册项：

```python
provider = BaseProvider.get("my-service")
provider.profile.add_models(ModelProfile(model=Attribute(default_value="another-model")))
BaseProvider.register(provider, replace_existing=True)
# 已构造 bot 使用原有快照；新 bot 读取新目录。
```
