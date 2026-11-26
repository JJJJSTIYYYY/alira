"""Application role and tool binding around my-llm-py's middleware interface."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Self

from alira.modules.agent.src.core.bot.provider import BaseProvider
from alira.modules.agent.src.shared.parameter import RoleDefinition
from alira.modules.agent.src.shared.message import *
from alira.modules.agent.src.shared.tools import *


class AliraBot:
    """Use my-llm-py profiles and middleware messages/tools for every provider.

    Profiles configure model/connection behavior; my-llm-py owns per-request SDK clients,
    protocol conversion, stream assembly and terminal validation.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        role_schema: RoleDefinition | None = None,
    ) -> None:
        """Select a registered provider, an exact model ID and an optional role.

        Args:
            provider: Registered provider name. Register custom catalogues through
                BaseProvider before constructing the bot.
            model: Exact model ID in the provider's catalogue.
            role_schema: Optional RoleDefinition, copied through bind_role_schema.

        Raises:
            ValueError: The provider or model is unknown.
            TypeError: The role definition is invalid.

        Profile resolution errors propagate from my-llm-py before inference.
        """
        self.provider = BaseProvider.get(provider)
        self.llm = self.provider.create_llm(model)
        self.model = self.llm.model.model.default_value
        self.role_schema = None
        self._tool_schemas: list[FunctionTool] = []
        self._tool_permissions: set[str] | None = None
        self._filtered_tool_schemas: list[FunctionTool] = []
        if role_schema is not None:
            self.bind_role_schema(role_schema)

    @property
    def name(self) -> str:
        """Return the trimmed role name, or ``"assistant"`` without a role."""
        return self.role_schema["name"].strip() if self.role_schema else "assistant"

    def bind_role_schema(self, role_schema: RoleDefinition) -> Self:
        """Replace the assistant role with an isolated copy of a schema.

        Args:
            role_schema: Dictionary with a non-empty string name, a string
                definition, and an optional string or None tag.

        Returns:
            This bot, allowing chained configuration calls.

        Raises:
            TypeError: The schema or one of its required fields is invalid.
        """
        if (
            not isinstance(role_schema, dict)
            or not isinstance(role_schema.get("name"), str)
            or not role_schema["name"].strip()
            or not isinstance(role_schema.get("definition"), str)
            or role_schema.get("tag") is not None
            and not isinstance(role_schema["tag"], str)
        ):
            raise TypeError("role_schema must be a RoleDefinition object")
        self.role_schema = deepcopy(role_schema)
        return self

    @property
    def tool_schemas(self) -> list[FunctionTool]:
        """Return an independent copy of the currently bound function schemas."""
        return deepcopy(self._tool_schemas)

    def _filter_tools(self) -> None:
        """Refresh the active schemas after bindings or permissions change."""
        self._filtered_tool_schemas = (
            self._tool_schemas
            if self._tool_permissions is None
            else [
                tool for tool in self._tool_schemas
                if tool.name in self._tool_permissions
            ]
        )

    def bind_tools(
        self,
        tool_set: Iterable[FunctionTool],
    ) -> Self:
        """Replace the function tools available to subsequent requests.

        Args:
            tool_set: Iterable of validated my-llm-py FunctionTool schemas with
                unique names, guaranteed by the caller. The iterable is consumed
                once. An empty iterable clears the current tools. Binding keeps
                the current permission selection; all tools are allowed by default.

        Returns:
            This bot, allowing chained configuration calls.

        Example:
            bot.bind_tools([weather.get_schema()]).set_tool_permission({"weather"})
        """
        self._tool_schemas = list(tool_set)
        self._filter_tools()
        return self

    def set_tool_permission(
        self,
        tool_permission_set: set[str] | None = None,
    ) -> Self:
        """Select which bound functions are offered to subsequent model requests.

        Args:
            tool_permission_set: Allowed function names. None selects all bound
                tools; an empty set selects none. Unknown names are ignored.
                The selection is copied and retained when tools are rebound.

        Returns:
            This bot, allowing chained configuration calls. The full bound schema
            collection remains available through tool_schemas.

        This selection controls model tool declarations. Tool execution remains
        the responsibility of the caller.
        """
        self._tool_permissions = (
            None if tool_permission_set is None else tool_permission_set.copy()
        )
        self._filter_tools()
        return self

    def _to_prompt(self) -> list[AliraSystemMessage] | None:
        """Build a role system message, or return None when no role is bound."""
        name = self.role_schema["name"] if self.role_schema else ""
        definition = self.role_schema["definition"] if self.role_schema else ""
        tag = (self.role_schema.get("tag") or "") if self.role_schema else ""
        if not definition.strip() and not name.strip() and not tag.strip():
            return None

        structured = "ROLE DEFINITION\n"
        if name.strip():
            structured += f"- NAME: {name.strip()}\n"
        if tag.strip():
            structured += f"- TAG: {tag.strip()}\n"
        if definition.strip():
            structured += f"- DESCRIPTION: {definition.strip()}\n"

        return [AliraSystemMessage(
            role="system",
            content=[TextContent(structured)],
        )]

    def _messages(
        self, messages: list[AliraMessage], fixed_system_prompt: list[AliraMessage] | None
    ) -> list[AliraMessage]:
        """Order fixed prompts, the role prompt and history in a new message list."""
        if not isinstance(messages, list):
            raise TypeError("messages must be a list")
        if fixed_system_prompt is not None and not isinstance(fixed_system_prompt, list):
            raise TypeError("system_prompt must be a list or None")
        result = list(fixed_system_prompt or [])
        result.extend(self._to_prompt() or [])
        result.extend(messages)
        return result

    async def invoke(
        self,
        messages: list[AliraMessage],
        system_prompt: list[AliraMessage] | None = None,
        *,
        api_key: str | None = None,
        **parameters: Any,
    ) -> AliraAssistantMessage:
        """Generate an Alira assistant message using the configured tool selection.

        Args:
            messages: Conversation history in application middleware message form.
            system_prompt: Optional fixed prompts placed before the bound role
                prompt and history. The input lists are not modified.
            api_key: Authentication override for this call; None uses the profile.
            **parameters: my-llm-py generation options such as reasoning,
                max_tokens and extra_body. Configure tools through bind_tools and
                set_tool_permission. Provider defaults belong in profiles.

        Returns:
            A complete AliraAssistantMessage. Function calls are returned as
            content for the caller to execute.

        Raises:
            TypeError: messages or a supplied system_prompt is not a list.

        Validation, provider and generation errors from my-llm-py propagate.
        """
        response = await self.llm.generate(
            messages=self._messages(messages, system_prompt),
            tools=self._filtered_tool_schemas,
            stream=False,
            api_key=api_key,
            **parameters,
        )
        return AliraAssistantMessage.from_middleware(response)

    async def stream(
        self,
        messages: list[AliraMessage],
        system_prompt: list[AliraMessage] | None = None,
        *,
        api_key: str | None = None,
        **parameters: Any,
    ) -> AliraMessageStream:
        """Start a middleware stream using the configured tool selection.

        Args:
            messages: Conversation history in application middleware message form.
            system_prompt: Optional fixed prompts placed before the bound role
                prompt and history. The input lists are not modified.
            api_key: Authentication override for this call; None uses the profile.
            **parameters: my-llm-py generation options such as reasoning,
                max_tokens and extra_body. Configure tools through bind_tools and
                set_tool_permission. Provider defaults belong in profiles.

        Returns:
            The my-llm-py MessageStream exposed as AliraMessageStream. Events are
            middleware MessageEvent objects; stream.result is a middleware
            AssistantMessage after successful consumption. Convert it through
            AliraAssistantMessage.from_middleware when application fields are needed.

        Raises:
            TypeError: messages or a supplied system_prompt is not a list.

        Use the stream as an async context manager to close the request after
        success, early exit, cancellation or failure. Library validation and
        generation errors propagate during creation or consumption.

        Example:
            stream = await bot.stream(messages)
            async with stream:
                async for event in stream:
                    handle(event)
            answer = AliraAssistantMessage.from_middleware(stream.result)
        """
        return await self.llm.generate(
            messages=self._messages(messages, system_prompt),
            tools=self._filtered_tool_schemas,
            stream=True,
            api_key=api_key,
            **parameters,
        )

    def reuseable(self, new_provider: str, new_model: str) -> bool:
        """Compare provider and model IDs using this bot's middleware protocol."""
        return self.llm.compare(new_provider, new_model, "middleware")

    def get_last_usage(self) -> Usage | None:
        """Return the last usage dictionary for this bot's provider and model."""
        return self.llm.get_last_usage()
