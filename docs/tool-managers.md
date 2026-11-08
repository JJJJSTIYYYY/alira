# 工具管理器与图节点

依赖版本为 `apixis==1.1.6`。工具服务的生命周期由应用管理，独立于 AgentGraph。

| 概念 | 职责 |
| --- | --- |
| `BaseTool` | 工具定义、Schema、调用格式校验和执行接口。 |
| `Tool` / `@tool` | 把 Python 函数转换为工具，负责参数绑定、自动注入和函数执行。 |
| `FunctionToolManager` | 常驻函数工具目录和事件消费者，拥有执行任务。 |
| `MCPToolManager` | 按身份管理工具目录与 Runtime，路由调用和生命周期操作。 |
| `MCPToolRuntime` | 一个长期任务持有一个客户端上下文，持续接收事件并管理并发调用。 |
| `MCPTool` | MCP 工具定义、Schema 和身份校验；执行委托给管理器的事件调用接口。 |
| `ToolNode` | 选择当前图可用的工具，校验整批调用，发布事件，等待处理结果，生成工具消息并选择后继节点。 |
| `AgentGraph` | 图状态和调度，不导入或关闭 MCP 资源。 |

目录结构：

```text
src/alira/modules/tool/
├── base.py                 # BaseTool 与共享调用契约
├── tool_node.py            # 图节点编排
├── function/               # 函数工具、注入上下文、FunctionToolManager
├── mcp/                    # MCP 定义、Manager、Runtime、结果适配
└── utils/                  # 常驻服务、类型化请求、结果事件与取消协议
```

工具模块位于 `modules/tool/`，与 `modules/agent/` 并列。公共导入使用 `alira.modules.tool`，MCP 类型从 `alira.modules.tool.mcp` 导入。函数工具实现位于 `function/`。

## 一次调用如何流转

假设模型返回 `weather(city="上海")`：

1. ToolNode 校验整批 ToolCall，找到该工具所属的管理器。
2. `ToolManager.invoke()` 登记请求，先订阅该请求 ID 对应的结果事件，再发布调用事件；调用信息放在 `event.context`。
3. 函数管理器创建执行任务。MCP 管理器校验身份和工具目录，将请求路由到对应 Runtime 的调用事件。
4. Runtime 的持续订阅把请求放入收件队列；长期任务消费队列，为每次调用创建受它管理的并发子任务。
5. 工具返回后，请求上下文保存结果、异常和耗时，执行者发布独立的结果事件。
6. ToolNode 按模型调用顺序生成 `AliraToolMessage`，返回一个 Command。

请求事件的 `point="processed"` 只说明请求已交给执行者。调用完成由独立结果事件表示，调用方不会在远端工具返回前提前结束。结果按请求 ID 关联，即使模型复用 ToolCall ID，也不会串用结果。

发布和等待期间通过 `suspend_process()` 释放当前事件链的执行许可。Runtime 的任务启动也脱离当前事件链；并发调用可以等待同批其他工具或继续发起嵌套请求，执行容量和队列容量为 1 时仍可完成。

## 函数工具

```python
from alira.modules.tool import FunctionToolManager, ToolNode, tool

@tool
async def weather(city: str) -> str:
    return f"{city}：晴"

functions = FunctionToolManager([weather])
tools = ToolNode(functions, bind_llm_node="model")
```

管理器构造不自动加入全局服务列表。应用拥有的实例通过 `auto_init.register(functions)` 显式注册，再由 `auto_init.start()` / `auto_init.stop()` 统一启停；独立实例也可以直接 `start()` / `stop()`。模块级 `mcp_mgr` 已显式注册。重复启停幂等，ToolNode 首次执行会启动所需管理器，后续图调用持续复用。

注册时可指定分组；每个实例只能属于一个分组，未指定时属于 `default`：

```python
auto_init.register(functions, group="tool")
await auto_init.start(group="tool")
await auto_init.start(group="tool", restart=True)
await auto_init.stop(group="tool")
```

内置 `resource_cleaner`、`mcp_mgr`、默认 `StoreManager` 分别属于 `core`、`mcp`、`database`。
`start()` / `stop()` 不传分组时选择全部服务，按注册顺序启动、逆序停止；指定不存在的分组抛出 `ValueError`。
已成功启动的服务会跳过，后续注册的服务仍可启动。运行中异常后可用 `restart=True` 显式重启整个分组；重启先停止选中的所有服务，清理失败时不会开始启动。
批量启停会继续处理其他服务，并通过 `ExceptionGroup` 汇总普通异常。启动失败后再次 `start()` 会先清理该服务再重试，成功启动的其他服务保持运行。取消会立即向上传播，后续 `stop()` 或 `start()` 可重试未完成的清理。

`ToolNode(weather)` 仍可用，它会创建 FunctionToolManager。需要在多个图中共享工具服务时，显式构造并复用管理器。

