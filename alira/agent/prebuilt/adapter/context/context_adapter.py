import copy
from typing import Any
from xml.sax.saxutils import escape

from alira.agent.core.utils.context import Todo
from alira.agent.core.utils.funcs import convert_generation_id_to_message_node_id
from alira.agent.core.utils.message import (
    CHAT_OPTIONAL_FIELDS,
    AnyMessage,
    AliraAiMessage,
    AliraSystemMessage,
    AliraToolMessage,
    AliraUserMessage,
)
from alira.common.utils.logger import logger


class AIContextAdapter:
    """Context adapter for agent core.
    """

    _MISSING_TOOL_OUTPUT = (
        "[The outputs of this tool have been lost, or the tool's execution was interrupted by the user.]"
    )

    def _build_user_context(
        self,
        extensions: dict[str, Any],
    ) -> str:
        context_parts: list[str] = []

        referenced_message = extensions.get("referenced_message") or {}
        if isinstance(referenced_message, dict) and referenced_message:
            referenced_role = referenced_message.get("role") or "[UNKNOWN]"
            referenced_speaker = referenced_message.get("name") or "[UNKNOWN]"
            referenced_content = (
                referenced_message.get("content")
                or "[CONTENT MISSED]"
            )

            context_parts.append(
                "<referenced_message>\n"
                f"  <role>{escape(str(referenced_role))}</role>\n"
                f"  <speaker>{escape(str(referenced_speaker))}</speaker>\n"
                f"  <content>{escape(str(referenced_content))}</content>\n"
                "</referenced_message>"
            )

        active_file = extensions.get("active_file") or ""
        if active_file:
            context_parts.append(
                f"<active_file>{escape(str(active_file))}</active_file>"
            )

        uploaded_files = extensions.get("uploaded_files") or []
        if isinstance(uploaded_files, list) and uploaded_files:
            files_xml = "\n".join(
                f"  <file>{escape(f'./upload_files/{filename}')}</file>"
                for filename in uploaded_files
            )

            context_parts.append(
                "<uploaded_files>\n"
                f"{files_xml}\n"
                "</uploaded_files>"
            )

        task = extensions.get("task") or {}
        if isinstance(task, dict) and task:
            task_type = task.get("type")
            task_name = task.get("name")
            task_prompt = task.get("prompt")

            task_parts: list[str] = []

            if task_type:
                task_parts.append(
                    f"  <type>{escape(str(task_type))}</type>"
                )

            if task_name:
                task_parts.append(
                    f"  <name>{escape(str(task_name))}</name>"
                )

            if task_prompt:
                task_parts.append(
                    f"  <prompt>{escape(str(task_prompt))}</prompt>"
                )

            if task_parts:
                context_parts.append(
                    "<task>\n"
                    f"{'\n'.join(task_parts)}\n"
                    "</task>"
                )

        if not context_parts:
            return ""

        return (
            "<context>\n"
            f"{'\n\n'.join(context_parts)}\n"
            "</context>\n\n"
        )


    def ensure_tool_message(self, agent_messages: list[AnyMessage]) -> None:
        """
        Make sure tool messages match tool calls in terms of ID, quantity, and order.

        For every AliraAiMessage containing tool calls:

        1. Tool messages immediately follow the corresponding AI message.
        2. Each tool call has exactly one corresponding tool message.
        3. Tool messages follow the order defined by tool_calls.
        4. Existing tool messages are matched using tool_call_id.
        5. Missing tool messages are replaced with placeholder messages.
        6. Duplicate and unmatched tool messages in the contiguous tool
           message block are discarded.

        The original list object is preserved.
        """
        if not agent_messages:
            return

        normalized_messages: list[AnyMessage] = []

        cursor = 0
        total_messages = len(agent_messages)

        while cursor < total_messages:
            current_message = agent_messages[cursor]
            normalized_messages.append(current_message)
            cursor += 1

            if not (
                isinstance(current_message, AliraAiMessage)
                and current_message.tool_calls
            ):
                continue

            # Match only the contiguous ToolMessage block immediately following the current AI message.
            # setdefault keeps the first message when duplicate IDs occur.
            existing_by_call_id: dict[str, AliraToolMessage] = {}

            while (
                cursor < total_messages
                and isinstance(agent_messages[cursor], AliraToolMessage)
            ):
                tool_message = agent_messages[cursor]

                existing_by_call_id.setdefault(
                    tool_message.tool_call_id,
                    tool_message,
                )

                cursor += 1

            seen_call_ids: set[str] = set()

            for tool_index, tool_call in enumerate(
                current_message.tool_calls
            ):
                try:
                    tool_call_id = tool_call["id"]
                    tool_name = tool_call["function"]["name"]
                except (KeyError, TypeError) as exc:
                    raise ValueError(
                        "Invalid tool call structure: "
                        f"message_index={cursor}, "
                        f"tool_call_index={tool_index}, "
                        f"tool_call={tool_call!r}"
                    ) from exc

                if not tool_call_id:
                    raise ValueError(
                        "Tool call must contain a non-empty call_id: "
                        f"tool_call_index={tool_index}"
                    )

                # A repeated call_id makes one-to-one matching ambiguous.
                if tool_call_id in seen_call_ids:
                    raise ValueError(
                        "Duplicate tool call ID found: "
                        f"tool_call_id={tool_call_id!r}, "
                        f"tool_call_index={tool_index}"
                    )

                seen_call_ids.add(tool_call_id)

                tool_message = existing_by_call_id.get(tool_call_id)

                if tool_message is None:
                    tool_message = AliraToolMessage(
                        content=self._MISSING_TOOL_OUTPUT,
                        name=tool_name,
                        tool_call_id=tool_call_id,
                    )

                normalized_messages.append(tool_message)

        # Preserve the identity of the input list.
        agent_messages[:] = normalized_messages


    def convert_to_alira_messages(
        self,
        dict_messages: list[dict],
        *,
        strict: bool = True
    ) -> tuple[list[AnyMessage], list[Todo]]:
        """
        Create alira messages list (dict list -> alira message objects list).

        This method is a pure converter:
        - Only transforms dict messages into alira messages.

        Args:
            dict_messages: Message dict list with format:

                ```python
                {
                    "message_uid": str,
                    "generation_id": str, # uuid4, tips: messages generated within the same graph loop share the same generation id.
                    "role": str,
                    "name": str | None,
                    "content": str | list,
                    "node_id": str
                    "parent_id": str,
                    "metadata": dict,
                    "extensions": dict,
                }
                ```

        Returns:
            tuple: A tuple containing two lists:
                - The first element is a list of :class:`AliraMessageBase` objects.
                - The second element is a list of the latest :class:`Todo` items.
        """
        logger.info(f"Get dict messages: {len(dict_messages)}.")

        messages = []
        todo: list[Todo] = None

        for row in dict_messages:
            msg_dict = copy.deepcopy(row)
            role = msg_dict.get("role")
            content = msg_dict.get("content")
            metadata = msg_dict.get("metadata") or {}
            extensions = msg_dict.get("extensions") or {}
            name = msg_dict.get("name")
            kwargs = {"metadata": metadata, "extensions": extensions}
            if msg_dict.get("message_uid"):
                kwargs["message_uid"] = msg_dict["message_uid"]

            if role == "user":
                prefix = self._build_user_context(extensions)
                if isinstance(content, list):
                    content = ([{"type": "text", "text": prefix}] if prefix else []) + content
                else:
                    content = prefix + (content or "")
                if not content:
                    continue
                messages.append(AliraUserMessage(**kwargs, content=content, name=name))
            elif role == "assistant":
                fields = {key: msg_dict[key] for key in CHAT_OPTIONAL_FIELDS
                          if key in msg_dict and msg_dict[key] is not None}
                suffix = "<conversation_abort>"
                if isinstance(content, str):
                    content = content.removesuffix(suffix)
                if isinstance(fields.get("reasoning_content"), str):
                    fields["reasoning_content"] = fields["reasoning_content"].removesuffix(suffix)
                if not content and not any(fields.values()):
                    continue
                messages.append(AliraAiMessage(**kwargs, content=content, name=name, **fields))
            elif role == "system":
                if not content:
                    continue
                messages.append(AliraSystemMessage(**kwargs, content=content or "", name=name))
            elif role == "tool":
                messages.append(AliraToolMessage(
                    **kwargs, content=content if content is not None else "",
                    name=name, tool_call_id=msg_dict.get("tool_call_id"),
                ))
            elif role == "info":
                if name == "todo":
                    items = extensions.get("todo_list") or []
                    todo = items if items and items[-1].get("status") != "completed" else None
            else:
                logger.warning(f"Unknown role ignored: {role}")

        if strict: self.ensure_tool_message(messages)
        return messages, todo
    
    
    def convert_to_dict_message(
        self,
        message: AnyMessage,
        generation_id: str,
        parent_id: str = '-',
        *,
        filter: bool = False,
    ) -> dict:
        """
        Convert an :class:`AliraAiMessage` or :class:`AliraToolMessage` into a dictionary representation.

        Args:
            message: An instance of `AliraMessageBase` to be converted.
            generation_id: The unique identifier for the current generation loop.
            parent_id: The node ID of the parent message. Defaults to '-'.
            filter: If True, returns a simplified dictionary containing only essential keys

        Returns:
            A dictionary representing the message. The full structure (when `filter=False`) is as follows:

            ```python
            {
                "generation_id": str,   # UUID4; shared by all messages in the same graph loop
                "role": str,
                "content": str | list,
                "message_uid": str,
                "node_id": str,
                "parent_id": str,
                "metadata": dict,
                "extensions": dict,
            }
            ```
        
        Raises:
            TypeError: If message is not an AliraAiMessage or AliraToolMessage.
            
            When ``filter=True``, the returned dict contains Chat protocol
            fields and optional business ``extensions``.
        """

        if not isinstance(message, (AliraAiMessage, AliraToolMessage)):
            raise TypeError(
                "message must be an AliraAiMessage or AliraToolMessage, "
                f"got {type(message).__name__}."
            )

        result = message.to_dict()
        if filter:
            result = message.to_chat()
            if message.extensions:
                result["extensions"] = copy.deepcopy(message.extensions)
            return result
        result.update(
            generation_id=generation_id,
            node_id=convert_generation_id_to_message_node_id(generation_id, message.role),
            parent_id=parent_id,
        )
        return result

    def drop_tool_messages(
        self,
        input_messages: list[AnyMessage],
        *,
        split_by_todos: bool = True,
        min_keep: int = 16
    ) -> list[AnyMessage]:
        """
        Drop tool messages content in input message list.

        Args:
            split_by_todos[bool]: Split by todo item, drop the completed and keep the in_progress.
            min_keep[int]: The min length of the tail that is not to be dropped.
        """

        if not input_messages:
            return input_messages

        if min_keep < 0:
            raise ValueError("min_keep must be greater than or equal to zero")

        n = len(input_messages)

        # Tail protected region start index
        protected_start = max(0, n - min_keep)

        # Step1: find last write_todos
        last_todo_idx = -1

        if split_by_todos:
            for i, msg in enumerate(input_messages):
                # Only check AliraAiMessage with tool_calls
                if isinstance(msg, AliraAiMessage):
                    if any(
                        call.get("function", {}).get("name") == "write_todos"
                        for call in msg.tool_calls
                    ):
                        last_todo_idx = i

        # Step2: process messages
        messages_after_drop = []

        for i, msg in enumerate(input_messages):
            # Keep tail untouched (highest priority)
            if i >= protected_start:
                messages_after_drop.append(msg)
                continue

            # Case1: no split or no write_todos found
            if not split_by_todos or last_todo_idx == -1:
                if isinstance(msg, AliraToolMessage):
                    # mark outdated
                    new_msg = copy.copy(msg)
                    new_msg.content = "[Tool Result Outdated]"
                    messages_after_drop.append(new_msg)
                else:
                    messages_after_drop.append(msg)
                continue

            # Case2: split_by_todos=True and found last write_todos
            if isinstance(msg, AliraToolMessage) and i < last_todo_idx:
                new_msg = copy.copy(msg)
                new_msg.content = "[outdated]"
                messages_after_drop.append(new_msg)
            else:
                messages_after_drop.append(msg)

        return messages_after_drop
    
    
    def split_messages(
        self,
        input_messages: list[AnyMessage],
        keep_recent: int | None = None,
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """
        Split messages into:
            - messages that can be summarized or dropped safely
            - recent messages length to keep

        The split point will be adjusted to avoid breaking an
        AliraAiMessage(tool_calls) <-> AliraToolMessage chain.

        Args:
            input_messages: Messages to split.
            keep_recent: Optional, if None, split automatically.

        Returns:
            tuple:
                - list[AnyMessage]: drop-safe or summary-safe messages.
                - list[AnyMessage]: invoke-safe messages.

        !!! This method does not ensure that the number of tool calls in an AliraAiMessage
        equals the number of following AliraToolMessages.
        """

        if not input_messages:
            return [], []

        if keep_recent is None:
            for idx in range(len(input_messages) - 1, -1, -1):
                if isinstance(input_messages[idx], AliraUserMessage):
                    to_summarize = input_messages[:idx]
                    recent_messages = input_messages[idx:]

                    logger.info(
                        f"Result: to_summarize={len(to_summarize)}, "
                        f"recent_messages={len(recent_messages)}"
                    )

                    return to_summarize, recent_messages

            return [], input_messages[:]

        if keep_recent <= 0:
            return input_messages[:], []

        if len(input_messages) <= keep_recent:
            return [], input_messages[:]

        split_idx = len(input_messages) - keep_recent

        while split_idx > 0 and isinstance(input_messages[split_idx], AliraToolMessage):
            # Find the full tool block that contains split_idx
            tool_start = split_idx
            while tool_start > 0 and isinstance(input_messages[tool_start - 1], AliraToolMessage):
                tool_start -= 1

            tool_end = split_idx
            while tool_end + 1 < len(input_messages) and isinstance(input_messages[tool_end + 1], AliraToolMessage):
                tool_end += 1

            prev_idx = tool_start - 1

            if (
                prev_idx >= 0
                and isinstance(input_messages[prev_idx], AliraAiMessage)
                and bool(getattr(input_messages[prev_idx], "tool_calls", None))
            ):
                split_idx = prev_idx
                break

            split_idx = tool_end + 1
            break

        to_summarize = input_messages[:split_idx]
        recent_messages = input_messages[split_idx:]

        logger.info(
            f"Result: to_summarize={len(to_summarize)}, "
            f"recent_messages={len(recent_messages)}"
        )

        return to_summarize, recent_messages
    

    def filter_alira_messages(
        self,
        input_messages: list[AnyMessage],
        keep_reasoning: bool = True
    ) -> tuple[list[AnyMessage], list[AnyMessage], str]:
        """
        Keep only summary-safe messages and preserve system messages.

        Summary-safe messages:
            - AliraUserMessage
            - AliraAiMessage(content only), reasoning content will be added in content if `keep_reasoning` is True.

        AliraToolMessage and AliraAiMessage.tool_calls are dropped.
        AliraSystemMessage will return by a independent message list.

        Return:
            list[AnyMessage]: System message list.
            list[AnyMessage]: AI and user messages after filtered.
            str: message's id
        """
        logger.info(f"Client messages count: {len(input_messages)}")

        messages = []
        system_msgs = []
        index = ""

        for input_msg in input_messages:
            if isinstance(input_msg, AliraUserMessage):
                content = input_msg.content or ""
                name = input_msg.name
                messages.append(AliraUserMessage(content=content, name=name))

            elif isinstance(input_msg, AliraAiMessage):
                if keep_reasoning:
                    parts = [
                        part
                        for part in (
                            input_msg.reasoning_content,
                            input_msg.content,
                        )
                        if isinstance(part, str) and part
                    ]
                    content = "\n\n".join(parts)
                else:
                    content = input_msg.content
                name = input_msg.name
                index = input_msg.message_uid
                if content:
                    messages.append(AliraAiMessage(content=content, name=name))

            elif isinstance(input_msg, AliraSystemMessage):
                system_msgs.append(copy.copy(input_msg))

        logger.info(f"The latest message id is {index}")
        return system_msgs, messages, index



ai_context_adapter = AIContextAdapter()
