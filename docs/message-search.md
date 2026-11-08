# 消息检索

`DataService.search_messages` 提供关键词检索，`search_messages_vec` 提供本地模型向量检索。
两者只返回当前用户在未删除会话中的未删除用户消息和 AI 消息，每个消息节点最多一条，按消息
`timestamp` 倒序分页，每页最多 20 条。`msg_cursor` 与 `conversation_uid` 倒序用于时间戳相同时的稳定排序。

## 启用向量检索

安装可选依赖：

```bash
uv sync --extra embedding
```

在项目根目录的 `config.yaml` 中配置本地 Sentence Transformers 模型目录：

```yaml
DATABASE:
  sqlite:
    database: "./.alira_data/sqlite/alira.sqlite3"
    embedding_enable: true
    embedding_model: "/path/to/model"
    vector_dimension: 640
```

对应配置常量为 `SQLITE_EMBEDDING_ENABLE`、`SQLITE_EMBEDDING_MODEL` 和 `VECTOR_DIMENSION`，默认值分别为
`false`、空字符串和 `640`。维度必须与模型实际输出一致；例如 Harrier OSS v1 270M 使用 640 维。
模型从本地目录加载，首次需要推理时才加载，模型加载与推理在线程中执行。

向量以 float32 BLOB 存入独立的 SQLAlchemy 表，表名由规范化模型路径和维度的哈希确定。
`messages` 表结构保持原样。检索使用 SQLite 的 `sqlite-vec` 余弦距离函数进行精确扫描，适合本地消息存储，
不提供 ANN 索引。同路径、同维度视为同一个模型；更换模型文件时应使用新目录。

只有 `user`、`assistant` 消息的顶层 `text` 内容块参与索引。思维链、工具调用、工具结果、图片、
协议回放信息、元数据和扩展字段均不参与索引。没有正文的消息不建立向量记录。

消息追加成功后，后台任务维护向量索引，追加调用不等待模型推理。索引可能暂时落后于消息；
服务停止时会等待后台任务完成。软删除消息会物理删除所有模型向量表中的对应记录，包括向量检索关闭时。

启动服务和切换模型都不自动同步历史消息。配置修改后重启服务，显式执行全量同步：

```python
from alira.modules.database import query_store

submitted = await query_store("sync_messages_embedding", {})
# {"success": True, "messages": {"task_id": "..."}}

result = await query_store(
    "get_messages_embedding_sync_status", submitted["messages"]
)
# {"success": True, "messages": {
#     "task_id": "...", "status": "running",
#     "inserted": 64, "deleted": 3, "error": None
# }}
```

提交动作立即返回任务 ID，同步在存储运行时后台执行，不占用请求 worker 等待全量同步。
同一模型有同步任务运行时，重复提交返回相同任务 ID，不额外创建或排队同步任务，调用方使用相同的处理流程。
已提交的任务不会因 HTTP 请求超时或取消而停止；正常关闭服务时会等待同步完成。

状态查询返回 `running`、`completed` 或 `failed`。`inserted` 和 `deleted` 是已经提交到数据库的计数；
失败时 `error` 包含错误类型和原因。需要时重复查询该动作即可获取进度，提交成功不代表同步已经成功完成。
完成或失败后再次提交会生成新的任务 ID。只保留当前模型最近一次任务的内存状态，重启后清空。
查询时传 `{}` 可获取最新任务，适用于提交成功但 HTTP 响应丢失、未收到任务 ID 的情况；
没有任务或指定的任务 ID 已被新任务替代时返回失败。

同步只作用于当前模型的表：删除对应消息已软删除或不存在的记录，为尚无向量的正文消息建立记录。
已经存在相同消息 ID 的记录直接跳过。向量检索关闭时，提交返回失败结果。
`query_store` 会把服务返回的失败结果转换为 `RuntimeError`，调用方可按现有存储异常约定处理。

## 检索与分页

通过 `query_store` 调用的搜索动作返回 `AliraMessage` 对象列表，并将会话信息置于 `conversations` 字典；
直接调用 `DataService` 则返回完整消息记录，并附上会话标题和活动时间。

```python
payload = {"user_uid": "user-1", "keyword": "如何保存聊天记录"}
result = await query_store("search_messages_vec", payload)

if result["success"] and result["next_cursor"] is not None:
    next_page = await query_store(
        "search_messages_vec", {**payload, "cursor": result["next_cursor"]}
    )
```

两种搜索都支持可选的 `conversation_uid`，用于限定会话。向量搜索使用余弦相似度筛选，默认
`min_similarity=0.5`，可在请求中设置 `[-1, 1]` 范围内的阈值；匹配结果仍按时间排序。
`DataService` 的向量搜索结果另含 `similarity` 字段。关闭向量检索时，`search_messages_vec` 直接回退至关键词检索。

搜索先匹配，再按会话与 `node_id` 分组，选取每个节点中最新的匹配消息；空 `node_id` 的消息分别返回。
分组后再应用游标，避免同一节点的多条消息跨页重复。向量查询会关联 `messages`，校验消息存在且未删除，
残留向量不会占用页面名额。

`next_cursor` 是包含 `timestamp`、`msg_cursor` 和 `conversation_uid` 的字典，直接传回下一页的 `cursor`。
下一页沿用相同关键词、会话范围和相似度阈值。消息时间戳可能精确到秒，而 `msg_cursor` 只在会话内唯一，
因此需要这三个字段共同定位。未满一页时返回 `None`；满页时返回末条记录的游标，最后可能再得到一个空页。
