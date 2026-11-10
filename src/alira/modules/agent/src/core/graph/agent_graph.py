from typing import get_type_hints

from apixis.core.graph import BaseNode, GraphManager, NodeGraph

from alira.modules.tool.src import BaseTool, ToolNode
from alira.modules.tool.src.base import ToolFunction
from alira.modules.tool.src.utils.manager import ToolManager
from alira.modules.agent.src.shared.identity import ConversationIdentity


class AgentGraph(NodeGraph):
    """Alira agent graph using the Apixis execution lifecycle."""


class AgentGraphCreator(GraphManager):
    """Alira agent graph creator.

    Examples:
        ```python
        class MessageState(TypedDict):
            messages: Annotated[list[str], AutoMerge()]
            status: str

        agent_graph_manager = (
            AgentGraphCreator(MessageState)
            .add_node(first)
        )

        agent_graph = agent_graph_manager.compile_agent("first")
        ```
    """

    def __init__(self, state_schema: type, messages_key: str = "messages"):
        """Create an empty graph definition.

        Args:
            state_schema:
                Annotated state schema containing messages_key. :class:`AgentGraph`
                uses it to discover :class:`AutoMerge` and :class:`KeepRef` fields.
            messages_key: The key in the state dictionary that contains the message list.
        """
        if messages_key not in get_type_hints(state_schema):
            raise KeyError(
                f"The messages_key `{messages_key}` not found in state_schema."
            )
        super().__init__(state_schema)
        self.messages_key = messages_key

    def compile_graph(
        self,
        entry_point: str | list[str] | None,
        *,
        max_steps: int = 1024,
        max_snapshots: int | None = None,
        using_namespace: str | None = None,
        no_snapshot: bool = False,
        exist_ok: bool = False,
    ) -> AgentGraph:
        """Compile a Command-driven agent graph starting at entry_point."""
        return AgentGraph(
            self._nodes,
            entry_point,
            max_steps=max_steps,
            max_snapshots=max_snapshots,
            state_schema=self._state_schema,
            using_namespace=using_namespace,
            no_snapshot=no_snapshot,
            exist_ok=exist_ok,
        )

    def compile_agent(
        self,
        entry_point: str | list[str] | None,
        *,
        max_steps: int = 1024,
        max_snapshots: int | None = None,
        using_namespace: str | None = None,
        no_snapshot: bool = False,
        exist_ok: bool = False,
    ) -> AgentGraph:
        """Compile this definition using the agent-specific graph runtime."""
        return self.compile_graph(
            entry_point,
            max_steps=max_steps,
            max_snapshots=max_snapshots,
            using_namespace=using_namespace,
            no_snapshot=no_snapshot,
            exist_ok=exist_ok,
        )

    def add_tools(
        self,
        tools: list[ToolFunction | BaseTool | ToolManager],
        node_name: str = "tools",
        *,
        bind_llm_node: str | BaseNode | None = None,
        identity: ConversationIdentity | None = None,
    ):
        """Add tools for agent.

        Tools added by this method will be organized as :class:`ToolNode`.

        Args:
            tools: Tool managers, discovered MCP tools, or function tools.
            node_name: Optional tools node name.
            bind_llm_node: Optional LLM node name or node object to run next.
            identity: Identity selecting a resident MCP manager's tool catalog.

        Returns:
            This creator, allowing fluent graph construction.
        """
        self.add_node(
            ToolNode(
                tools,
                node_name,
                messages_key=self.messages_key,
                bind_llm_node=bind_llm_node,
                identity=identity,
            )
        )
        return self
