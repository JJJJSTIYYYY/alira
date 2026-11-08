"""Business action orchestration across store-owned services."""

import copy
import inspect
from collections.abc import Awaitable, Callable
from uuid import uuid4

from alira.modules.database.services.cache import CacheService
from alira.modules.database.services.data import DataService
from alira.modules.database.services.files import FileService
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
        if not result.get("success"):
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
        if cached.get("success") and cached.get("cache_hit"):
            return {"success": True, "messages": cached["messages"]}

        result = await reader(payload)
        if not result.get("success"):
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
        if result.get("success"):
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
        """Create a user after checking that the account does not exist."""
        res = await self.data_store.ensure_user_exists(payload, exist=False)
        if not res.get("success"):
            return res

        return await self.data_store.create_user(payload)

    @store_action
    async def verify_user(self, payload: dict) -> dict:
        """Verify the supplied user credentials."""
        return await self.data_store.verify_user(payload)

    @store_action
    async def ensure_user_exists(self, payload: dict) -> dict:
        """Ensure the user exists in persistent storage."""
        return await self.data_store.ensure_user_exists(payload)

    # --------------------------------------------------
    # Conversations
    # --------------------------------------------------

    @store_action
    async def create_conversation(self, payload: dict) -> dict:
        """Create a conversation after checking that the user exists."""
        res = await self.data_store.ensure_user_exists(payload)
        if not res.get("success"):
            return res

        return await self.data_store.create_conversation(payload)

    @store_action
    async def update_conversation(self, payload: dict) -> dict:
        """Invalidate conversation messages only after its deletion succeeds."""
        result = await self.data_store.update_conversation(payload)
        if result.get("success") and payload.get("is_deleted"):
            await self._cache_call(self.cache_store.delete_messages, payload)
        return result

    @store_action
    async def list_conversations(self, payload: dict) -> dict:
        """Fetch the user's conversation list."""
        return await self.data_store.list_conversations(payload)

    @store_action
    async def get_conversation(self, payload: dict) -> dict:
        """Fetch metadata for one conversation."""
        return await self.data_store.get_conversation(payload)

    # --------------------------------------------------
    # Conversation Messages
    # --------------------------------------------------

    @store_action
    async def append_message(self, payload: dict) -> dict:
        """Snapshot an Alira message or record for persistence and caching."""
        message = payload["message"]
        if isinstance(message, AliraMessageExtension):
            message = message.to_record()
        message = copy.deepcopy(message)
        write_payload = {**payload, "message": message}
        result = await self.data_store.append_message(write_payload)
        if not result.get("success"):
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
        return {"success": True, "messages": "success"}

    @store_action
    async def delete_messages(self, payload: dict) -> dict:
        """Invalidate cached messages and delete persisted messages."""
        res = await self.data_store.delete_messages(payload)
        if not res.get("success"):
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
            if not res.get("success"):
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
            "messages": "success",
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
            if cached.get("success") and cached.get("cache_hit") and cached["messages"]:
                current_node_id = cached["messages"][-1]

        result = await self._cache_call(
            self.cache_store.get_messages, payload, borrow_records=True
        )
        if not (result.get("success") and result.get("cache_hit")):
            result = await self.data_store.list_messages({**payload, "cursor": 1})
            if not result.get("success"):
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
        return {
            **result,
            "messages": messages,
            "branches": branches,
            "current_chain": chain,
        }

    @store_action
    async def list_messages(self, payload: dict) -> dict:
        """Return model-visible Alira messages through the selected node."""
        return await self._list_messages(
            payload,
            allow_roles=("user", "assistant", "tool"),
            guess_children=False,
        )

    @store_action
    async def list_user_face_messages(self, payload: dict) -> dict:
        """Return user-visible Alira messages through the selected branch's leaf."""
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
        if not result.get("success"):
            return result
        conversations = {}
        messages = []
        for row in result["messages"]:
            conversations[row["conversation_uid"]] = {
                "title": row["title"],
                "last_active_at": row["last_active_at"],
            }
            messages.append(AliraMessageExtension.from_record(row))
        return {**result, "messages": messages, "conversations": conversations}

    @store_action
    async def search_messages(self, payload: dict) -> dict:
        """Search message nodes by keyword with a chronological cursor."""
        return await self._search_messages(payload, self.data_store.search_messages)

    @store_action
    async def search_messages_vec(self, payload: dict) -> dict:
        """Search message nodes by embedding, falling back to keyword search."""
        return await self._search_messages(payload, self.data_store.search_messages_vec)

    @store_action
    async def sync_messages_embedding(self, payload: dict) -> dict:
        """Submit or reuse the current model's background embedding sync.

        Args:
            payload: Unused dictionary; pass ``{}``.

        Returns:
            Success: ``{"success": True, "messages": {"task_id": str}}``.
                First and repeated submissions share the same response contract.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        return await self.data_store.sync_messages_embedding(payload)

    @store_action
    async def get_messages_embedding_sync_status(self, payload: dict) -> dict:
        """Read the current model's latest sync status.

        Args:
            payload: Optional ``task_id``; an empty dictionary selects the latest.

        Returns:
            Success: ``{"success": True, "messages": status}``, containing
                task_id, status, inserted, deleted and error fields.
            Failure: ``{"success": False, "messages": "fail: <error>"}``.
        """
        return await self.data_store.get_messages_embedding_sync_status(payload)

    @store_action
    async def get_branch_chain(self, payload: dict) -> dict:
        return await self.cache_store.get_branch_chain(payload)

    # --------------------------------------------------
    # Files
    # --------------------------------------------------

    @store_action
    async def upload_files(self, payload: dict) -> dict:
        """Copy selected files into the workspace."""
        res = await self.data_store.ensure_user_exists(payload)
        if not res.get("success"):
            return res
        return await self.file_store.save_files(payload)

    # ------------------------------------------------------------------
    # Skills Files
    # ------------------------------------------------------------------

    @store_action
    async def create_skills(self, payload: dict) -> dict:
        """Import skill archives without persisting database metadata.

        Args:
            payload: Required ``user_uid`` and ``file_path`` list of ZIP paths.

        Returns:
            Success: ``{"success": True, "messages": skills}``, containing each
                archive's skill identity, frontmatter metadata, package_path,
                package_size and package_sha256.
            Failure: The user check or file import failure result.
        """
        res = await self.data_store.ensure_user_exists(payload)
        if not res.get("success"):
            return res
        return await self.file_store.import_skill_packages(payload)

    # --------------------------------------------------
    # Compressed context
    # --------------------------------------------------

    @store_action
    async def list_compressed_contexts(self, payload: dict) -> dict:
        """List the conversation's active compressed contexts."""
        return await self._read_through_resource(
            payload,
            cache_group=(f"compressed-context:{payload.get('user_uid') or '-'}"),
            cache_key=payload.get("conversation_uid") or "-",
            reader=self.data_store.list_compressed_contexts,
        )

    @store_action
    async def create_compressed_context(self, payload: dict) -> dict:
        """Create a compressed context and invalidate its conversation cache."""
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
        """Create a provider and invalidate its list and item caches."""
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
        """List the user's providers."""
        return await self._read_through_resource(
            payload,
            cache_group="providers:list",
            cache_key=payload.get("user_uid") or "-",
            reader=self.data_store.list_llm_providers,
        )

    @store_action
    async def get_llm_provider(self, payload: dict) -> dict:
        """Get one provider by id."""
        return await self._read_through_resource(
            payload,
            cache_group="providers:item",
            cache_key=payload.get("provider_id") or "-",
            reader=self.data_store.get_llm_provider,
        )

    @store_action
    async def update_llm_provider(self, payload: dict) -> dict:
        """Update a provider and invalidate its list and item caches."""
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
        """Create an MCP server and invalidate its user's server lists."""
        write_payload = {**payload, "mcp_id": uuid4().hex}
        return await self._write_resource(
            write_payload,
            self.data_store.create_mcp_server,
            {"cache_group": f"mcp:{write_payload.get('user_uid') or '-'}"},
        )

    @store_action
    async def list_mcp_servers(self, payload: dict) -> dict:
        """List the user's MCP servers."""
        return await self._read_through_resource(
            payload,
            cache_group=f"mcp:{payload.get('user_uid') or '-'}",
            cache_key="all",
            reader=self.data_store.list_mcp_servers,
        )

    @store_action
    async def list_enabled_mcp_servers(self, payload: dict) -> dict:
        """List the user's enabled MCP servers."""
        return await self._read_through_resource(
            payload,
            cache_group=f"mcp:{payload.get('user_uid') or '-'}",
            cache_key="enabled",
            reader=self.data_store.list_enabled_mcp_servers,
        )

    @store_action
    async def update_mcp_server(self, payload: dict) -> dict:
        """Update an MCP server and invalidate its user's server lists."""
        return await self._write_resource(
            payload,
            self.data_store.update_mcp_server,
            {"cache_group": f"mcp:{payload.get('user_uid') or '-'}"},
        )

    @store_action
    async def create_cron_task(self, payload: dict) -> dict:
        """Create a scheduled task with an application-level id."""
        write_payload = {**payload, "task_id": uuid4().hex}
        return await self.data_store.create_cron_task(write_payload)

    @store_action
    async def list_enabled_cron_tasks(self, payload: dict) -> dict:
        return await self.data_store.list_enabled_cron_tasks(payload)

    @store_action
    async def list_cron_tasks(self, payload: dict) -> dict:
        return await self.data_store.list_cron_tasks(payload)

    @store_action
    async def get_cron_task(self, payload: dict) -> dict:
        return await self.data_store.get_cron_task(payload)

    @store_action
    async def update_cron_task(self, payload: dict) -> dict:
        """Update a scheduled task, including its enabled and deleted status."""
        return await self.data_store.update_cron_task(payload)
