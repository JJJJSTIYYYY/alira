# Provider / Model 预设及配置依据

配置与同协议验证日期：2026-11-06。厂商依据沿用 2026-11-03 的核对；预设不是在线模型发现。
型号可用性和接口规则发生变化时，应重新核对下列官方资料。
本次提供 5 家官方供应商的 22 个模型，以及由调用方声明配置的 CustomProvider。
不额外指定未找到官方依据的采样参数、超时、重试次数或输出上限。

## 使用与管理

```python
from my_llm import TextContent, UserMessage
from alira.modules.agent.core.bot import AliraBot, BaseProvider

bot = AliraBot(provider="moonshot", model="kimi-k2.6", api_key="YOUR_API_KEY")

async def ask():
    async with bot:
        # ModelProfile.max_tokens 的 Parameter.path 将此上限交给 SDK 的 max_tokens 参数。
        return await bot.invoke(
            [UserMessage(role="user", content=[TextContent("你好")])], max_tokens=1024
        )

providers = BaseProvider.list_providers()
models = BaseProvider.get("moonshot").list_models()  # list[my_llm.ModelProfile]
profile = BaseProvider.get("moonshot").get_profile()  # my_llm.ProviderProfile
```

构造参数 `provider`、`model` 只接受字符串。provider 名称忽略大小写及首尾空格，
model ID 精确匹配，不猜测型号、不匹配通配符。未知名称在网络请求前报错。
`bot.provider` 是选中的 BaseProvider 子类实例，`bot.model` 是模型 ID；
原生配置可通过 `bot.llm.provider`、`bot.llm.model`、`bot.llm.resolved_profile` 查看。
接入使用 MyLLM 2.0.1：模型 ID、上下文窗口和模型目录使用 `Attribute`，
协议与行为字段使用 `Config`，输入和输出参数分别使用 `InputParameter` 与 `OutputParameter`。
两者继承公共基类 `Parameter`；直接构造基类不能声明参数方向。下表的数据与配置值指 wrapper 的
`default_value`，参数位置使用 `path`，支持状态使用 `supports`。`name` / `source` 由
Profile 自动绑定，不能手动传入；请求参数的 source 为 input，响应参数为 output。
`resolved_profile.get(...)` 返回解析后的配置值；Profile 的 `get_attributes()` /
`get_configs()` / `get_parameters()` 按类型枚举原生声明，不解析继承。
读取目录项的型号与窗口使用 `model.model.default_value` / `model.max_context_window.default_value`。

| 名称 | 实现 | 模型 |
| --- | --- | --- |
| `openai` | `OpenAIPorvider` | `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.4`、`gpt-5.4-pro`、`gpt-5.4-mini` |
| `deepseek` | `DeepseekProvider` | `deepseek-flash`、`deepseek-v4-pro` |
| `moonshot` | `MoonShotProvider` | `kimi-k3`、`kimi-k3.1-pro`、`kimi-k3.1-pro-highspeed`、`kimi-k2.6`、`kimi-k2.7-code`、`kimi-k2.7-code-highspeed` |
| `mimo` | `XiaomiMiMOProvider` | `mimo-v2.6-pro`、`mimo-v2.6-pro-ultraspeed`、`mimo-v2.6-flash` |
| `anthropic` | `AnthropicProvider` | `claude-fable-5-1`、`claude-opus-5-5`、`claude-sonnet-5-5`、`claude-haiku-5-5` |
| `custom` | `CustomProvider` | 初始为空，由调用方注册 |

`OpenAIPorvider` 按本项目要求的公开类名保留这一拼写。
`BaseProvider.get()` 和 `register()` 复制嵌套配置，隔离注册项与调用方。
`get_profile()` 返回当前 provider 持有的原生配置；`list_models()` 返回新列表并复用其中的模型对象。
构造 MyLLM 时由库复制配置形成快照，调用时的凭据覆盖不修改 provider。
模型增删复用原生 `ProviderProfile.add_models()` / `delete_model()`，配置合并、能力验证、
请求构造、工具关联、SDK 客户端与流状态仍由 MyLLM 负责。
Provider 只负责配置目录和创建 MyLLM；AliraBot 直接调用 `MyLLM.generate()`。
token 字段通过原生 `max_tokens=InputParameter(...)` 声明，推理参数按 profile 验证和映射。

