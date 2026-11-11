"""Fingerprint local requirement inputs without resolving or downloading packages."""

import hashlib
import os
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

from alira.kernel.module.dependencies.types import DependencyError


def _included_paths(content: str, source: Path):
    content = content.replace("\\\r\n", "").replace("\\\n", "")
    for number, line in enumerate(content.splitlines(), 1):
        try:
            tokens = iter(shlex.split(line, comments=True))
            for token in tokens:
                if token in {"-r", "--requirement", "-c", "--constraint"}:
                    reference = next(tokens, None)
                    if reference is None:
                        raise ValueError(f"{token} requires a path")
                elif token.startswith(("--requirement=", "--constraint=")):
                    reference = token.partition("=")[2]
                elif token.startswith(("-r", "-c")) and len(token) > 2:
                    reference = token[2:]
                else:
                    continue
                if not reference:
                    raise ValueError(f"{token} requires a path")
                location = urlsplit(reference)
                if location.scheme in {"http", "https"}:
                    # The URL itself is hashed with the parent input. Remote
                    # content is resolved only by an explicit deployment.
                    continue
                if location.scheme and (
                    location.scheme != "file"
                    or location.netloc not in {"", "localhost"}
                ):
                    raise ValueError(
                        f"included requirements must be local files: {reference}"
                    )
                path = Path(
                    unquote(location.path) if location.scheme else reference
                ).expanduser()
                yield (path if path.is_absolute() else source.parent / path).resolve()
        except ValueError as exc:
            raise DependencyError(
                f"Invalid requirements include in {source}:{number}: {exc}"
            ) from exc


def requirements_fingerprint(path: Path | None) -> str | None:
    """Hash local -r/-c inputs; preserve remote URLs without fetching content."""
    if path is None:
        return None
    root = path.resolve()
    visited = set()
    digest = hashlib.sha256()

    def visit(source: Path) -> None:
        if source in visited:
            return
        visited.add(source)
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise DependencyError(
                f"Cannot read requirements input {source}: {exc}"
            ) from exc
        relative = os.path.relpath(source, root.parent).encode("utf-8")
        for value in (relative, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DependencyError(
                f"Requirements input must be UTF-8: {source}"
            ) from exc
        for included in _included_paths(text, source):
            visit(included)

    visit(root)
    return digest.hexdigest()