## MCP 工具

```python
from alira.modules.tool import FunctionToolManager, ToolNode
from alira.modules.tool.mcp import MCPToolManager

identity = {"id": "user-1", "platform": "web", "conversation_uid": "chat-1"}
functions = FunctionToolManager([weather])
mcp = MCPToolManager()
await mcp.start()
await mcp.load_all_mcp_tools(identity)

# 身份只决定当前节点暴露的 MCP 工具目录。
tools = ToolNode([functions, mcp], identity=identity, bind_llm_node="model")
# 图执行状态还必须包含同一身份：state["target"] = identity。
```

也可以将 `await mcp.get_mcp_tools(meta, identity)` 返回的部分工具直接交给 ToolNode。模型绑定接受 ToolNode 或 BaseTool 列表；MCPTool 不继承函数工具 Tool。

客户端按 `(id, platform, conversation_uid)` 隔离，同一身份下再按 `mcp_id` 复用。发现接口要求完整 `ConversationIdentity`；执行时检查 `state["target"]` 与发现身份一致，校验通过后才访问连接。

身份类型从 `alira.shared.models.identity` 导入：`AliraIdentity` 包含 `id`、`platform`，`ConversationIdentity(AliraIdentity)` 再增加必填的 `conversation_uid`。MCP 发现、调用和关闭单个会话使用 `ConversationIdentity`；`close_user()` 只要求 `AliraIdentity`。

旧的 `config.lifecycle`、`agent_invoke` 和 `tool_calls` 资源作用域已移除。连接在工具批次和图调用结束后保持打开，通过以下入口显式回收：

```python
await mcp.close_session(identity)  # 当前会话
await mcp.close_user({"id": identity["id"], "platform": identity["platform"]})  # 所有会话
await mcp.stop()                   # 整个管理器
```

每个 `(id, platform, conversation_uid, mcp_id)` 对应一个 Runtime。客户端进入和退出都由 Runtime 的同一个任务完成；旧的 `client_lease()` 与 `session.py` 已移除。配置变化先通过生命周期事件停止旧 Runtime、排空其在途调用并退出客户端，再启动新 Runtime。刷新配置或关闭会话后，旧 MCPTool 必须重新发现并绑定，避免恢复旧配置。关闭时拒绝新请求并等待在途调用；调用方取消等待不会中断管理器拥有的清理任务。关闭失败保留资源和关闭状态，允许再次关闭。

## 发布调用并等待结果的接口

需要直接调用 MCP 工具时，无需自行构造事件、订阅或 ToolCall：

```python
result = await mcp.call_tool(
    identity=identity,
    mcp_id="weather-server",
    tool_name="weather",
    arguments={"city": "上海"},
)
```

调用前需启动管理器并为该身份发现工具。这个接口完成目录查找、请求构造、事件发布、结果等待和异常传播，返回与 ToolNode 一致的工具文本结果。`arguments` 省略时使用空字典。它会一直等到 MCP 工具返回；需要限制等待时间时，调用方可以使用 `asyncio.timeout()`。

`MCPTool.execute(state, tool_call)` 和 ToolNode 也复用相同的发布／等待协议，不会绕过 Runtime 直接调用客户端。

## Runtime 生命周期

Runtime 在接受调用前注册持续订阅，客户端上下文贯穿整个事件消费循环。工具正在执行时，后续调用和退出事件仍会进入收件队列，不依赖单次 `await_for()` 重复注册。

`MCPToolRuntime.lifecycle_event` 接收类型化的 `MCPRuntimeStop`，正常情况下通过 `close_session()`、`close_user()` 或管理器 `stop()` 发布。Runtime 收到停止指令后拒绝新调用，等待已启动的调用结束，然后在原任务中退出客户端上下文并注销订阅。退出失败时原任务保留，下一次停止事件在同一任务中重试清理。

服务退出时先停止应用拥有的管理器，等生命周期事件与结果事件处理完成，再停止事件系统。临时 Runtime 和管理器不会自动登记为全局服务。

## 失败与取消

整批调用在发布前完成格式和工具名校验。任一工具失败会将原异常传回 ToolNode，并取消、等待同批尚未结束的调用。直接调用 `call_tool()` 同样传播工具异常。取消等待或节点超时，只取消对应调用并等待其异步清理，不停止 Runtime 或影响同一客户端上的其他调用。

管理器停止会排空已接收的任务，拒绝尚未接收的请求；停止前排队的请求不会在重启后执行。即使请求尚未送达调度器，已登记的调用也会得到明确结果，不会因订阅移除而永久等待。

`AgentGraph.invoke()` 调用方取消沿用 Apixis 的中止语义：当前节点可能继续运行。它不会关闭工具管理器或 MCP 连接。需要终止具体执行时，应使用节点执行的取消或超时机制。