### 完整 Profile 配置

所有内置 `ProviderProfile` 的 18 个字段、`ModelProfile` 的 16 个字段均在源码中显式列出，
每个模型通过 `ModelProfile.protocol=Config(default_value=...)` 声明上游协议；
Parameter 显式声明 `path`、`default_value` 和 `supports`。
模型字段的 `None` 表示继承本项目已声明的 provider 配置，继承后的声明为
Config、Parameter、Attribute 或 omit。provider 的 `protocol=None` 显式留空，由每个模型提供协议。
需要空值时使用 wrapper 的
`default_value=None`，不依赖 MyLLM 的协议默认值。
`name`、`source` 仍由 MyLLM 绑定，不手动赋值。

| ProviderProfile 字段 | 显式配置 |
| --- | --- |
| `protocol` | `None`；全部内置型号在 ModelProfile 中声明协议 |
| `base_url` | 各厂商地址 |
| `api_key` | `InputParameter(path="$.api_key", default_value=omit, supports=True)`，不预置密钥 |
| `timeout` | `Config(default_value=None)`，不向 SDK 注入超时 |
| `max_retries` | `Config(default_value=None)`，不向 SDK 注入重试次数 |
| `supports_stream` | `Config(default_value=True)` |
| `usage_mode` | OpenAI、Anthropic 为 `native`；其余为 `none` |
| `extra_headers` | `Config(default_value={})` |
| `extra_body` | 各厂商的可见推理默认值，见下表；Kimi 由模型分别声明 |
| `max_tokens` | 下表中的协议绑定；默认值为 omit，模型可覆盖 |
| `content` | 下表中的输出文本路径，默认值为 omit |
| `reasoning_content` | 下表中的输出推理路径，默认值为 omit |
| `feedback_reasoning_item` | `OutputParameter`，提取可原样回放的原生推理项；路径见下表 |
| `reasoning_enable` | 下表中的开启绑定，按调用参数注入 |
| `reasoning_effort` | 下表中的强度绑定，按调用参数注入 |
| `reasoning_effort_mapping` | 显式列出 none、low、medium、high、xhigh、max；型号差异见后续 effort 表 |
| `should_feedback_reasoning_item` | MiMo 为 `when_tool_calls`，其余为 `always` |
| `models` | `Attribute` 包含完整的型号目录 |

下表列出 provider 层的路径和开启值；模型覆盖在各厂商章节说明。
除标记为不支持的强度绑定外，所有 Parameter 的 `supports` 均为 True。

| 字段 | OpenAI Responses | DeepSeek | Kimi | MiMo | Anthropic |
| --- | --- | --- | --- | --- | --- |
| `max_tokens` | `$.max_output_tokens` | 同 OpenAI | 同 OpenAI；K2.x 覆盖为 `$.max_tokens` | 同 OpenAI | `$.max_tokens` |
| `content` | `$.delta` | 同 OpenAI | 同 OpenAI；K2.x 覆盖为 `$.choices[0].delta.content` | 同 OpenAI | `$.delta.text` |
| `reasoning_content` | `$.delta` | 同 OpenAI | 同 OpenAI；K2.x 覆盖为 `$.choices[0].delta.reasoning_content` | 同 OpenAI | `$.delta.thinking` |
| `feedback_reasoning_item` | `$.item` | 同 OpenAI | 同 OpenAI；K2.x 覆盖为 `$.choices[0].delta.reasoning_content` | 同 OpenAI | `$.content_block` |
| `reasoning_enable` | `$.reasoning`，`{effort:medium,summary:detailed}` | `$.reasoning.effort`，high | omit；K2.x 为 `$.extra_body.thinking`，`{type:enabled,keep:all}` | `$.reasoning.effort`，medium | `$.thinking`，`{type:adaptive,display:summarized}` |
| `reasoning_effort` | `$.reasoning.effort` | 同 OpenAI | 同 OpenAI；K2.x 覆盖为 `$.reasoning_effort` 且不支持 | 同 OpenAI，不支持 | `$.output_config.effort` |

