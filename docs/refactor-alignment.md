# 目录迁移与调用方对齐记录

目录迁移的调用方已对齐，确认移除的身份旧字段及 generation 访问接口也已同步更新。

## 目录与导入

| 原位置 | 当前实现 | 处理结果 |
| --- | --- | --- |
| `alira.agent.store` | `alira.database` | 已核对业务导入、内部导入、测试和 monkeypatch 路径，更新文档目录。 |
| `alira.common.lifespan` | `alira.lifespan` | 已核对应用、数据库管理器和测试调用，保留分组启停契约。 |
| `alira.common.type.AliraIdentity` | `alira.interface.utils.identiity` 中的 `AliraIdentity` / `ConversationIdentity` | 已按用户和会话边界对齐图、工具管理器、MCP、身份工具和相关测试。 |

身份类型沿用当前文件名 `identiity.py`：`AliraIdentity` 必填字段为 `id`、`platform`；
`ConversationIdentity(AliraIdentity)` 额外要求 `conversation_uid`。
普通请求和用户配置使用 `AliraIdentity`；Generation、图工具目录和 MCP 会话操作使用 `ConversationIdentity`。
补齐 `interface/utils/__init__.py`，使该目录能被当前 `namespaces = false` 的打包配置发现。

## 身份校验与存储调用

[check_identity](../src/alira/agent/core/utils/funcs.py) 校验用户身份并返回
`(user_uid, platform)`；`check_conversation_identity` 额外校验会话 ID，返回
`(user_uid, platform, conversation_uid)`。缺少身份抛出 `RuntimeError`，
必填字段为空时抛出 `IdentityError`。

预置上下文和存储适配器已删除，其专属测试随实现移除。消息模型测试直接验证
`AliraMessageBase` 的应用字段与扩展属性；SQLite 持久化测试直接使用消息的
`to_dict()` 和 `DataService`，验证协议字段、业务扩展、消息树标识及重启后的读取。

## Generation 上下文

[generation.py](../src/alira/interface/utils/generation.py) 通过
`generation_context(generation)` 绑定 `Generation` 对象，`get_generation() -> Generation`
返回当前绑定对象；需要 ID 时读取其 `generation_id` 属性。上下文外调用会抛出
`RuntimeError`，退出上下文时恢复此前绑定，包括嵌套上下文发生异常的情况。

当前源码和测试没有旧 generation manager、stream runtime 或 `AliraEntryDataSchema`
的遗留调用，因此没有恢复这些已删除的 API。

## 验证

- 消息持久化测试覆盖 null、空字符串、JSON 形状字符串、多模态内容和工具消息的 SQLite 往返。
- 配置测试使用 `DATABASE` 和 `DATACACHE`，并验证已移除配置不会覆盖当前值。
- 应用启停测试通过真实 `AutoInit` 和 `ApixisCore` 验证重启、运行时清理顺序和日志线程退出。
- 身份测试覆盖用户及会话校验、普通请求绑定；MCP 测试覆盖用户身份关闭所有会话和会话操作的必填字段校验。
- 新增 generation 上下文测试，覆盖对象返回、上下文外访问、嵌套异常恢复和异步任务隔离。
