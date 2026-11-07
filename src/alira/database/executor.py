"""Business action orchestration across store-owned services."""

import copy
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import uuid4

from alira.database.services.cache import CacheService
from alira.database.services.data import DataService
from alira.database.services.files import FileService
from alira.database.utils.handlers import store_action
from alira.database.utils.message_tree import MessageNodeTree
from alira.database.utils.serialization import decode_message_row
from alira.utils.logger import logger
from alira.shared.message import AliraMessageExtension


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
            sm_payload = {
                "user_uid": payload.get("user_uid", ""),
                "conversation_uid": payload.get("conversation_uid", ""),
                "memory_ids": mem_ids,
            }

            res = await self.data_store.delete_shortterm_memories(sm_payload)
            if not res.get("success"):
                return res
            await self._invalidate_resources(
                {
                    "cache_group": (
                        f"shortterm-memory:{payload.get('user_uid') or '-'}"
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
            allow_roles=("user", "assistant", "system", "developer", "tool"),
            guess_children=False,
        )

    @store_action
    async def list_user_messages(self, payload: dict) -> dict:
        """Return user-visible Alira messages through the selected branch's leaf."""
        return await self._list_messages(
            payload,
            allow_roles=("user", "assistant", "tool"),
            guess_children=True,
        )

    @store_action
    async def search_messages(self, payload: dict) -> dict:
        """Return business messages with conversation details kept separately."""
        result = await self.data_store.search_messages(payload)
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
        """Import skill packages, persist their metadata, and return visible fields."""
        res = await self.data_store.ensure_user_exists(payload)
        if not res.get("success"):
            return res

        file_res = await self.file_store.import_skill_packages(payload)
        if not file_res.get("success"):
            return file_res

        skill_payload = {
            "user_uid": payload["user_uid"],
            "skills": file_res.get("messages", []),
        }
        store_result = await self._write_resource(
            skill_payload,
            self.data_store.create_skills,
            {"cache_group": f"skills:{payload.get('user_uid') or '-'}"},
        )
        if not store_result.get("success"):
            return store_result

        skill_info_list = file_res.get("messages", [])
        visible_skill_info_list = []

        for skill_info in skill_info_list:
            visible_skill_info = {
                "skill_id": skill_info.get("skill_id"),
                "skill_name": skill_info.get("skill_name"),
                "skill_description": skill_info.get("skill_description"),
                "skill_version": skill_info.get("skill_version"),
                "package_size": skill_info.get("package_size"),
                "is_active": False,
                "upload_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            visible_skill_info_list.append(visible_skill_info)

        return {
            "success": True,
            "messages": visible_skill_info_list,
        }

    @store_action
    async def update_skill(self, payload: dict) -> dict:
        """Update a skill's active or deleted status."""
        return await self._write_resource(
            payload,
            self.data_store.update_skill,
            {"cache_group": f"skills:{payload.get('user_uid') or '-'}"},
        )

    @store_action
    async def list_skills(self, payload: dict) -> dict:
        """List metadata for skills uploaded by the user."""
        limit = int(payload.get("limit", 5))
        return await self._read_through_resource(
            payload,
            cache_group=f"skills:{payload.get('user_uid') or '-'}",
            cache_key=f"list:{limit}",
            reader=self.data_store.list_skills,
        )

    @store_action
    async def get_skill(self, payload: dict) -> dict:
        """Get metadata for one uploaded skill."""
        return await self._read_through_resource(
            payload,
            cache_group=f"skills:{payload.get('user_uid') or '-'}",
            cache_key=f"item:{payload.get('skill_id') or '-'}",
            reader=self.data_store.get_skill,
        )

    # --------------------------------------------------
    # Memory
    # --------------------------------------------------

    @store_action
    async def list_shortterm_memories(self, payload: dict) -> dict:
        """List the conversation's active short-term memories."""
        return await self._read_through_resource(
            payload,
            cache_group=(f"shortterm-memory:{payload.get('user_uid') or '-'}"),
            cache_key=payload.get("conversation_uid") or "-",
            reader=self.data_store.list_shortterm_memories,
        )

    @store_action
    async def create_shortterm_memory(self, payload: dict) -> dict:
        """Create a short-term memory and invalidate its conversation cache."""
        return await self._write_resource(
            payload,
            self.data_store.create_shortterm_memory,
            {
                "cache_group": f"shortterm-memory:{payload.get('user_uid') or '-'}",
                "cache_key": payload.get("conversation_uid") or "-",
            },
        )

    @store_action
    async def list_longterm_memories(self, payload: dict) -> dict:
        """Fetch active long-term memories for a user."""
        return await self._read_through_resource(
            payload,
            cache_group="longterm-memory",
            cache_key=payload.get("user_uid") or "-",
            reader=self.data_store.list_longterm_memories,
        )

    @store_action
    async def create_longterm_memory(self, payload: dict) -> dict:
        """Create a long-term memory with an application-level id."""
        write_payload = {**payload, "memory_id": uuid4().hex}
        return await self._write_resource(
            write_payload,
            self.data_store.create_longterm_memory,
            {
                "cache_group": "longterm-memory",
                "cache_key": write_payload.get("user_uid") or "-",
            },
        )

    @store_action
    async def update_longterm_memory(self, payload: dict) -> dict:
        """Partially update or soft-delete a long-term memory."""
        return await self._write_resource(
            payload,
            self.data_store.update_longterm_memory,
            {
                "cache_group": "longterm-memory",
                "cache_key": payload.get("user_uid") or "-",
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
