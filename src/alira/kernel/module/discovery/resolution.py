"""Resolve module names, versions and kernel compatibility before lifecycle hooks."""

import keyword
import re
from collections.abc import Callable, Collection
from graphlib import CycleError, TopologicalSorter
from typing import Protocol

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from alira._version import __version__
from alira.kernel.module.dependencies.types import DependencyError


class ModuleDeclaration(Protocol):
    """Metadata shared by initialized declarations and statically inspected sources."""

    name: str
    version: str
    required_modules: Collection[str]
    required_kernel: str


def validate_module_name(name: str) -> None:
    """Reject invalid identifiers, Python keywords and reserved package names."""
    if (
        not isinstance(name, str)
        or not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("__")
    ):
        raise DependencyError(f"Invalid module name: {name}")


def parse_module_requirement(value: str) -> tuple[str, SpecifierSet]:
    """Parse an exact module identifier and optional PEP 440 constraint.

    Module identifiers retain their spelling, including underscores and Unicode.
    Package extras, URLs and environment markers are not module requirements.
    Invalid syntax raises DependencyError before dependency traversal.
    """
    if not isinstance(value, str):
        raise DependencyError(f"Invalid module requirement: {value!r}")
    match = re.fullmatch(r"([^\s<>=!~]+)\s*(.*)", value.strip())
    if match is None:
        raise DependencyError(f"Invalid module requirement: {value!r}")
    try:
        validate_module_name(match[1])
    except DependencyError as exc:
        raise DependencyError(f"Invalid module requirement: {value!r}") from exc
    try:
        return match[1], SpecifierSet(match[2])
    except InvalidSpecifier as exc:
        raise DependencyError(f"Invalid module requirement: {value!r}") from exc


def resolve_module_graph(
    names: Collection[str], get_module: Callable[[str], ModuleDeclaration]
) -> dict[str, set[str]]:
    """Resolve and validate a selected closure without invoking startup hooks.

    The provider supplies runtime declarations or static source metadata. Nodes are
    recorded before iterative traversal of prerequisites so cycles terminate without
    recursion, and are reported by
    TopologicalSorter. Every incoming constraint must match the one selected module
    version; incompatible consumers therefore fail the whole preflight. Kernel and
    module versions use PEP 440 comparisons, including prerelease semantics.
    """
    graph = {}
    declarations = {}
    constraints = {}

    pending = list(reversed(tuple(names)))
    while pending:
        name = pending.pop()
        if name in graph:
            continue
        validate_module_name(name)
        declaration = get_module(name)
        if declaration.name != name:
            raise DependencyError(
                f"Module name {declaration.name!r} does not match {name!r}"
            )
        if not isinstance(declaration.required_modules, (list, tuple)):
            raise DependencyError(
                f"required_modules must be a list of module requirements: {name}"
            )
        declarations[name] = declaration
        graph[name] = set()
        dependencies = []
        for value in declaration.required_modules:
            dependency, specifier = parse_module_requirement(value)
            graph[name].add(dependency)
            constraints.setdefault(dependency, []).append((name, specifier))
            dependencies.append(dependency)
        pending.extend(reversed(dependencies))
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise DependencyError(f"Module dependency cycle: {exc.args[1]}") from exc
    for name, declaration in declarations.items():
        try:
            version = Version(declaration.version)
            kernel = SpecifierSet(declaration.required_kernel)
        except (InvalidVersion, InvalidSpecifier, TypeError) as exc:
            raise DependencyError(
                f"Invalid version metadata for module {name}: {exc}"
            ) from exc
        if not kernel.contains(Version(__version__)):
            raise DependencyError(
                f"Module {name} requires kernel{kernel}, current kernel is {__version__}"
            )
        incoming = constraints.get(name, ())
        if any(
            specifier and not specifier.contains(version) for _, specifier in incoming
        ):
            requirements = ", ".join(
                f"{owner} requires {name}{spec}" for owner, spec in incoming
            )
            raise DependencyError(
                f"Module version conflict: {requirements}; selected {name}=={version}"
            )
    return graph
