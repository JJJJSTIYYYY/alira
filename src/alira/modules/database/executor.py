"""Business action orchestration across store-owned services."""

import copy
import inspect
from collections.abc import Awaitable, Callable
from uuid import uuid4

from alira.modules.database.services.cache import CacheService
from alira.modules.database.services.data import DataService
from alira.modules.database.services.files import FileService
from alira.modules.database.utils.errors import StoreCode, business_error_result
from alira.modules.database.utils.handlers import store_action
from alira.modules.database.utils.message_tree import MessageNodeTree
from alira.modules.database.utils.serialization import decode_message_row
from alira.shared.models.message import AliraMessageExtension
from alira.utils.logger import logger


class StoreExecutor:
    """
    Execution layer.

    Responsibilities:
    - Translate high-level business actions into ordered service calls
    - Coordinate cache-store and data-store handlers
    - Normalize return format
    """

    def __init__(
        self,
        *,
        cache_store: CacheService,
        data_store: DataService,
        file_store: FileService,
    ):
        self.cache_store = cache_store
        self.data_store = data_store
        self.file_store = file_store

    async def _cache_call(
        self,
        method: Callable[..., Awaitable[dict]],
        payload: dict,
        **options: bool,
    ) -> dict:
        """Observe structured cache failures while allowing data-store work to continue."""
        result = await method(payload, **options)
        if result["code"] != StoreCode.SUCCESS:
            logger.warning(
                "Cache operation %s failed: %s",
                method.__name__,
                result.get("messages"),
            )
        return result

    async def _read_through_resource(
        self,
        payload: dict,
        *,
        cache_group: str,
        cache_key: str,
        reader: Callable[[dict], Awaitable[dict]],
    ) -> dict:
        """Read a disposable cache copy, backfilling only successful data-store reads."""
        cache_payload = {"cache_group": cache_group, "cache_key": cache_key}
        cached = await self._cache_call(self.cache_store.get_resource, cache_payload)
        if cached["code"] == StoreCode.SUCCESS and cached.get("cache_hit"):
            return {"success": True, "messages": cached["messages"]}

        result = await reader(payload)
        if result["code"] != StoreCode.SUCCESS:
            return result
        messages = result["messages"]
        if not isinstance(messages, list):
            raise TypeError("Data-store resource messages must be a list")
        await self._cache_call(
            self.cache_store.set_resource,
            {**cache_payload, "messages": messages},
        )
        return result

    async def _invalidate_resources(self, *targets: dict) -> None:
        """Invalidate resources after a successful data-store mutation."""
        for target in targets:
            await self._cache_call(self.cache_store.delete_resources, target)

    async def _write_resource(
        self,
        payload: dict,
        writer: Callable[[dict], Awaitable[dict]],
        *targets: dict,
    ) -> dict:
        """Invalidate cached resources only after their persistent write succeeds."""
        result = await writer(payload)
        if result["code"] == StoreCode.SUCCESS:
            await self._invalidate_resources(*targets)
        return result

    # ------------------------------------------------------------------
    # Handler Export
    # ------------------------------------------------------------------

    def export_handlers(self) -> dict[str, Callable]:
        handlers: dict[str, Callable] = {}

        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue

            attr = getattr(self, attr_name)
            if not callable(attr):
                continue

            task_name = getattr(attr, "_handler_name", None)
            if not task_name:
                continue

            if not inspect.iscoroutinefunction(attr):
                raise TypeError(f"Task handler '{task_name}' must be async function")

            handlers[task_name] = attr

        return handlers

    @store_action
    async def create_user(self, payload: dict) -> dict:
        """Register a name with an automatically generated UID.

        Args:
            payload: ``{"user_name": str}``, a nonempty user name. Any supplied
                user_uid is replaced by a new UUID; the input is not mutated.

        Returns:
            ``{"success": True, "code": 0, "messages": {"user_uid": str,
            "user_name": str}}``. Duplicate names return code 2002; invalid
            payloads return code 1001. Business rejections return success=True
            and standardized messages; unexpected exceptions return success=False.
        """
        user_name = payload.get("user_name")
        if not isinstance(user_name, str) or not user_name.strip():
            return business_error_result(StoreCode.INVALID_PAYLOAD)
        return await self.data_store.create_user(
            {"user_uid": uuid4().hex, "user_name": user_name}
        )

    @store_action
    async def verify_user(self, payload: dict) -> dict:
        """Resolve an existing user without a password.

        Args:
            payload: ``{"user_uid": str}`` or ``{"user_name": str}``. Each
                supplied identifier must be nonempty. When both are supplied,
                both must match the same account.

        Returns:
            ``{"success": True, "code": 0, "messages": {"user_uid": str,
            "user_name": str}}``. Missing users return code 2001; invalid
            identifiers return code 1001. Business rejections return success=True
            with a standard message string; unexpected exceptions use success=False.
        """
        return await self.data_store.verify_user(payload)

    @store_action
    async def list_all_user(self, payload: dict) -> dict:
        """List all registered user identities in registration order.

        Args:
            payload: Unused dictionary; pass ``{}``.

        Returns:
            ``{"success": True, "code": 0, "messages": [{"user_uid": str,
            "user_name": str}, ...]}``, including an empty list if none exist.
            Failures use a nonzero code and a standard message string.
        """
        return await self.data_store.list_all_user(payload)

    # --------------------------------------------------
    # Conversations
    # --------------------------------------------------

    @store_action
    async def create_conversation(self, payload: dict) -> dict:
        """Create a conversation after checking that the user exists.

        Args:
            payload: Required ``user_uid`` for an existing user. Optional fields:
                ``platform`` defaults to ``"default"`` when absent; ``title``
                defaults to ``"New Conversation..."`` when absent or ``None``;
                falsey ``workspace`` becomes ``""``; ``is_cron`` defaults to
                ``False``. The initial cursor is zero.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"conversation_uid": str}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        res = await self.data_store.verify_user({"user_uid": payload["user_uid"]})
        if res["code"] != StoreCode.SUCCESS:
            return res

        return await self.data_store.create_conversation(payload)

    @store_action
    async def update_conversation(self, payload: dict) -> dict:
        """Invalidate conversation messages only after its deletion succeeds.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``. Optional
                ``title``, ``workspace``, ``is_pinned``, ``is_deleted`` and
                ``has_new_message``; absent or ``None`` values leave fields unchanged.
                Deleted rows can also be updated or restored by this method.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                conversation matches. Does not update activity timestamps.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        result = await self.data_store.update_conversation(payload)
        if result["code"] == StoreCode.SUCCESS and payload.get("is_deleted"):
            await self._cache_call(self.cache_store.delete_messages, payload)
        return result

    @store_action
    async def list_conversations(self, payload: dict) -> dict:
        """Fetch the user's conversation list.

        Args:
            payload: Required ``user_uid``. Excludes rows with ``is_deleted = 1``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``,
                ``has_new_message`` and ``is_cron``. Flags are SQLite integers.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.list_conversations(payload)

    @store_action
    async def get_conversation(self, payload: dict) -> dict:
        """Fetch metadata for one conversation.

        Args:
            payload: Required ``conversation_uid``; no user filter is applied.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row.
                Each row has ``conversation_uid``, ``title``, ``work_space``,
                ``last_active_at``, ``created_at``, ``latest_cursor``, ``is_pinned``
                and ``has_new_message``. Flags are SQLite integers.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.get_conversation(payload)

    # --------------------------------------------------
    # Conversation Messages
    # --------------------------------------------------

    @store_action
    async def append_message(self, payload: dict) -> dict:
        """Snapshot an Alira message or record for persistence and caching.

        Args:
            payload: Required ``user_uid``, ``conversation_uid`` and ``message`` (an
                AliraMessage or flat record). The message requires ``role`` and
                ``content``; optional identity, node, metadata, extensions and protocol
                fields follow DataService.append_message. The caller retains ownership.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{}`` after persistence and cache updates.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        message = payload["message"]
        if isinstance(message, AliraMessageExtension):
            message = message.to_record()
        message = copy.deepcopy(message)
        write_payload = {**payload, "message": message}
        result = await self.data_store.append_message(write_payload)
        if result["code"] != StoreCode.SUCCESS:
            return result

        message.update(result["messages"])
        message["user_uid"] = write_payload["user_uid"]
        message["conversation_uid"] = write_payload["conversation_uid"]
        message["is_deleted"] = False
        decode_message_row(message)
        await self._cache_call(
            self.cache_store.append_message, write_payload, take_ownership=True
        )
        await self._cache_call(
            self.cache_store.update_branch_chain,
            {
                "user_uid": write_payload["user_uid"],
                "conversation_uid": write_payload["conversation_uid"],
                "node_id": message.get("node_id", ""),
                "parent_id": message.get("parent_id", ""),
            },
        )
        return {"success": True, "messages": {}}

    @store_action
    async def delete_messages(self, payload: dict) -> dict:
        """Invalidate cached messages and delete persisted messages.

        Args:
            payload: Required ``user_uid``, ``conversation_uid`` and nonempty
                ``messages`` list of node IDs. Deletes matching nodes and linked
                compressed contexts.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{}`` after deletion and cache invalidation.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        res = await self.data_store.delete_messages(payload)
        if res["code"] != StoreCode.SUCCESS:
            return res
        await self._cache_call(self.cache_store.delete_messages, payload)

        deleted_messages = res.get("messages", []) or []
        mem_ids = []
        for message in deleted_messages:
            mem_id = message.get("message_uid")
            if not mem_id:
                continue
            mem_ids.append(mem_id)

        if mem_ids:
            context_payload = {
                "user_uid": payload.get("user_uid", ""),
                "conversation_uid": payload.get("conversation_uid", ""),
                "memory_ids": mem_ids,
            }

            res = await self.data_store.delete_compressed_contexts(context_payload)
            if res["code"] != StoreCode.SUCCESS:
                return res
            await self._invalidate_resources(
                {
                    "cache_group": (
                        f"compressed-context:{payload.get('user_uid') or '-'}"
                    ),
                    "cache_key": payload.get("conversation_uid") or "-",
                }
            )

        return {
            "success": True,
            "messages": {},
        }

    async def _list_messages(
        self,
        payload: dict,
        *,
        allow_roles: tuple[str, ...],
        guess_children: bool,
    ) -> dict:
        """Load a message set once, then assemble and cache its selected branch."""
        current_node_id = payload.get("current_node_id")
        if current_node_id == "-":
            cached = await self._cache_call(self.cache_store.get_branch_chain, payload)
            if (
                cached["code"] == StoreCode.SUCCESS
                and cached.get("cache_hit")
                and cached["messages"]
            ):
                current_node_id = cached["messages"][-1]

        result = await self._cache_call(
            self.cache_store.get_messages, payload, borrow_records=True
        )
        if not (result["code"] == StoreCode.SUCCESS and result.get("cache_hit")):
            result = await self.data_store.list_messages({**payload, "cursor": 1})
            if result["code"] != StoreCode.SUCCESS:
                return result
            await self._cache_call(
                self.cache_store.set_messages,
                {**payload, "messages": result["messages"]},
                take_ownership=True,
            )

        records, branches, chain = MessageNodeTree(
            result["messages"]
        ).build_visible_messages(
            current_node_id,
            allow_roles=allow_roles,
            guess_children=guess_children,
        )
        messages = [
            AliraMessageExtension.from_record(record)
            for record in copy.deepcopy(records)
        ]
        logger.info("Current node_id_chain: %s", chain)
        await self._cache_call(
            self.cache_store.set_branch_chain,
            {**payload, "node_id_chain": chain},
        )
        del result["success"], result["code"]
        result.update(messages=messages, branches=branches, current_chain=chain)
        return {"success": True, "messages": result}

    @store_action
    async def list_messages(self, payload: dict) -> dict:
        """Return model-visible Alira messages through the selected node.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``. Optional
                ``current_node_id`` selects the final node; ``"-"`` reuses the cached
                branch when available. Omitting it selects the default branch.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{"messages": list[AliraMessage], "branches": dict, "current_chain":
            list[str]}``. Messages contain only the selected branch through the
            requested node. Optional next_cursor and cache_hit are retained here.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._list_messages(
            payload,
            allow_roles=("user", "assistant", "tool"),
            guess_children=False,
        )

    @store_action
    async def list_user_face_messages(self, payload: dict) -> dict:
        """Return user-visible Alira messages through the selected branch's leaf.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``. Optional
                ``current_node_id`` selects a branch; ``"-"`` reuses the cached branch.
                Descendants are followed to its leaf.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{"messages": list[AliraMessage], "branches": dict, "current_chain":
            list[str]}``. Messages include the selected branch through its leaf.
            Optional next_cursor and cache_hit are retained in the same dictionary.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._list_messages(
            payload,
            allow_roles=("user", "assistant", "tool"),
            guess_children=True,
        )

    async def _search_messages(
        self, payload: dict, reader: Callable[[dict], Awaitable[dict]]
    ) -> dict:
        """Return business messages with conversation details kept separately."""
        result = await reader(payload)
        if result["code"] != StoreCode.SUCCESS:
            return result
        conversations = {}
        messages = []
        for row in result["messages"]:
            conversations[row["conversation_uid"]] = {
                "title": row["title"],
                "last_active_at": row["last_active_at"],
            }
            messages.append(AliraMessageExtension.from_record(row))
        return {
            "success": True,
            "messages": {
                "messages": messages,
                "conversations": conversations,
                "next_cursor": result["next_cursor"],
            },
        }

    @store_action
    async def search_messages(self, payload: dict) -> dict:
        """Search message nodes by keyword with a chronological cursor.

        Args:
            payload: Required ``user_uid`` and ``keyword`` strings. Optional
                ``conversation_uid`` scopes the query; ``cursor`` is the previous
                next_cursor dictionary containing timestamp, msg_cursor and
                conversation_uid.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{"messages": list[AliraMessage], "conversations": dict, "next_cursor":
            dict | None}``. At most 20 nodes in reverse chronological order.
            Conversation details are keyed by UID with title and last_active_at; None
            ends pagination.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._search_messages(payload, self.data_store.search_messages)

    @store_action
    async def search_messages_vec(self, payload: dict) -> dict:
        """Search message nodes by embedding, falling back to keyword search.

        Args:
            payload: Required ``user_uid`` and ``keyword`` strings. Optional
                ``conversation_uid``, ``cursor`` (timestamp, msg_cursor,
                conversation_uid) and ``min_similarity`` in [-1, 1], default 0.5.
                Disabled embeddings use keyword search.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            ``{"messages": list[AliraMessage], "conversations": dict, "next_cursor":
            dict | None}``, with the same pagination and conversation metadata as
            search_messages.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._search_messages(payload, self.data_store.search_messages_vec)

    @store_action
    async def sync_messages_embedding(self, payload: dict) -> dict:
        """Submit or reuse the current model's background embedding sync.

        Args:
            payload: Optional unused dictionary; pass ``{}`` through the store
                action. Requires an initialized service with embedding enabled.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"task_id": str}}``.
                Returns immediately. While a sync is active, repeated calls
                return the same task ID without starting or queuing another sync.
                Caller cancellation does not cancel the submitted task. Use
                ``get_messages_embedding_sync_status`` to query its progress.
                After completion or failure, another call starts a new task.
            Disabled embedding: ``{"success": True, "code": 2004,
            "messages": "Message embedding is disabled."}``, returned directly.
            Unexpected exceptions use success=False and a standard error code
                and message. Background failures are recorded in task status.
        """
        return await self.data_store.sync_messages_embedding(payload)

    @store_action
    async def get_messages_embedding_sync_status(self, payload: dict) -> dict:
        """Read the current model's latest sync status.

        Args:
            payload: Optional dictionary with ``task_id`` to identify the task.
                Omit it or pass ``{}`` to query the most recent task.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": status}``. Status contains
                ``task_id``, ``status`` (running/completed/failed), ``inserted``
                and ``deleted`` committed record counts, and nullable ``error``.
                Only the most recent task is retained; restarting the service
                clears this in-memory status.
            Missing or mismatched task: ``{"success": True, "code": 2005,
            "messages": "Message embedding sync task not found."}``, returned directly.
            Unexpected exceptions use success=False and a standard error code
                and message.
        """
        return await self.data_store.get_messages_embedding_sync_status(payload)

    @store_action
    async def get_branch_chain(self, payload: dict) -> dict:
        """Get branch chain.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid`` strings.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            A list of node ID strings; missing or expired entries return an empty list.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        result = await self.cache_store.get_branch_chain(payload)
        if result["code"] != StoreCode.SUCCESS:
            return result
        return {"success": True, "messages": result["messages"]}

    # --------------------------------------------------
    # Files
    # --------------------------------------------------

    @store_action
    async def upload_files(self, payload: dict) -> dict:
        """Copy selected files into the workspace.

        Args:
            payload: Required ``user_uid``, ``workspace`` directory path and
                ``file_path`` list of source file paths. The user must exist.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            A list of dictionaries with ``file_name``, absolute ``saved_path`` and
            workspace-relative ``ws_saved_path``.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        res = await self.data_store.verify_user({"user_uid": payload["user_uid"]})
        if res["code"] != StoreCode.SUCCESS:
            return res
        return await self.file_store.save_files(payload)

    # ------------------------------------------------------------------
    # Skills Files
    # ------------------------------------------------------------------

    @store_action
    async def create_skills(self, payload: dict) -> dict:
        """Import skill archives without persisting database metadata.

        Args:
            payload: Required ``user_uid`` and ``file_path`` list of ZIP paths. The
                user must exist. Archives are moved into the user skill directory and
                inspected for SKILL.md.

        Returns:
            ``{"success": True, "code": 0, "messages": result}``, where result is
            A list of dictionaries with ``skill_id``, ``skill_name``,
            ``skill_description``, ``skill_version``, ``package_path``,
            ``package_size`` and ``package_sha256``. Invalid archives are removed.
            Failure: ``{"success": False, "code": int, "messages": str}``, using
            the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        res = await self.data_store.verify_user({"user_uid": payload["user_uid"]})
        if res["code"] != StoreCode.SUCCESS:
            return res
        return await self.file_store.import_skill_packages(payload)

    # --------------------------------------------------
    # Compressed context
    # --------------------------------------------------

    @store_action
    async def list_compressed_contexts(self, payload: dict) -> dict:
        """List the conversation's active compressed contexts.

        Args:
            payload: Required ``user_uid`` and ``conversation_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row
                containing ``memory_id``, ``content`` and integer
                ``created_timestamp`` (Unix microseconds).
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._read_through_resource(
            payload,
            cache_group=(f"compressed-context:{payload.get('user_uid') or '-'}"),
            cache_key=payload.get("conversation_uid") or "-",
            reader=self.data_store.list_compressed_contexts,
        )

    @store_action
    async def create_compressed_context(self, payload: dict) -> dict:
        """Create a compressed context and invalidate its conversation cache.

        Args:
            payload: Required ``memory_id``, ``user_uid``, ``conversation_uid``
                and string ``content``. The memory ID must be unique; user and
                conversation foreign keys must exist.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._write_resource(
            payload,
            self.data_store.create_compressed_context,
            {
                "cache_group": f"compressed-context:{payload.get('user_uid') or '-'}",
                "cache_key": payload.get("conversation_uid") or "-",
            },
        )

    # --------------------------------------------------
    # LLM Provider
    # --------------------------------------------------

    @store_action
    async def create_llm_provider(self, payload: dict) -> dict:
        """Create a provider and invalidate its list and item caches.

        Args:
            payload: Required ``user_uid``, ``provider_name``,
                ``endpoint`` and JSON-serializable ``model_list`` (normally a list).
                ``type`` defaults to ``"openai"`` when absent or falsey and is
                lowercased; the schema permits ``openai`` and ``anthropic``.
                ``description`` defaults to ``None``. The executor generates provider_id.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"provider_id": provider_id}}``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        write_payload = {**payload, "provider_id": uuid4().hex}
        return await self._write_resource(
            write_payload,
            self.data_store.create_llm_provider,
            {
                "cache_group": "providers:list",
                "cache_key": write_payload.get("user_uid") or "-",
            },
            {
                "cache_group": "providers:item",
                "cache_key": write_payload["provider_id"],
            },
        )

    @store_action
    async def list_llm_providers(self, payload: dict) -> dict:
        """List the user's providers.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``provider_id``, ``provider_name``, ``type``, ``endpoint``,
                decoded ``model_list``, nullable ``description`` and string
                ``created_at``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._read_through_resource(
            payload,
            cache_group="providers:list",
            cache_key=payload.get("user_uid") or "-",
            reader=self.data_store.list_llm_providers,
        )

    @store_action
    async def get_llm_provider(self, payload: dict) -> dict:
        """Get one provider by id.

        Args:
            payload: Required ``provider_id``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one row
                shaped as in ``list_llm_providers``; ``model_list`` is decoded.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._read_through_resource(
            payload,
            cache_group="providers:item",
            cache_key=payload.get("provider_id") or "-",
            reader=self.data_store.get_llm_provider,
        )

    @store_action
    async def update_llm_provider(self, payload: dict) -> dict:
        """Update a provider and invalidate its list and item caches.

        Args:
            payload: Required ``provider_id`` and ``user_uid``. Optional
                ``provider_name``, ``type``, ``endpoint``, ``model_list``,
                ``description`` and ``is_deleted``; absent or ``None`` values
                leave fields unchanged. String ``type`` is lowercased. A list
                ``model_list`` is encoded as a native JSON value.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted provider matches. Deleted providers cannot be restored.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._write_resource(
            payload,
            self.data_store.update_llm_provider,
            {
                "cache_group": "providers:list",
                "cache_key": payload.get("user_uid") or "-",
            },
            {
                "cache_group": "providers:item",
                "cache_key": payload.get("provider_id") or "-",
            },
        )

    # --------------------------------------------------
    # MCP Server
    # --------------------------------------------------

    @store_action
    async def create_mcp_server(self, payload: dict) -> dict:
        """Create an MCP server and invalidate its user's server lists.

        Args:
            payload: Required ``user_uid``, ``mcp_name``, ``transport``
                and ``endpoint`` (which may be ``None``). Transport must be
                ``stdio``, ``http``, ``streamable_http``, ``websocket`` or ``sse``.
                Optional JSON-serializable ``config`` defaults to an empty object
                when absent; ``description`` defaults to ``None``. The executor generates a unique ID.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"mcp_id": mcp_id}}``.
                New servers are enabled with a tool count of zero.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        write_payload = {**payload, "mcp_id": uuid4().hex}
        return await self._write_resource(
            write_payload,
            self.data_store.create_mcp_server,
            {"cache_group": f"mcp:{write_payload.get('user_uid') or '-'}"},
        )

    @store_action
    async def list_mcp_servers(self, payload: dict) -> dict:
        """List the user's MCP servers.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint``, decoded ``config``, nullable ``description``,
                integer ``enabled`` and ``tool_count``, and string ``created_at``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._read_through_resource(
            payload,
            cache_group=f"mcp:{payload.get('user_uid') or '-'}",
            cache_key="all",
            reader=self.data_store.list_mcp_servers,
        )

    @store_action
    async def list_enabled_mcp_servers(self, payload: dict) -> dict:
        """List the user's enabled MCP servers.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``mcp_id``, ``mcp_name``, ``transport``, nullable
                ``endpoint`` and decoded ``config``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._read_through_resource(
            payload,
            cache_group=f"mcp:{payload.get('user_uid') or '-'}",
            cache_key="enabled",
            reader=self.data_store.list_enabled_mcp_servers,
        )

    @store_action
    async def update_mcp_server(self, payload: dict) -> dict:
        """Update an MCP server and invalidate its user's server lists.

        Args:
            payload: Required ``mcp_id`` and ``user_uid``. Optional ``mcp_name``,
                ``transport``, ``endpoint``, ``config``, ``description``, ``enabled``,
                ``tool_count`` and ``is_deleted``; absent or ``None`` values leave
                fields unchanged. Config is encoded as a native JSON value.
                Transport retains its schema constraints.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted server matches. Deleted servers cannot be restored.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self._write_resource(
            payload,
            self.data_store.update_mcp_server,
            {"cache_group": f"mcp:{payload.get('user_uid') or '-'}"},
        )

    @store_action
    async def create_cron_task(self, payload: dict) -> dict:
        """Create a scheduled task with an application-level id.

        Args:
            payload: Required ``user_uid`` keys. The schema also
                requires non-None ``conversation_uid``, ``task_name`` and
                ``exec_time``; the executor generates task IDs and foreign keys must exist.
                Falsey ``platform`` defaults to ``"default"``; falsey ``repeat``
                defaults to ``"once"``. Repeat permits ``once``, ``day``, ``week``,
                ``month``, ``year`` and ``cron``. ``prompt`` and ``execute`` default
                to ``None``; ``description`` defaults to ``""`` when absent.
                ``extra_config`` defaults to an empty object when absent or
                ``None``; otherwise it is encoded as a native JSON value.
                ``exec_time`` is stored as supplied without date-time validation.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {"task_id": task_id}}``.
                New tasks are enabled by default.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        write_payload = {**payload, "task_id": uuid4().hex}
        return await self.data_store.create_cron_task(write_payload)

    @store_action
    async def list_enabled_cron_tasks(self, payload: dict) -> dict:
        """List enabled cron tasks.

        Args:
            payload: Ignored; callers may pass an empty dictionary.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``user_uid``, ``conversation_uid``,
                ``platform``, ``name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, decoded ``extra_config``, nullable ``description``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.list_enabled_cron_tasks(payload)

    @store_action
    async def list_cron_tasks(self, payload: dict) -> dict:
        """List cron tasks.

        Args:
            payload: Required ``user_uid``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, possibly empty.
                Rows have ``task_id``, ``conversation_uid``, ``platform``, ``name``,
                ``prompt``, ``execute``, ``exec_time``, ``repeat``, decoded
                ``extra_config``, nullable ``description``, integer ``enabled``,
                ``created_at`` and ``updated_at``. Time columns are strings.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.list_cron_tasks(payload)

    @store_action
    async def get_cron_task(self, payload: dict) -> dict:
        """Get cron task.

        Args:
            payload: Required ``task_id``.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": rows}``, with zero or one
                row shaped as in ``list_cron_tasks`` plus ``user_uid``.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.get_cron_task(payload)

    @store_action
    async def update_cron_task(self, payload: dict) -> dict:
        """Update a scheduled task, including its enabled and deleted status.

        Args:
            payload: Required ``task_id``. Optional ``conversation_uid``,
                ``platform``, ``task_name``, ``prompt``, ``execute``, ``exec_time``,
                ``repeat``, ``extra_config``, ``description``, ``enabled`` and
                ``is_deleted``; absent or ``None`` values leave fields unchanged.
                Config is encoded as a native JSON value.
                The repeat/foreign-key constraints still apply. No user filter
                or date-time validation is applied.

        Returns:
            Success: ``{"success": True, "code": 0, "messages": {}}``, even if no
                undeleted task matches. Matching rows update ``updated_at``;
                deleted tasks cannot be restored by this method.
            Failure: ``{"success": False, "code": int, "messages": str}``
                using the standard database.utils.errors table. Business rejections instead
            return success=True with a nonzero code and a standard message.
        """
        return await self.data_store.update_cron_task(payload)
