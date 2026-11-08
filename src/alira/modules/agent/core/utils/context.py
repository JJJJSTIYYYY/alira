from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict

from alira.utils.validation import validate_typed_dict_fields


class RoleSchema(TypedDict):
    name: str
    title: NotRequired[str | None]
    definition: str


class Todo(TypedDict):
    content: str
    status: Literal["pending", "in_progress", "completed"]


class Skill(TypedDict):
    skill_id: str
    skill_name: str
    description: str


class CompressedContext(TypedDict):
    memory_id: str
    content: str
    created_timestamp: int


# ============================================================
# Context item detection
# ============================================================

_CONTEXT_ITEM_SCHEMAS = (RoleSchema, Todo, Skill, CompressedContext)


def _get_context_item_type(
    context_item: dict | list,
) -> str:
    """Determine context item type.

    Supported:
        - RoleSchema
        - list[Todo]
        - list[Skill]
        - list[CompressedContext]
    """
    if isinstance(context_item, dict):
        item = context_item

    elif isinstance(context_item, list):
        if not context_item:
            raise ValueError(
                "Cannot determine context item type from empty list."
            )

        item = context_item[0]

    else:
        raise ValueError(
            "Context item must be dict or list."
        )

    if not isinstance(item, dict):
        raise ValueError(
            "Context item element must be dict."
        )

    for schema in _CONTEXT_ITEM_SCHEMAS:
        try:
            validate_typed_dict_fields(item, schema)
        except ValueError:
            continue
        return schema.__name__

    raise ValueError(
        "Invalid context item format."
    )


# ============================================================
# Prompt conversion
# ============================================================

def to_prompt(
    context_item: Any,
    type_hint: str | None = None,
) -> str:
    """Convert context item into model prompt.

    Supported context items:

        RoleSchema:
            Role definition injected directly.

        list[Todo]:
            Current task progress.

        list[Skill]:
            Available skills for progressive loading.

        list[CompressedContext]:
            Conversation compression summary.
    """
    ci_type = (
        type_hint if type_hint is not None else _get_context_item_type(context_item)
    )
    if not context_item and ci_type in ("Todo", "Skill", "CompressedContext"):
        return ""

    if ci_type == "RoleSchema":
        name = context_item.get("name", "").strip()
        title = (context_item.get("title") or "").strip()
        definition = context_item.get("definition", "").strip()

        if not name and not title and not definition:
            return ""

        lines = ["## Role Definition", ""]
        if name:
            lines.append(f"- Your name: {name}.")
        if title:
            lines.append(f"- Your title: {title}.")
        if definition:
            lines.append("- Your Characteristics:")
            lines.append(f"  {definition}")

        return "\n".join(lines) + "\n"

    if ci_type == "Todo":
        lines = ["## Task Progress:"]
        for index, item in enumerate(context_item, start=1):
            content = item.get("content", "").strip()
            if not content:
                continue
            status = item.get("status", "").strip()
            lines.append(f"{index}. {content} -- {status};")

        return "\n".join(lines)

    if ci_type == "Skill":
        lines = [
            (
                "Skills are reusable capability packages that help you "
                "perform complex tasks. Each skill provides specialized "
                "instructions, workflows, and examples."
            ),
            "",
            (
                "Skills are not loaded automatically. "
                "Before using a skill, you must load it first to obtain "
                "its detailed guide (SKILL.md)."
            ),
            "",
            (
                "Only load a skill when it is clearly relevant to the "
                "user's request. Avoid loading unnecessary skills."
            ),
            "",
            "### Available skills to load:",
            "",
        ]

        for skill in context_item:
            name = skill.get("skill_name", "").strip()
            if not name:
                continue

            lines.append(f"- {name}")
            description = skill.get("description", "").strip()
            if description:
                lines.append(f"  Description: {description}")
            lines.append("")

        lines.append(
            "After loading a skill, you can follow the instructions "
            "provided in its guide."
        )
        return "\n".join(lines) + "\n"

    if ci_type == "CompressedContext":
        lines = [
            "## Conversation Compress",
            "",
            "The following is a summary of earlier messages in this conversation:",
            "",
        ]
        for item in context_item:
            content = item.get("content", "").strip()
            if content:
                lines.append(content)

        return "\n".join(lines)

    raise ValueError(f"Unsupported context item type: {ci_type}")



def get_date_natural_language() -> str:
    '''Get current date in natural language.

    Example: "Wednesday, April 15th, 2026"
    '''

    import inflect

    now = datetime.now()

    day = now.day
    month = now.strftime("%B")
    year = now.year
    weekday = now.strftime("%A")

    p = inflect.engine()
    ordinal_day = p.ordinal(day)

    natural_date = f"DATE: {weekday}, {month} {ordinal_day}, {year}"

    return natural_date
