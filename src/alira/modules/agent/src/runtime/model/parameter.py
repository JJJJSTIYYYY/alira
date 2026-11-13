"""Agent loop configuration shared with modules integrating the agent runtime."""

from typing import NotRequired, TypedDict

from alira.modules.agent.src.shared.identity import AliraIdentity


class LoopParameter(TypedDict):
    """
    Configuration for the Alira agent loop.
    """

    # LLM Runtime
    models_provider: str
    model_name: str
    api_key: str
    model_temperature: float
    custom_provider_id: NotRequired[str]

    enable_think: bool
    llm_calls_warning_threshold: int
    use_model_vision: bool  # If true, the picture will be sent to the LLM to analyze if the LLM supports picture input.


    # Agent Runtime Behavior
    work_dir: str
    keep_tools_message: bool  # If true, async returns will save to database.
    pure_chat_on: bool  # If true, the agent will be a simple LLM without tools.


    # Memory Strategy
    enable_longterm_memory: bool
    enable_shortterm_memory: bool  # If is true, message_summary node will invoke llm to compress else just truncate.
    summary_trigger_threshold: int  # If zero, not compress or truncate.
    summary_exempt_tail_length: int


    # Capabilities / Tools
    enable_file_opration: bool
    enable_web_search: bool
    enable_knowledge_retrieval: bool
    enable_command_opration: bool
    enable_skill_load: bool
    enable_task_flow: bool
    enable_agent_assign: bool
    enable_agent_swarm: bool


    # External Services
    link_provider: str
    link_api_key: str
    content_provider: str
    content_api_key: str
    embed_model: str  # The embed model for knowledge retrieval.
    web_cleaner_mode: str
    auto_save_config: bool  # If true, the agent config will auto save when changed.


    # Agent Identity / Prompt
    role_prompt: RoleSchema
    higher_role_prompt_permission: bool  # If true, the role prompt will insert into system prompt.
