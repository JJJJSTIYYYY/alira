"""Install a declarative source set that can be retried after interruption."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from alira.kernel.module.dependencies.types import DependencyError


@dataclass(frozen=True)
class PackageSource:
    """Immutable archive/repository source selection read from a package manifest.

    Attributes:
        source: Resolved local ZIP/directory path or GitHub repository URL.
        ref: Optional GitHub revision; None selects its default branch.
    """

    source: str
    ref: str | None = None


class _PackageLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys instead of silently overwriting."""

    def construct_mapping(self, node, deep=False):
        """Construct a safe mapping and reject a key count reduced by duplicate keys."""
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise yaml.constructor.ConstructorError(
                None, None, "Duplicate package key", node.start_mark
            )
        return mapping


def read_package(path: Path) -> list[PackageSource]:
    """Read a strict source manifest and resolve local paths relative to its location.

    Args:
        path: Manifest path; relative paths are based on the current working
            directory and ~ is expanded. The filename need not literally be package.yml.

    Returns:
        Ordered PackageSource list from a nonempty modules list. Each entry must
        contain a nonempty source string and may contain a nonempty ref string.
        No other fields are accepted; local source paths are resolved relative
        to the manifest's parent, not the command's working directory.

    Raises:
        DependencyError: File/encoding/YAML failure, duplicate keys or invalid schema.

    Notes:
        Reads YAML only. Archive existence, source safety, declared module names
        and GitHub-only ref validation are checked during source preparation.
    """
    path = path.expanduser().resolve()
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_PackageLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DependencyError(
            "Cannot read package.yml; check the file and YAML syntax"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.keys() != {"modules"}
        or not isinstance(data["modules"], list)
        or not data["modules"]
    ):
        raise DependencyError("package.yml must contain a nonempty modules list")
    sources = []
    for item in data["modules"]:
        if (
            not isinstance(item, dict)
            or not item.keys() <= {"source", "ref"}
            or not isinstance(item.get("source"), str)
            or not item["source"].strip()
            or (
                "ref" in item
                and (not isinstance(item["ref"], str) or not item["ref"].strip())
            )
        ):
            raise DependencyError(
                "Each package module needs source and an optional nonempty ref"
            )
        source = item["source"]
        if not urlsplit(source).scheme:
            source = str((path.parent / Path(source).expanduser()).resolve())
        sources.append(PackageSource(source, item.get("ref")))
    return sources
