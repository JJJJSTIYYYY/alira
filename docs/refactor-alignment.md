# 目录迁移与调用方对齐记录

目录迁移的调用方已对齐，确认移除的身份旧字段及 generation 访问接口也已同步更新。

## 目录与导入

| 原位置 | 当前实现 | 处理结果 |
| --- | --- | --- |
| `alira.agent.store` | `alira.database` | 已核对业务导入、内部导入、测试和 monkeypatch 路径，更新文档目录。 |
| `alira.common.lifespan` | `alira.lifespan` | 已核对应用、数据库管理器和测试调用，保留分组启停契约。 |
| `alira.common.type.AliraIdentity` | `alira.interface.utils.identiity.AliraIdentity` | 已更新图、工具管理器、MCP、预置状态、存储适配器、身份工具和相关测试的导入。 |

身份类型沿用当前文件名 `identiity.py`，必填字段为 `id`、`platform`、`conversation_uid`。
补齐 `interface/utils/__init__.py`，使该目录能被当前 `namespaces = false` 的打包配置发现。

## 身份校验与存储调用

[check_identity](../src/alira/agent/core/utils/funcs.py) 返回
`(user_uid, platform, conversation_uid)` 三元组。`associated_account` 已从类型、
返回值、读取逻辑和返回值说明中移除；原有必填字段校验和异常行为保留。

[AIStoreAdapter](../src/alira/agent/prebuilt/adapter/store/store_adapter.py) 的六处调用已同步：

| 方法 | 当前解包 |
| --- | --- |
| `append_message_to_store` | `user_uid, _, conversation_uid` |
| `append_shortterm_to_store` | `user_uid, _, conversation_uid` |
| `get_messages_from_store` | `user_uid, _, conversation_uid` |
| `get_shortterm_from_store` | `user_uid, _, conversation_uid` |
| `get_longterm_from_store` | `user_uid, _, conversation_uid` |
| `get_skills_from_store` | `user_uid, _, _` |

## Generation 上下文

[generation.py](../src/alira/interface/utils/generation.py) 通过
`generation_context(generation)` 绑定 `Generation` 对象，`get_generation() -> Generation`
返回当前绑定对象；需要 ID 时读取其 `generation_id` 属性。上下文外调用会抛出
`RuntimeError`，退出上下文时恢复此前绑定，包括嵌套上下文发生异常的情况。

当前源码和测试没有旧 generation manager、stream runtime 或 `AliraEntryDataSchema`
的遗留调用，因此没有恢复这些已删除的 API。

## 验证

- 存储适配器的既有测试覆盖三元组解包后的读写调用。
- 新增 generation 上下文测试，覆盖对象返回、上下文外访问、嵌套异常恢复和异步任务隔离。
