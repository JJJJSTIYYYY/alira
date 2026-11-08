# 模型集成

Alira 使用 `my-llm-py[all]>=2.0.1`。应用层统一传递 middleware 的 `Message`、
`FunctionTool` 和 `MessageEvent` 模型；厂商协议转换、SDK 请求和流累积由 MyLLM 负责。
上游支持 `openai_chat`、`openai_responses` 和 `anthropic`，不再提供 Gemini/Ollama。

## 调用与流

```python
from my_llm import TextContent
from alira.modules.agent.core.bot import AliraBot
from alira.shared.models.message import AliraAssistantMessage, AliraUserMessage

async with AliraBot(provider="deepseek", model="deepseek-flash", api_key="...") as bot:
    history = [AliraUserMessage(content=[TextContent("你好")])]
    answer = await bot.invoke(history)
    # answer 是扩展了业务字段的 AssistantMessage。
    history.append(answer)
    stream = await bot.stream(history)
    async with stream:
        async for event in stream:
            if event.type == "delta" and event.field == "text":
                print(event.value, end="")
    # stream.result 是 MyLLM 累积完成的 AssistantMessage。
    completed = AliraAssistantMessage.from_middleware(stream.result)
    usage = bot.llm.get_last_usage()
```

`AliraBot` 固定使用 `stdio_protocol="middleware"`，同时接受原生 middleware 消息
和 Alira 消息子类。`invoke()` 返回 `AliraAssistantMessage`；`stream()` 直接返回库的
`MessageStream`，不另建累积器。调用方应完整消费流或使用 `async with` 关闭它。
成功消费流后，通过 `get_last_usage()` 读取用量；非流调用不提供此用量接口。
用量需要持久化时放入消息的业务 `metadata`。

## 消息、推理与工具

`content` 是有序内容块列表：`TextContent`、`ImageContent`、`ReasoningContent`
和 `ToolCall`。工具调用使用 `ToolCall(call_id, name, arguments)`，其中
`arguments` 是对象，不是 JSON 字符串。工具结果使用 `ToolMessage`：其 `role`
为 `tool`，`call_id` 标识对应调用，`content` 为文本或图片结果块。

```python
from my_llm import ToolCall
from alira.modules.tool import ToolNode, tool
from alira.shared.models.message import AliraAssistantMessage, AliraUserMessage

@tool
def weather(city: str) -> str:
    return f"sunny in {city}"

async def run(bot):
    node = ToolNode(weather)
    bot.bind_tools(node)
    history = [AliraUserMessage(content=[TextContent("Tokyo weather?")])]
    while True:
        answer = await bot.invoke(history)
        history.append(answer)
        if not any(isinstance(part, ToolCall) for part in answer.content):
            return answer
        command = await node.execute({"messages": history})
        history.extend(command.update["messages"])
```

本地工具和 MCP 工具的 `get_schema()` 均返回 middleware `FunctionTool`。
`ToolNode` 从 assistant 的 `content` 中选取完整 `ToolCall`，执行后返回
`AliraToolMessage`。工具管理器的生命周期见 [工具管理器](tool-managers.md)。

### 同协议调用与推理回放

`ReasoningContent.reasoning` 用于显示可见推理，`feedback_item` 保存原生回放数据，
包括 Responses 的 reasoning 项或 Anthropic 的签名块。消息的 `protocol`、`item`
和 `origin` 由库保留，存储后也应一起还原。不要把 `feedback_item` 转成显示文本。
仅有加密回放数据时，`reasoning` 可以为 `None`。

`should_feedback_reasoning_item` 继续按 Profile 决定历史回传范围：DeepSeek 的
`always` 保留普通历史和工具轮次；MiMo 的 `when_tool_calls` 只回传工具轮次。
应用统一使用 middleware，不需要为同协议回放改用厂商原生输入输出格式。

## Profile 与语义损失

Profile 使用库的 `Attribute`、`Config`、`InputParameter` 和 `OutputParameter`。
模型级协议优先于 provider 级协议；详细型号和参数路径见
[provider 预设](provider-presets.md)。`CustomProvider` 由调用方声明协议和模型能力。

- 解析后的上游协议为 `openai_chat` 时，输入语义损失使用库预置的
  `my_llm.codec.openai.chat.prebuilt.chat_semantic_loss_hook`。它补偿可见推理回传，
  并将工具结果图片转移到后续 user 图片消息，通过引用与工具结果关联。
- Chat 的输出语义损失忽略；其他上游协议统一使用 `ignore`。
- 补偿不代表任意跨协议转换无损。加密推理不能作为可见推理发送，
  图片能力仍由模型的 `supports_vision` 决定。

## 消息存储

`messages` 表按 middleware `Message` 的字段存储：`role`、有序 `content`、
`name`、`protocol`、`item`、`origin`，并增加 `call_id` 兼容 `ToolMessage`。
不再维护 Chat 专用的 `tool_calls`、`tool_call_id`、`reasoning_content` 等列。
存储的 `role` 支持 `user`、`assistant` 和 `tool`；工具结果使用
`tool` 角色，并必须携带非空 `call_id`。其他角色不携带 `call_id`。搜索与新消息标记
直接按角色处理，无需通过 `call_id` 区分用户输入与工具结果。

