# 数据库接口

通过 `alira.modules.database.query_store(action, payload)` 调用数据库业务动作。
所有动作最外层只有三个字段：

```python
{"success": True, "code": 0, "messages": {...}}  # 查询结果为 dict 或 list
{"success": True, "code": 2001, "messages": "User not found."}  # 业务失败
{"success": False, "code": 3002, "messages": "Database operation failed."}  # 异常失败
```

无额外返回数据的写操作成功时，`messages` 为 `{}`。业务层正常完成处理时 `success=True`，
包括用户不存在、名称重复、嵌入未启用等业务失败；业务失败直接返回非零 `code` 和标准消息。
调用方使用 `code == 0` 判断业务成功。意外异常由边界转换为 `success=False` 的标准错误结果。
返回结果不包含 SQL、路径、异常文本或错误堆栈。请求取消仍传播取消异常。
生命周期方法 `start()` / `stop()` 的失败仍抛异常。

## 本地用户

用户接口统一使用 `user_name`，用户标识为 `user_uid`。`users` 不存储密码。
注册仅提交名称，`StoreExecutor.create_user` 生成 UUID，数据库写入成功后返回身份：

```python
from alira.modules.database import query_store

registered = await query_store("create_user", {"user_name": "alice"})
# {"success": True, "code": 0,
#  "messages": {"user_uid": "<generated UUID>", "user_name": "alice"}}

logged_in = await query_store("verify_user", {"user_name": "alice"})
if logged_in["code"] == 0:
    user_uid = logged_in["messages"]["user_uid"]

verified = await query_store("verify_user", {"user_uid": user_uid})
users = await query_store("list_all_user", {})
# messages: [{"user_uid": "...", "user_name": "alice"}, ...]
```

名称和 UID 均唯一；注册不采用调用方提供的 UID。重复注册返回 2002，数据库原子处理名称冲突。
名称必须是非空、非纯空白字符串，按原值存储并精确匹配。
`verify_user` 至少提供一个非空字符串标识，两个都提供时必须匹配同一用户；不存在返回 2001，
缺少或无效标识返回 1001。`list_all_user` 按注册顺序返回全部用户，无用户时返回空列表。
`ensure_user_exists` 已移除，内部存在性校验也使用 `verify_user`。

本次仅实现数据库业务层，尚未实现 HTTP 注册／登录接口。数据库初始化创建当前结构，
不自动迁移历史 SQLite 文件；已有含 `username` / `password` 的表需要另行迁移。

## 消息结果

消息列表的 `messages` 为包含业务消息及分支信息的字典：

```python
result = await query_store("list_messages", {
    "user_uid": user_uid,
    "conversation_uid": "...",
    "current_node_id": "...",
})
# result["messages"]:
# {"messages": list[AliraMessage], "branches": dict, "current_chain": list[str]}
```

搜索结果的 `messages` 为
`{"messages": list[AliraMessage], "conversations": dict, "next_cursor": dict | None}`。
下一页使用 `result["messages"]["next_cursor"]` 作为 payload 的 `cursor`。
消息列表原有的 `next_cursor`、`cache_hit` 等服务元数据也保留在 `messages` 内，
不再占用最外层字段。
各动作的完整 payload、默认值和返回字段见 `StoreExecutor` 方法的 `Args` / `Returns`。

## 错误码

错误码及标准消息统一定义于
[`src/alira/modules/database/utils/errors.py`](../src/alira/modules/database/utils/errors.py)。

| code | 名称 | messages |
| --- | --- | --- |
| 0 | SUCCESS | 成功时为 dict 或 list |
| 1001 | INVALID_PAYLOAD | Invalid request payload. |
| 1002 | UNKNOWN_ACTION | Unknown database action. |
| 2001 | USER_NOT_FOUND | User not found. |
| 2002 | USER_ALREADY_EXISTS | User name already exists. |
| 2003 | CONVERSATION_NOT_FOUND | Conversation not found or deleted. |
| 2004 | EMBEDDING_DISABLED | Message embedding is disabled. |
| 2005 | SYNC_TASK_NOT_FOUND | Message embedding sync task not found. |
| 3001 | CONSTRAINT_VIOLATION | Database constraint violation. |
| 3002 | DATABASE_ERROR | Database operation failed. |
| 3003 | FILE_ERROR | File operation failed. |
| 3004 | STORE_UNAVAILABLE | Database service is unavailable. |
| 5000 | INTERNAL_ERROR | Internal database service error. |

预期业务失败直接返回结果，不通过 raise／catch 传递，也不输出异常堆栈。
意外内部错误在服务端保留诊断堆栈，对调用方仍只返回标准消息。
嵌入同步后台任务失败时，状态中的 `error` 同样使用标准消息。
内部数据／缓存服务可以携带分页或缓存元数据，由 executor 组织为上述公开返回结构。