| ModelProfile 字段 | 显式配置 |
| --- | --- |
| `model` / `max_context_window` | 各型号 ID 和展示窗口 |
| `protocol` | 各型号使用的上游协议，见下表 |
| `supports_function_calling` / `supports_vision` / `supports_message_name` | function calling 与 message name 为 True；vision 仅 DeepSeek Flash 为 True，其余为 False |
| `extra_headers` / `extra_body` | 通常为 `None`；Kimi 按型号显式配置 Responses effort 或 Chat thinking |
| `max_tokens` | 通常为 `None`；K2.x 覆盖路径，Claude 显式设置 4096 |
| `content` / `reasoning_content` | 通常为 `None`；Kimi K2.x 显式覆盖 Chat 输出路径 |
| `feedback_reasoning_item` | 通常为 `None`；Kimi K2.x 显式覆盖 Chat 推理字段路径 |
| `reasoning_enable` / `reasoning_effort` | 通常为 `None`；Kimi K2.x 覆盖 Chat 参数绑定 |
| `reasoning_effort_mapping` | 通常为 `None`；OpenAI 型号差异显式覆盖，Kimi K2.x 为 omit |
| `should_feedback_reasoning_item` | `None`，继承 provider |

| 模型 | `ModelProfile.protocol` |
| --- | --- |
| OpenAI 七款、DeepSeek 两款、MiMo 三款 | `openai_responses` |
| Kimi K3、K3.1 Pro、K3.1 Pro Highspeed | `openai_responses` |
| Kimi K2.6、K2.7 Code、K2.7 Code Highspeed | `openai_chat` |
| Claude 四款 | `anthropic` |

协议由所选模型解析，连接地址与凭据仍属于 provider。MyLLM 先解析模型协议，
再应用该协议、provider 与模型的字段配置；切换协议的模型也显式覆盖 token、
文本、推理及控制参数的路径，避免继承另一协议的请求字段。

CustomProvider 没有默认协议，未传 profile 时使用模型目录为空的原生 `ProviderProfile()`。
调用方传入的 profile 保持原样，不套用任何内置厂商配置。

`timeout` 和 `max_retries` 使用 `Config(default_value=None)`，
解析结果保留显式空值，SDK 不接收对应选项，继续使用 SDK 默认行为。

### 默认可见推理

预设通过原生 `extra_body` 请求可见摘要或原文，未传 `reasoning` 选项时也保留这些配置。
`reasoning_enable` 仍负责调用方显式开启推理时的参数注入，映射后的 effort 可以覆盖同路径的默认值。

| 模型 | 默认请求配置 | 厂商返回形式 |
| --- | --- | --- |
| OpenAI | `reasoning={effort:medium,summary:detailed}` | 可见推理摘要；不是完整内部推理 |
| DeepSeek | `reasoning.effort=high` | 原始 `reasoning_text` |
| Kimi K3 系列 | 模型级 `reasoning.effort=max` | `summary_text` |
| Kimi K2.x | 模型级 `thinking={type:enabled,keep:all}` | `reasoning_content` |
| MiMo | `reasoning.effort=medium` | 原始 `reasoning_text` |
| Claude | `thinking={type:adaptive,display:summarized}` | 可见推理摘要，另带回放签名 |

MyLLM 2.0.1 已在同协议路径中通过原始推理的流式、完整结果及工具回放测试：
模型上游协议为 `openai_responses` 时，DeepSeek / MiMo 的
`response.reasoning_text.delta` 可以提取为可见推理，最终结果保留原生 reasoning 项中的
`content`、`summary`、`encrypted_content` 和 ID；下一轮工具结果回传保留原项。
测试通过官方 SDK 和本地 HTTP transport 执行，没有调用真实厂商服务。

`reasoning_content` 用于显示文本，`feedback_reasoning_item` 用于回放原生数据；
`should_feedback_reasoning_item` 决定哪些历史项回传。DeepSeek 的 `always` 保留普通历史与工具轮次，
MiMo 的 `when_tool_calls` 仅在工具轮次回传。仅有加密数据的项也会保留，不会伪造可见文本。

