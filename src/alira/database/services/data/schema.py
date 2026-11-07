"""SQLAlchemy table definitions for the asynchronous data service."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_uid", Text, nullable=False, unique=True),
    Column("username", Text, nullable=False, unique=True),
    Column("password", Text, nullable=False),
    Column(
        "created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
)

conversations = Table(
    "conversations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("platform", Text, nullable=False, server_default=text("'default'")),
    Column("conversation_uid", Text, nullable=False, unique=True),
    Column("title", Text, nullable=False, server_default=text("'New Conversation...'")),
    Column(
        "last_active_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column("latest_cursor", Integer, nullable=False, server_default=text("0")),
    Column("has_new_message", Integer, nullable=False, server_default=text("0")),
    Column("is_pinned", Integer, nullable=False, server_default=text("0")),
    Column("is_cron", Integer, nullable=False, server_default=text("0")),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
    Column(
        "created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column("work_space", Text),
    UniqueConstraint("user_uid", "conversation_uid"),
)

shortterm_memory = Table(
    "shortterm_memory",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("memory_id", Text, nullable=False, unique=True),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "conversation_uid",
        Text,
        ForeignKey("conversations.conversation_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("content", Text, nullable=False),
    Column("created_timestamp", Integer, nullable=False),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("message_uid", Text, nullable=False, unique=True),
    Column("msg_cursor", Integer, nullable=False),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "conversation_id",
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("conversation_uid", Text, nullable=False),
    Column("generation_id", Text, nullable=False, server_default=text("''")),
    Column("node_id", Text, nullable=False, server_default=text("''")),
    Column("parent_id", Text, nullable=False, server_default=text("''")),
    Column(
        "role",
        Text,
        CheckConstraint("role IN ('user', 'assistant', 'system', 'developer', 'tool')"),
        nullable=False,
    ),
    Column("name", Text),
    Column(
        "content",
        Text,
        CheckConstraint("json_valid(content) AND json_type(content) = 'array'"),
        nullable=False,
    ),
    Column("protocol", Text),
    Column("item", Text, CheckConstraint("item IS NULL OR json_type(item) = 'object'")),
    Column(
        "origin",
        Text,
        CheckConstraint("origin IS NULL OR json_type(origin) = 'object'"),
    ),
    Column("call_id", Text),
    CheckConstraint(
        "(role = 'tool' AND call_id IS NOT NULL AND length(call_id) > 0) "
        "OR (role != 'tool' AND call_id IS NULL)"
    ),
    Column(
        "metadata", Text, CheckConstraint("metadata IS NULL OR json_valid(metadata)")
    ),
    Column(
        "extensions",
        Text,
        CheckConstraint("extensions IS NULL OR json_valid(extensions)"),
    ),
    Column("timestamp", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
    UniqueConstraint("conversation_id", "msg_cursor"),
)

llm_provider = Table(
    "llm_provider",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("provider_id", Text, nullable=False, unique=True),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("provider_name", Text, nullable=False),
    Column(
        "type",
        Text,
        CheckConstraint("type IN ('openai', 'anthropic')"),
        nullable=False,
        server_default=text("'openai'"),
    ),
    Column("endpoint", Text, nullable=False),
    Column(
        "model_list", Text, CheckConstraint("json_valid(model_list)"), nullable=False
    ),
    Column("description", Text),
    Column(
        "created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
)

mcp_server = Table(
    "mcp_server",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("mcp_id", Text, nullable=False, unique=True),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("mcp_name", Text, nullable=False),
    Column(
        "transport",
        Text,
        CheckConstraint(
            "transport IN ('stdio', 'http', 'streamable_http', 'websocket', 'sse')"
        ),
        nullable=False,
    ),
    Column("endpoint", Text),
    Column("config", Text, CheckConstraint("json_valid(config)"), nullable=False),
    Column("description", Text),
    Column("tool_count", Integer, nullable=False, server_default=text("0")),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column(
        "created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
)

cron_task = Table(
    "cron_task",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("task_id", Text, nullable=False, unique=True),
    Column(
        "user_uid",
        Text,
        ForeignKey("users.user_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "conversation_uid",
        Text,
        ForeignKey("conversations.conversation_uid", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("platform", Text, nullable=False, server_default=text("'default'")),
    Column("name", Text, nullable=False),
    Column("prompt", Text),
    Column("execute", Text),
    Column("exec_time", Text, nullable=False),
    Column(
        "repeat",
        Text,
        CheckConstraint("repeat IN ('once', 'day', 'week', 'month', 'year', 'cron')"),
        nullable=False,
        server_default=text("'once'"),
    ),
    Column(
        "extra_config",
        Text,
        CheckConstraint("extra_config IS NULL OR json_valid(extra_config)"),
    ),
    Column("description", Text),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column(
        "created_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column(
        "updated_at", Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    ),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
)

Index(
    "idx_conversations_user_pinned_active",
    conversations.c.user_uid,
    conversations.c.is_pinned.desc(),
    conversations.c.last_active_at.desc(),
)
Index(
    "idx_conversations_user_active",
    conversations.c.user_uid,
    conversations.c.last_active_at.desc(),
)
Index(
    "idx_conversations_lookup",
    conversations.c.user_uid,
    conversations.c.conversation_uid,
    conversations.c.is_deleted,
)
Index(
    "idx_shortterm_memory_lookup",
    shortterm_memory.c.user_uid,
    shortterm_memory.c.conversation_uid,
    shortterm_memory.c.created_timestamp.desc(),
)
Index(
    "idx_messages_user_conversation_cursor",
    messages.c.user_uid,
    messages.c.conversation_uid,
    messages.c.msg_cursor,
)
Index(
    "idx_messages_user_conversation_node",
    messages.c.user_uid,
    messages.c.conversation_uid,
    messages.c.node_id,
)
Index(
    "idx_messages_conversation_search",
    messages.c.conversation_id,
    messages.c.is_deleted,
    messages.c.id.desc(),
)
Index(
    "idx_cron_task_user",
    cron_task.c.user_uid,
    cron_task.c.is_deleted,
    cron_task.c.exec_time,
)
Index(
    "idx_cron_task_enabled",
    cron_task.c.is_deleted,
    cron_task.c.enabled,
    cron_task.c.exec_time,
)

__all__ = [
    "conversations",
    "cron_task",
    "llm_provider",
    "mcp_server",
    "messages",
    "metadata",
    "shortterm_memory",
    "users",
]
