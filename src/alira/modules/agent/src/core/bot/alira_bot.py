"""Application role and tool binding around my-llm-py's middleware interface."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, Literal, Self

from my_llm import (
    FunctionTool,
    InstructionMessage,
    Message,
    MessageStream,
    ModelProfile,
    ProviderProfile,
    ResolvedProfile,
    TextContent,
)

from alira.modules.agent.src.core.bot.provider import BaseProvider
from alira.modules.tool.src import BaseTool, ToolNode
from alira.modules.agent.src.core.utils.context import RoleSchema, to_prompt
from alira.modules.agent.src.shared.message import AliraAssistantMessage


class AliraBot:
    """Use my-llm-py profiles and middleware messages/tools for every provider.

    Profiles configure model/connection behavior; my-llm-py owns SDK clients,
    protocol conversion, stream assembly and terminal validation.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None = None,
        role_schema: RoleSchema | None = None,
    ) -> None:
        """Select a registered provider and an exact model ID.

        ``api_key`` overrides credentials on the profile's active binding.
        CustomProvider catalogues are registered through BaseProvider before
        selecting them by name.
        Unknown providers/models and invalid profiles fail before inference.
        """
        self.provider = BaseProvider.get(provider)
        self.llm = self.provider.create_llm(model, api_key=api_key)
        self.model = self.llm.model.model.default_value
        self.role_schema = None
        self._tool_schemas: list[FunctionTool] = []
        if role_schema is not None:
            self.bind_role_schema(role_schema)

    async def __aenter__(self) -> Self:
        await self.llm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close MyLLM's shared SDK clients after all requests have finished."""
        await self.llm.aclose()

    @property
    def name(self) -> str:
        """Return the trimmed role name, or ``"assistant"`` without a role."""
        return self.role_schema["name"].strip() if self.role_schema else "assistant"

    def bind_role_schema(self, role_schema: RoleSchema) -> Self:
        """Replace the assistant role with an isolated copy of a schema.

        Args:
            role_schema: Dictionary with a non-empty string name, a string
                definition, and an optional string or None title.

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
            or role_schema.get("title") is not None
            and not isinstance(role_schema["title"], str)
        ):
            raise TypeError("role_schema must be a RoleSchema object")
        self.role_schema = deepcopy(role_schema)
        return self

    @property
    def tool_schemas(self) -> list[FunctionTool]:
        """Return an independent copy of the currently bound function schemas."""
        return deepcopy(self._tool_schemas)

    @staticmethod
    def _schemas(
        tool_set: Iterable[BaseTool] | ToolNode, permissions: set[str] | None = None
    ) -> list[FunctionTool]:
        """Filter tools and copy validated schemas with unique function names."""
        if isinstance(tool_set, ToolNode):
            schemas = tool_set.get_schemas(filter_names=permissions)
        else:
            if isinstance(tool_set, (str, bytes, Mapping)):
                raise TypeError(
                    "tool_set must be an iterable of BaseTool objects or ToolNode"
                )
            try:
                tools = list(tool_set)
            except TypeError as exc:
                raise TypeError(
                    "tool_set must be an iterable of BaseTool objects or ToolNode"
                ) from exc
            if any(not isinstance(tool, BaseTool) for tool in tools):
                raise TypeError("tool_set must contain only BaseTool objects")
            schemas = [
                tool.get_schema()
                for tool in tools
                if permissions is None or tool.name in permissions
            ]
        names = [schema.name for schema in schemas]
        if len(names) != len(set(names)):
            raise ValueError("function tool names must be unique")
        return schemas

    def bind_tools(
        self,
        tool_set: Iterable[BaseTool] | ToolNode,
        tool_permission_set: set[str] | None = None,
    ) -> Self:
        """Replace the function tools available to subsequent requests.

        Args:
            tool_set: Iterable of BaseTool objects or a ToolNode supplying
                schemas. An empty iterable clears the current tools.
            tool_permission_set: Allowed tool names. None allows all tools;
                an empty set allows none.

        Returns:
            This bot, allowing chained configuration calls.

        Raises:
            TypeError: tool_set is invalid or contains objects other than BaseTool.
            ValueError: A schema is invalid or function names are duplicated.
        """
        self._tool_schemas = self._schemas(tool_set, tool_permission_set)
        return self

    def _messages(
        self, messages: list[Message], system_prompt: list[Message] | None
    ) -> list[Message]:
        if not isinstance(messages, list):
            raise TypeError("messages must be a list")
        if system_prompt is not None and not isinstance(system_prompt, list):
            raise TypeError("system_prompt must be a list or None")
        result = list(system_prompt or [])
        if self.role_schema:
            result.append(
                InstructionMessage(
                    role="system",
                    content=[TextContent(to_prompt(self.role_schema, "RoleSchema"))],
                )
            )
        result.extend(messages)
        return result

    async def invoke(
        self,
        messages: list[Message],
        system_prompt: list[Message] | None = None,
        *,
        tools: Iterable[BaseTool] | ToolNode | None = None,
        **parameters: Any,
    ) -> AliraAssistantMessage:
        """Generate a complete middleware assistant message without executing tools.

        ``parameters`` are my-llm-py generate options, including reasoning,
        max_tokens and extra_body. Provider-specific parameters belong in profiles.
        """
        response = await self.llm.generate(
            messages=self._messages(messages, system_prompt),
            tools=self.tool_schemas if tools is None else self._schemas(tools),
            stream=False,
            **parameters,
        )
        return AliraAssistantMessage.from_middleware(response)

    async def stream(
        self,
        messages: list[Message],
        system_prompt: list[Message] | None = None,
        *,
        tools: Iterable[BaseTool] | ToolNode | None = None,
        **parameters: Any,
    ) -> MessageStream:
        """Return the library stream: use async with, then read stream.result.

        Events are middleware MessageEvent objects; result is an AssistantMessage.
        The library closes requests on success, early exit, cancellation and failure.
        """
        return await self.llm.generate(
            messages=self._messages(messages, system_prompt),
            tools=self.tool_schemas if tools is None else self._schemas(tools),
            stream=True,
            **parameters,
        )

    def compare_profiles(
        self, provider: ProviderProfile, model: ModelProfile
    ) -> tuple[ResolvedProfile, int] | Literal[True]:
        """Return True if the provider and model are identical, or a tuple of the resolved profile and a hash key."""
        return self.llm.compare(provider, model)