业务字段包括内部 `id`、唯一 `message_uid`、`user_uid`、内部关联
`conversation_id`、公开查询 `conversation_uid`、`generation_id`、`node_id`、
`parent_id`、`metadata` 和 `extensions`。保留 `msg_cursor`、`timestamp` 和
`is_deleted` 供同步及软删除使用。消息查询不暴露 `id` 和 `conversation_id`。

同一轮 user → assistant/tool_calls → tool → assistant 共用一个 `generation_id`；
由业务调用方为这一整轮分配，不要每条消息重新生成。`metadata` 存用量等信息，
`extensions` 存上传文件、任务等业务数据。

`Generation.generation_id` 默认使用 UUID4 的 32 位 hex 字符串。节点 ID 保留完整
generation ID，追加 `-user` 或 `-alira`（assistant/tool 共用）后缀。
数据库的 `generation_id` 声明为 `String(32)`，`node_id` 和 `parent_id` 声明为
`String(38)`，覆盖最长的 assistant/tool 节点 ID；根节点仍使用 `-`。SQLite 不按声明长度
截断或校验字符串。这些声明用于新建表，不自动迁移已有表。

`AliraBot` 将调用参数 `system_prompt` 和 `role_schema` 生成的 system 消息依次放在
历史消息之前。system 仅用于运行时上下文，不生成消息树节点，也不保存到数据库。
`append_message` 拒绝 system 消息，`list_messages` 和 `list_user_face_messages` 只返回
user、assistant 和 tool 消息。父子关系由调用方通过 `parent_id` 设置。

业务消息统一定义在 `alira.shared.models.message`：`AliraSystemMessage`、
`AliraUserMessage`、`AliraAssistantMessage` 和 `AliraToolMessage`，联合类型为
`AliraMessage`，共享业务字段与扩展属性由 `AliraMessageExtension` 提供。
各消息类均支持 `from_middleware()`，用于在对应的原生 middleware 消息上附加业务字段，
直接复用原生内容和协议身份；工具消息同时保留 `call_id`。

可持久化消息的 `to_record()` 输出包含业务字段的平铺存储记录；
`AliraSystemMessage.to_record()` 会抛出 `ValueError`。`to_wire()` 输出库的 middleware
协议结构，不包含业务字段。`AliraMessageExtension.from_record()` 按角色恢复
对应的 user、assistant 或 tool 消息子类，并拒绝 system 记录。
`from_middleware()` 仍支持运行时的 system 消息；具体消息类的工厂方法只接受匹配的角色。
新消息自动分配 `message_uid`，尚未绑定的用户、会话、generation 和
节点标识默认为空字符串。

`query_store("append_message", payload)` 的 `message` 接受 user、assistant、tool 的
平铺记录字典或对应的 `Alira*Message`。`StoreExecutor` 创建独立的存储快照，数据库、缓存和消息树内部
继续使用字典。`list_messages`、`list_user_face_messages` 和 `search_messages` 返回的
`messages["messages"]` 均为 `Alira*Message` 列表，可以直接传给 `AliraBot.invoke()`：

```python
from alira.modules.database import query_store

identity = {"user_uid": "user-1", "conversation_uid": "conversation-1"}
message = AliraUserMessage(content=[TextContent("你好")], node_id="n1", parent_id="-")
await query_store("append_message", {**identity, "message": message})
result = await query_store("list_messages", {**identity, "current_node_id": "n1"})
answer = await bot.invoke(result["messages"]["messages"])
```

用户和会话需已存在。消息对象不包含 `id`、`conversation_id`、`msg_cursor`、
`timestamp` 或 `is_deleted`；列表响应在 `messages` 内提供业务消息、分支和链信息。
搜索响应的会话标题与活跃时间放在 `messages["conversations"][conversation_uid]` 中。

写入时只创建一份独立消息快照，再将其所有权交给缓存；数据层直接校验记录结构，
不再恢复消息对象后重新序列化。缓存默认读写仍复制数据，Executor 使用显式的所有权
移交和只读记录快照，先筛选分支，再只复制选中的记录。记录的可选 JSON 字段在数据库
读取、缓存写入或缓存文件恢复时归一化，查询返回时直接构造 Alira 消息。
`delete_messages` 接受不重复的节点 ID 列表，通过一条更新语句完成软删除。

MyLLM 的 `stdio_protocol="middleware"` 接受这些原生消息子类，保留输入对象上的
业务字段，请求序列化通过原生 `to_wire()` 投影排除这些字段。SDK 请求测试覆盖
Chat、Responses、Anthropic 的流式与非流式，以及消息缓存开启和关闭。

索引分别覆盖用户/会话/游标列表、用户/会话/节点删除，以及内部会话/软删除/主键搜索。
唯一约束覆盖 `message_uid` 和会话内游标。测试使用实际执行 SQL 的
`EXPLAIN QUERY PLAN` 验证索引命中；`LIKE '%keyword%'` 的正文匹配仍需扫描已按会话
限定的候选内容，多会话搜索的最终排序可能使用临时排序结构。

本次只支持新表结构，不自动迁移已有 SQLite 表。使用新数据库文件启用新结构。
