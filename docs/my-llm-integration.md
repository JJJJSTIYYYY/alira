# 模型调用与消息存储

Apix 使用 `my-llm-py[all]==1.2.0` 的 `MyLLM`、`ProviderProfile` 和 `ModelProfile`。
所有上游协议都显式使用 `stdio_protocol="openai_chat"`，消息和工具声明采用
[OpenAI Chat Completions 格式](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
协议转换、SDK 客户端、流累积和终止校验由 my-llm-py 负责。

## 调用

```python
import os
from apix.agent.core.bot import ApixBot
from apix.agent.core.utils.message import ApixAiMessage, ApixUserMessage
from apix.agent.core.tool import tool

@tool
def weather(city: str) -> str:
    """查询城市天气。"""
    return f"sunny in {city}"

bot = ApixBot(
    provider="deepseek",
    model="deepseek-flash",
    api_key=os.environ["DEEPSEEK_API_KEY"],
).bind_tools([weather])

async def ask():
    history = [ApixUserMessage(content="你好")]
    answer = await bot.invoke(history, max_tokens=1024)
    history.append(answer)
    return answer.content

async def ask_stream():
    stream = await bot.stream([{"role": "user", "content": "你好"}])
    async with stream:
        async for event in stream:
            for choice in event.get("choices", []):
                print(choice["delta"].get("content", ""), end="")
    # stream.result 是库累积完成的 Chat assistant 字典；无需另建累积器。
    return ApixAiMessage.from_chat(stream.result)
```

供应商与模型使用字符串选择预设；列表、逐字段官方依据和自定义注册方式见
[Provider 预设](provider-presets.md)。连接、重试、模型能力和厂商默认参数通过库的 profiles 配置。
`invoke` / `stream` 的其他关键字传给 `MyLLM.generate`，例如 `reasoning`、
`reasoning_effort`、`extra_body`。更换模型或 provider 时创建新 bot。
Profile 的模型 ID、上下文窗口和模型目录使用 `Attribute(default_value=...)`，
协议与行为配置使用 `Config(default_value=...)`，请求参数使用
`Parameter(path=..., default_value=..., supports=True)`。
`name` / `source` 由 Profile 字段自动绑定，不能作为构造参数传入；
参数支持状态由 `Parameter.supports` 表达。`Attribute` 的数据通过 `.default_value` 读取，
`ApixBot(provider=..., model=...)` 仍接受字符串，`bot.model` 仍返回模型 ID。
依赖启用 `[all]`，为五种上游协议安装官方 SDK。
DeepSeek 在 ProviderProfile、Kimi K2.x 在 ModelProfile 的 `max_tokens` Parameter
中指定 `$.max_tokens`，K3 沿用 Chat 默认的 `$.max_completion_tokens`。
不传 `max_tokens` 时，缺失字段使用 profile 默认预算，手动原生字段优先；
显式正整数覆盖预算，`None` 传递实际空值，`omit` 删除本次请求的目标字段。
Profile 字段整体为 None 表示继承，整体为 omit 表示禁用该声明并保留手动原生字段。
调用方的 effort 按 `low → medium → high → xhigh → max` 从低到高映射，
未使用的档位只在末尾设为 None，不能中间断档。`none` 表示不注入绑定，独立处理。
例如 K3 / DeepSeek 的本地 low/medium/high 依次映射为上游 low/high/max，
本地 xhigh/max 不可用；仅传 `reasoning=True` 时，库默认 medium 映射为上游 high。
GPT-5.4 Pro 的本地 low/medium/high 依次映射为上游 medium/high/xhigh。
这些映射由 MyLLM 根据 profile 完成，完整对照表见 Provider 预设文档。
`tools=None` 使用绑定工具，`tools=[]` 在当前调用禁用工具。
`stream` 需先 await，再 `async with` 消费；取消或提前退出会由库关闭本次请求。
同一 bot 的请求复用 MyLLM 持有的 SDK 客户端。在所有流消费或关闭后，使用
`await bot.aclose()` 释放客户端，或用 `async with bot` 管理 bot 的生命周期：

```python
async def main():
    async with bot:
        await ask()
        await ask_stream()
```

关闭可以重复调用；关闭后的 bot 不能再次发起请求。

`tool_choice` 等厂商参数使用对应上游协议的 `extra_body` 或 profile 配置。
工具调用由 Apix 的 `ToolNode` 发布事件并收集结果，常驻管理器负责实际执行（见[工具管理器](tool-managers.md)）：

```python
from apix.agent.core.tool import ToolNode

tool_node = ToolNode(weather)

async def tool_round():
    history = [ApixUserMessage(content="查询上海天气")]
    answer = await bot.invoke(history)
    history.append(answer)
    if answer.tool_calls:
        command = await tool_node.execute({"messages": history})
        history.extend(command.update["messages"])
        answer = await bot.invoke(history)
    return answer
```

当前 Ollama SDK 不能完整表达部分工具 JSON Schema 约束；my-llm-py 会抛出
`UnsupportedCapabilityError`。Apix 保留这个异常，不静默删除约束。
SDK 错误及库的 `StreamError`、`IncompleteStreamError` 等异常直接向调用者传播。
完成原因和 usage 位于 Chat 流事件中；需要存储时由调用方记录到 `metadata`。

## 消息

`ApixUserMessage`、`ApixSystemMessage`、`ApixAiMessage`、
`ApixToolMessage` 保留业务属性访问方式。`to_chat()` 返回发送给模型的字段，
`to_dict()` 还包含 `message_uid`、`metadata` 和 `extensions`，返回值均独立复制。
工具消息的 `name` 保留在应用/存储中，不作为 Chat tool 消息字段发送。

- `tool_calls`、`tool_call_id`、`refusal`、`audio`、`annotations`、`function_call` 为顶层协议字段。
- 工具调用为 `{"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}`；仅执行工具时解析 arguments。
- `reasoning_content` 是 Chat 兼容服务的顶层扩展，不属于官方 OpenAI 字段；回放由 profile 控制。
- `metadata` 保留 provider、model、usage、duration 等运行信息。
- `extensions` 保留文件、引用、任务、todo、搜索、系统指令及其他自定义业务字段。
- `content` 保留字符串、内容数组及 assistant 的 null；不将多模态内容转换为字符串。
- `info` 仍是仅用于数据库的业务事件，由上下文适配器读取，不能发送给模型。

## 数据库

数据库服务使用 SQLAlchemy Core + aiosqlite，SQLite 文件位置由
`DATA_STORE.sqlite.database` 配置。`DataService.start()` 创建异步引擎并通过
`core/services/schema.py` 中的表定义创建缺失表；`stop()` 等待当前事务结束后释放连接，支持重启。
应用只将默认 `StoreManager` 注册到 `auto_init`。管理器在专用线程内创建独立
事件循环，并拥有缓存、数据库、文件服务、执行器及查询 worker。启动、查询、
消息树处理、JSON 编解码、缓存复制与持久化都在该线程内执行。

每次存储操作使用独立事务。消息游标通过原子更新分配，与消息写入一起提交或回滚，
多个服务实例访问同一数据库时也不会重复分配游标。外键约束、WAL 和 30 秒忙等待
在连接建立时启用。取消任务时先等待当前底层 SQL 结束，再回滚未提交的事务，
避免取消导致内存数据库的连接失效。

`messages` 表存储 Chat 协议字段，不接受 `developer` 角色。JSON 列为带校验的
TEXT，读取时恢复 `content`、工具调用、`metadata`、`extensions` 等原生值。
Provider 的 `model_list`、MCP 的 `config` 和 cron 的 `extra_config` 也由数据服务
统一编解码，调用方传原生 JSON 值；字符串按字符串保存，更新时的 `None`
表示不修改。执行器和缓存直接使用解码后的值。消息支持游标、树分支和软删除。

存储代码集中在 `apix/agent/store/core`：`manager.py` 提供跨线程异步接口并管理线程，
`runtime.py` 在线程内启停服务并调度 worker；`executor.py` 编排服务调用和缓存读写。
`services` 下的 `data.py`、`cache.py`、
`files.py` 分别负责 SQLite、缓存和文件。通用错误协议与 JSON 辅助集中在
`handlers.py`、`serialization.py`。操作名与执行器方法同名，使用
`create_*`、`list_*`、`get_*`、`update_*`、`delete_*` 等动词。

包对外导出 `query_store` 和 `StoreManager`。原有 `await query_store(action, payload)`
调用方式不变，失败结果仍转为 `RuntimeError`。独立实例通过路径配置创建，
无需从调用方传入内部服务或事件循环：

```python
from apix.agent.store import StoreManager

store = StoreManager(db_path="store.sqlite3", cache_path="cache.json", worker_count=2)
try:
    result = await store.query("list_conversations", {"user_uid": "user-1"})
finally:
    await store.stop()
```

首个查询自动启动线程，也可提前 `await store.start()`。`stop()` 拒绝新查询、
取消待处理任务、关闭数据库并持久化缓存，最后等待线程退出；同一实例随后可以重启。
查询执行期间调用方应保持 `payload` 不变。取消查询会放弃结果，已经开始的写入仍可能完成；
取消某个启停等待者不会取消共享的启动或清理过程。线程隔离改善主事件循环的响应性，
Python 计算仍共享 GIL，存储操作本身的计算量不变。