AliraBot 固定使用 middleware 输入输出；原生推理保留为 `ReasoningContent` 的
`reasoning` 和 `feedback_item`，由同协议 decoder 按 Profile 回放。
Chat 输入语义损失使用库预置 `chat_semantic_loss_hook`；输出与其他协议的损失统一忽略。
调用示例见[模型集成文档](my-llm-integration.md#同协议调用与推理回放)。

### 通用 effort 与上游档位

本地 `low → medium → high → xhigh → max` 按上游支持的强度顺序依次映射。
有效映射必须从 low 开始连续排列；None 只能出现在末尾，不能在更高档位恢复有效值。
`none` 不属于这五个强度档位，按 MyLLM 的不注入绑定语义独立处理。

| 预设 | low | medium | high | xhigh | max |
| --- | --- | --- | --- | --- | --- |
| GPT-6 / 当前 Claude 系列 | low | medium | high | xhigh | max |
| GPT-5.4 / Mini | low | medium | high | xhigh | None |
| GPT-5.4 Pro | medium | high | xhigh | None | None |
| DeepSeek / Kimi K3 系列 | low | high | max | None | None |

表头是调用方的通用档位，表内值是实际发送给上游的档位。
本地 high 可以映射为上游 max；不能通过上游同名参数推断本地档位。
Kimi K2.x / MiMo 没有强度控制，只使用其 profile 中的思考开关绑定。

## 官方依据：每项显式预设

以下列出代码中每项显式赋值的依据。`protocol`、`usage_mode`、`omit` 和
`supports_*` 是 MyLLM 的本地配置名称，不是提交给厂商的新参数；
它们对应的上游 API/能力见各表。
`max_context_window` 只是展示元数据，不计数、不截断，也不作为请求参数。
MyLLM 的协议配置已在本项目完整声明；连接选项的 `Config(default_value=None)` 继续由官方 SDK 决定。

### OpenAI

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` | `openai_responses` / `https://api.openai.com/v1`；[官方 quickstart](https://developers.openai.com/api/docs/quickstart) 展示 Responses 地址。输入输出为 middleware，由 MyLLM 转换。 |
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
| `protocol` / `base_url` | 两个模型均为 `openai_responses` / `https://api.deepseek.com`；[Responses 指南](https://api-docs.deepseek.com/guides/responses_api/) 展示 `client.responses.create(...)`。 |
| `model` / `max_context_window` | `deepseek-flash`、`deepseek-v4-pro` 均为 `1_048_576`；[官方模型列表示例](https://api-docs.deepseek.com/zh-cn/api/list-models/) 给出精确整数。 |
| 推理能力 / `supports_function_calling` | 两型号均为 True；[模型与接口](https://api-docs.deepseek.com/quick_start/pricing/) 明确支持思考和工具调用。 |
| `reasoning_effort.supports` / `reasoning_effort` | True / `$.reasoning.effort`；[Responses API](https://api-docs.deepseek.com/api/create-response/) 支持 low/high/max。 |
| `reasoning_effort_mapping` | 上游原生 low/high/max 依次映射到本地 low/medium/high，本地 xhigh/max 禁用；本地预设不额外占用兼容别名档位。 |
| 可见推理 | 原生 reasoning 项的 `content` 包含 `{type:reasoning_text,text:...}`，流式事件为 `response.reasoning_text.delta`；[Responses 指南](https://api-docs.deepseek.com/guides/responses_api/) 明确 `summary` 被接受但不生成摘要，因此不注入 OpenAI 的 `summary=detailed`。 |
| `reasoning_enable` | `$.reasoning.effort` 的默认值为 high，与迁移前开启思考且未指定 effort 时的上游默认强度一致。 |
| `should_feedback_reasoning_item` | 显式 `always`，同协议回放完整原生 reasoning 项，包含原始推理文本。 |
| `max_tokens` Parameter 的 path | `$.max_output_tokens`，由 MyLLM 写入 Responses SDK 参数，见上述 Responses API。 |

当前目录已覆盖[官方模型表](https://api-docs.deepseek.com/quick_start/pricing/)的两个型号。
`deepseek-v4-flash` 等旧 ID 已转发至 `deepseek-flash`，不作为额外模型重复预设。

### MoonShot / Kimi

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` | K3 系列三款为 `openai_responses`，K2.x 三款保留 `openai_chat`；共同地址为 `https://api.moonshot.ai/v1`。见 [Responses API](https://platform.kimi.ai/docs/api/responses) 与 [Chat API](https://platform.kimi.ai/docs/api/chat)。 |
| `model` | K3、K3.1 Pro、K3.1 Pro Highspeed、K2.6、K2.7 Code 与 highspeed 六个 ID；见[当前型号列表](https://platform.kimi.ai/docs/models)与 [K3.1 Pro 指南](https://platform.kimi.com/docs/guide/kimi-k3-1-pro-quickstart)。 |
| `max_context_window` | K3 系列为 `1_048_576`，K3.1 Pro 两款沿用其官方 1M 窗口与 [K3 模型卡](https://huggingface.co/moonshotai/Kimi-K3)的整数口径；K2.6 / K2.7 Code 为 `262_144`，见官方 [K2.6 配置](https://huggingface.co/moonshotai/Kimi-K2.6/raw/main/config.json)、[K2.7 配置](https://huggingface.co/moonshotai/Kimi-K2.7-Code/raw/main/config.json)。[型号参数说明](https://platform.kimi.ai/docs/api/models-overview) 确认 highspeed 与 Code 参数相同。 |
| 推理能力 / `supports_function_calling` | 六型号均 True；上述型号指南与[官方参数与工具说明](https://platform.kimi.ai/docs/api/chat)。 |
| `should_feedback_reasoning_item` | 显式 `always`；[Preserved Thinking](https://platform.kimi.ai/docs/guide/use-thinking-models#preserve-thinking-across-turns-preserved-thinking) 要求 K3、K2.7 和开启 `keep=all` 的 K2.6 完整回传每条历史 assistant 消息的 reasoning_content。 |
| K3 系列 `reasoning_effort.supports` / `reasoning_effort` / `reasoning_effort_mapping` | True / `$.reasoning.effort`；[官方参数表](https://platform.kimi.ai/docs/api/models-overview)和 K3.1 Pro 指南允许上游 low/high/max，依次映射到本地 low/medium/high，本地 xhigh/max 禁用。位置映射遵循本项目的 MyLLM 调用约束，不表示厂商新增档位。 |
| K3 系列可见推理 | Responses reasoning 项的 `summary` 包含 `{type:summary_text,text:...}`，流式事件为 `response.reasoning_summary_text.delta`；请求只声明 `reasoning.effort`，不添加未文档化的 `summary` 或 `display` 开关。见 [Responses API](https://platform.kimi.ai/docs/api/responses)及[官方 OpenAPI schema](https://platform.kimi.com/docs/openapi.json)。 |
| K3 系列 `reasoning_enable` | omit 禁用自动开启绑定，强度仍由独立的 `$.reasoning.effort` 注入。仅传 `reasoning=True` 时，库默认 medium 经 profile 映射为 high；未指定推理选项时由模型级 extra_body 显式发送上游默认 max。K3 系列始终思考。 |
| K2.x `reasoning_enable` | Parameter 替换 OpenAI reasoning_effort 绑定，开启时发送 `thinking={type:enabled,keep:all}`；[参数对照表](https://platform.kimi.ai/docs/api/models-overview) 列出 K2.6 可选 preserved thinking，K2.7 必须保留且不可关闭。 |
| `max_tokens` Parameter 的 path | K3 系列为 `$.max_output_tokens`；K2.x 为 `$.max_tokens`，分别见上述 Responses API 与 [K2.6 官方调用示例](https://platform.kimi.ai/docs/api/models-overview)。 |

K2.5、K2、moonshot-v1 已在[官方型号页](https://platform.kimi.ai/docs/models)标为停用，因此不预设。

### Xiaomi MiMo

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` | 三个模型均为 `openai_responses` / `https://api.xiaomimimo.com/v1`；[官方 Responses 兼容接口](https://mimo.mi.com/docs/en-US/api/chat/responses) 列出三款模型与 `/v1/responses` 地址。 |
| `model` / `max_context_window` | [V2.6 Pro](https://mimo.mi.com/models/en-US/mimo-v2.6-pro)、[Pro UltraSpeed](https://mimo.mi.com/models/en-US/mimo-v2.6-pro-ultraspeed)、[V2.6 Flash](https://mimo.mi.com/models/en-US/mimo-v2.6-flash) 均为 1M。整数 `1_048_576` 沿用[官方上下文配置](https://mimo.mi.com/docs/zh-CN/tokenplan/integration/codex-configuration)的 1M 口径；UltraSpeed 规格声明保留 Pro 完整能力与相同窗口。 |
| 推理能力 / `supports_function_calling` | 三型号均 True；三份模型规格列出 Deep Thinking 与 Tool Call。 |
| `reasoning_enable` | 使用 Responses 的 `reasoning.effort=medium` 开启思考；官方接口规定 none 关闭，其他合法值均开启且目前没有强度差异，因此不把这些值声明成有效的强度档位。 |
| 可见推理 | 原生返回推理文本，流式事件为 `response.reasoning_text.delta`；官方请求 schema 未声明 `reasoning.summary`，因此不添加该字段。 |
| `should_feedback_reasoning_item` | 保留 `when_tool_calls`；同协议工具轮次回放原生 reasoning 项，普通无工具轮次不回放。 |
| token 参数 | `$.max_output_tokens`；上述 Responses API 将可见输出和推理 token 一并计入该上限。 |

MiMo 2.5 系列即将停用，见[官方型号说明](https://mimo.mi.com/docs/quick-start/model)，默认预设选择 2.6。

### Anthropic

| 配置项 | 取值与依据 |
| --- | --- |
| `protocol` / `base_url` | `anthropic` / `https://api.anthropic.com`；[API overview](https://platform.claude.com/docs/en/api/overview) 定义 Messages API 地址。 |
| `usage_mode` | `native`：读取 Messages 的标准 usage；[Messages API](https://platform.claude.com/docs/en/api/messages/create)。 |
| `model` / `max_context_window` | 四个 ID 见[当前模型目录](https://platform.claude.com/docs/en/models/overview)，均为官方标示的 1M（本地元数据为 `1_000_000`）；详见 [Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/overview)、[Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview)、[Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview)、[Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview)。 |
| 推理能力 / `supports_function_calling` | 四型号均 True；上述目录列出 adaptive thinking 与 tool use；[工具调用指南](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) 定义工具协议。 |
| `reasoning_effort.supports` | True；[effort 表](https://platform.claude.com/docs/en/build-with-claude/effort) 确认四型号支持 low/medium/high/xhigh/max，沿用 MyLLM 的原值映射。 |
| `reasoning_enable` | `InputParameter(path="$.thinking", default_value={"type": "adaptive", "display": "summarized"})`；[思考控制](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost) 定义 adaptive thinking，显式 `display=summarized` 请求可见推理摘要。摘要中的明文与用于回放的签名分别保留，不将签名当成可读推理。 |
| `max_tokens` | 4096 是应用的初始输出预算，采用同上官方示例值；它不是型号最大输出能力，调用时可覆盖。Messages API 要求此参数，因此在 ModelProfile 配置，不复制原生参数转换逻辑。 |

Fable 5.1 与 Opus 5.5 为始终开启的 adaptive thinking；不为它们伪造关闭参数。
本次补齐官方主目录的当前系列，不加入受限的 specialized 型号或展开所有历史版本。

## 应用约定与兼容边界

- 本地 provider 名称是 Alira 的注册键，不是新的厂商参数。
- 凭据使用显式 `api_key` 或 profile 的 `Parameter.default_value`，Alira 不读取环境变量兜底；覆盖凭据时保留有效 Parameter 的认证路径。`api_key=omit` 禁用绑定，显式传入密钥也不会重建绑定。
- `usage_mode=Config(default_value="native", ...)`、能力标记、Parameter 路径和 omit 的语义来自 [my-llm-py 配置指南](https://github.com/JJJJSTIYYYY/my-llm/blob/main/docs/reference/configuration.md)。它们在本地配置，实际请求仍使用上表的官方原生字段。成功调用后通过 `bot.llm.get_last_usage()` 读取用量，公开事件和结果不含 usage。
- `max_tokens` Parameter 的 path 从 SDK 参数字典根部开始；`$.max_tokens` 直接传 SDK 参数，`$.extra_body.max_tokens` 经 extra_body 传入。路径由库解释，项目不维护转换层。不传调用参数时，仅在原生目标字段缺失时补 profile 默认预算；显式正整数覆盖目标值，`max_tokens=None` 写入实际空值，`max_tokens=omit` 删除本次目标键。Profile 的 `max_tokens=None` 继承整份 Parameter，`max_tokens=omit` 禁用自动写入并保留手动原生字段。输入参数不被修改。
- `reasoning=True` 或非 none 的强度会触发自动生成的思考参数；开启参数和映射后的 effort 优先于同路径的原生 extra_body 值。普通默认字段仍仅在缺失时补值。
- SDK 客户端在 bot 内复用；所有请求完成后 `await bot.aclose()`，或使用 `async with bot`。流自身的 `async with` 关闭本次响应，不关闭共享客户端。
- `reasoning=False` 按 MyLLM 语义表示本次不注入开启思考的绑定，不等于厂商一定关闭默认思考。需要显式控制时，使用该型号支持的原生 extra_body；K3 系列与 K2.7 不支持关闭思考，不能伪造关闭参数。
- 图片输入须在 ModelProfile 显式声明 `supports_vision=Config(default_value=True)`；当前仅 DeepSeek Flash 开启，其余内置型号仍为 False。这是应用配置边界，不代表所有厂商模型均无视觉能力。Chat 输入使用预置语义损失 hook 补偿，Chat 输出及其他协议的损失统一忽略。
- 当前内置型号经官方 SDK + 本地 HTTP transport 验证请求序列化与资源释放；没有使用付费 API 或验证账户权限。

## 自定义服务

CustomProvider 不猜测远端模型能力；由调用方提供有文档依据的原生配置，
在 `ModelProfile` 显式声明 `protocol=Config(default_value=...)`，或继承调用方在
`ProviderProfile` 声明的协议。空模板不提供默认协议，解析模型时必须有有效值。
`reasoning_enable=InputParameter(..., supports=False)` 拒绝推理请求；
`reasoning_effort=InputParameter(..., supports=False)` 禁用自动强度绑定。
`reasoning_enable=omit` 仅停止自动开启参数写入，仍可通过独立的强度参数或原生配置控制推理。

```python
from my_llm import Attribute, Config, ModelProfile, InputParameter, ProviderProfile, omit
from my_llm import TextContent, UserMessage
from alira.modules.agent.core.bot import AliraBot, BaseProvider, CustomProvider

custom = CustomProvider(
    name="my-service",
    profile=ProviderProfile(
        base_url=Config(default_value="https://your-service.example/v1"),
        max_tokens=InputParameter(
            path="$.max_tokens", default_value=omit, comment="输出上限",
        ),  # 若服务使用 max_tokens；其他服务按文档设置。
        models=Attribute(default_value=[ModelProfile(
            model=Attribute(default_value="your-model"),
            protocol=Config(default_value="openai_chat"),
            supports_function_calling=Config(default_value=True),
        )]),
    ),
)
BaseProvider.register(custom)
bot = AliraBot(provider="my-service", model="your-model", api_key="YOUR_API_KEY")
```

更改已有配置时，先获取隔离副本，使用库的模型目录方法，再显式替换注册项：

```python
provider = BaseProvider.get("my-service")
provider.profile.add_models(ModelProfile(
    model=Attribute(default_value="another-model"),
    protocol=Config(default_value="openai_chat"),
))
BaseProvider.register(provider, replace_existing=True)
# 已构造 bot 使用原有快照；新 bot 读取新目录。
```
