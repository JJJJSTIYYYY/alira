# 目录迁移与调用方对齐记录

目录迁移的调用方已对齐，确认移除的身份旧字段及 generation 访问接口也已同步更新。

## 目录与导入

| 原位置 | 当前实现 | 处理结果 |
| --- | --- | --- |
| `agent/core/tool/` | `alira.modules.tool` | 工具模块与 Agent 并列，已更新业务导入、测试、动态导入和文档示例。 |
| `agent/`（移出工具后） | `alira.modules.agent` | 保留内部 `core/` 结构，已更新 Agent SDK、图与上下文导入。 |
| `database/` | `alira.modules.database` | 已核对业务导入、内部导入、测试和 monkeypatch 路径，更新文档目录。 |
| `alira.common.lifespan` | `alira.kernel` | 已核对应用、数据库管理器和测试调用，保留分组启停契约。 |
| `alira.common.type.identiity` | `alira.shared.models.identity` | 修正文件名，集中身份类型和键生成；字段检查由通用工具提供。 |
| `alira.common.type.generation` | `alira.shared.runtime.generation` | 保留生成状态和上下文绑定契约。 |
| `alira.common.type.configuration` | `alira.shared.models.configuration` | 用户级配置契约；进程级设置继续由 `alira.shared.application.config` 管理。 |
| `alira.common.type.request` | `alira.shared.models.request` / `alira.interface.request` | 请求与载荷基类共享，HTTP/WebSocket 请求归接口层。 |

`AliraIdentity` 必填字段为 `id`、`platform`；
`ConversationIdentity(AliraIdentity)` 额外要求 `conversation_uid`。
普通请求和用户配置使用 `AliraIdentity`；Generation、图工具目录和 MCP 会话操作使用 `ConversationIdentity`。

`shared` 按共享内容的职责分类，从具体模块导入：

```text
shared/
├── models/                  # 业务数据契约及其校验、转换
│   ├── identity.py
│   ├── message.py
│   ├── request.py
│   └── configuration.py    # 用户配置模型
├── runtime/                 # 执行状态与上下文
│   └── generation.py
└── application/             # 应用进程配置和版本展示
    ├── config.py
    └── version.py
```

身份模块只依赖标准库；生成上下文、用户配置和请求基类依赖身份模块。
`models` 不依赖 `runtime` 和进程配置，导入业务模型不会加载应用配置。
`modules/` 下的 `agent`、`tool` 和 `database` 可以使用共享契约，共享契约不依赖这些上层模块。
业务无关的工具仍放在 `utils`。

## 异常归属

异常与产生它的业务定义放在一起，避免把模块内部错误集中到公共异常文件：

- TypedDict 字段检查使用标准 `TypeError` / `ValueError`，不再定义身份专用异常。
- `InvalidToolArgsError` 在 `alira.modules.tool.function.tool`，保留 `TypeError` 继承，
  可从 `alira.modules.tool` 导入。
- `MCPToolError` 继续由 `alira.modules.tool.mcp.base` 定义，
  可从 `alira.modules.tool.mcp` 导入。

移除没有调用点的 `PlatformNotRegisteredError`、`InvalidOutputsError`、
`ProviderNotFoundError`、`ProviderTypeMismatchError`、`ChunkMergeError` 和
`IncompleteToolCallError`，并删除原有的两个 `exception.py`，不保留旧路径转发。

## TypedDict 字段完整性校验与存储调用

[validate_typed_dict_fields](../src/alira/utils/validation.py) 根据 TypedDict 的
`__required_keys__` 检查必填字段，遵循继承、`total=False`、`Required` 和 `NotRequired`
生成的必填字段元数据。可选字段和额外字段允许省略或保留，字段值保持原样。
启用 `non_empty=True` 后，必填字段的 `None`、空字符串和空容器也会被拒绝，
`0`、`False` 仍然有效。该函数只检查字段完整性，不检查或转换字段值的类型。

```python
from alira.shared.models.identity import AliraIdentity, ConversationIdentity
from alira.utils.validation import validate_typed_dict_fields

validate_typed_dict_fields(identity, AliraIdentity, non_empty=True)
validate_typed_dict_fields(conversation, ConversationIdentity, non_empty=True)
```

成功时返回 `None`，调用方直接读取原字典。数据不是映射或定义不是 TypedDict 时抛出
`TypeError`；必填字段缺失或不符合非空要求时抛出 `ValueError`，错误包含类型名和字段名。
原有的两个身份专用检查函数和 `IdentityError` 已移除。

预置上下文和存储适配器已删除，其专属测试随实现移除。消息模型测试直接验证
`AliraMessageExtension` 的应用字段与扩展属性；SQLite 持久化测试直接使用消息的
`to_record()` 和 `DataService`，验证协议字段、业务扩展、消息树标识及重启后的读取。

## Generation 上下文

[generation.py](../src/alira/shared/runtime/generation.py) 通过
`generation_context(generation)` 绑定 `Generation` 对象，`get_generation() -> Generation`
返回当前绑定对象；需要 ID 时读取其 `generation_id` 属性。上下文外调用会抛出
`RuntimeError`，退出上下文时恢复此前绑定，包括嵌套上下文发生异常的情况。

当前源码和测试没有旧 generation manager、stream runtime 或 `AliraEntryDataSchema`
的遗留调用，因此没有恢复这些已删除的 API。

## 验证

测试目录与当前源码职责对齐：

| 测试目录 | 覆盖内容 |
| --- | --- |
| `tests/modules/agent` | Agent SDK、Provider 配置与消息协议。 |
| `tests/modules/tool` | 函数工具、工具节点与 MCP 工具。 |
| `tests/modules/database` | 数据、缓存、文件服务、存储执行器与管理器。 |
| `tests/integration` | Agent、工具、事件和应用生命周期的集成测试。 |
| `tests/utils` | 字典合并、JSON 序列化、YAML 读写、日志、Uvicorn 日志适配、TypedDict 字段检查。 |
| `tests/kernel` | 应用服务初始化与启停。 |
| `tests/shared/models` | 共享消息、身份数据。 |
| `tests/shared/runtime` | 生成上下文。 |
| `tests/shared/application` | 应用配置与版本。 |

原 `tests/common` 和 `tests/config` 的用例已迁入上述目录，配置测试命名为
`test_config.py`，与 `shared/application/config.py` 对应。

- 消息持久化测试覆盖 null、空字符串、JSON 形状字符串、多模态内容和工具消息的 SQLite 往返。
- 配置测试使用 `DATABASE` 和 `DATACACHE`，并验证已移除配置不会覆盖当前值。
- 应用启停测试通过真实 `AutoInit` 和 `ApixisCore` 验证重启、运行时清理顺序和日志线程退出。
- 身份测试覆盖用户及会话数据的必填字段检查、普通请求绑定；MCP 测试覆盖用户身份关闭所有会话和会话操作的必填字段校验。
- 新增 generation 上下文测试，覆盖对象返回、上下文外访问、嵌套异常恢复和异步任务隔离。
