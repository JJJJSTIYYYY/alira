"""Read declarative module metadata without importing module code."""

import ast
import keyword
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from alira._version import __version__
from alira.kernel.module.dependencies.types import DependencyError


def module_directory(root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name.isidentifier()
        or keyword.iskeyword(name)
        or name.startswith("__")
    ):
        raise DependencyError(f"Invalid module name: {name}")
    directory = root / name
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        raise DependencyError(f"Module directory must remain inside {root}: {name}")
    return directory


def _module_declarations(tree: ast.Module) -> list[tuple[ast.Call, list[ast.expr]]]:
    """Recognize imported BaseModule names and constructors without importing them."""
    public_modules = {
        "alira.kernel",
        "alira.kernel.module",
        "alira.kernel.module.base_module",
    }
    bases = {"BaseModule"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                imported = f"{node.module}.{alias.name}"
                local = alias.asname or alias.name
                if node.module in public_modules and alias.name == "BaseModule":
                    bases.add(local)
                elif imported in public_modules:
                    bases.add(local + ".BaseModule")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in public_modules:
                    bases.add((alias.asname or alias.name) + ".BaseModule")

    constructors = set(bases)
    module_classes = set(bases)
    declarations = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not any(
            ast.unparse(base) in module_classes for base in node.bases
        ):
            continue
        module_classes.add(node.name)
        initializer = next(
            (
                item
                for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name == "__init__"
            ),
            None,
        )
        if initializer is None:
            if any(ast.unparse(base) in constructors for base in node.bases):
                constructors.add(node.name)
            continue
        for call in ast.walk(initializer):
            if not isinstance(call, ast.Call) or not isinstance(
                call.func, ast.Attribute
            ):
                continue
            if call.func.attr != "__init__":
                continue
            owner = call.func.value
            if ast.unparse(owner) in bases:
                declarations.append((call, call.args[1:]))
            elif (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Name)
                and owner.func.id == "super"
            ):
                declarations.append((call, call.args))
    declarations.extend(
        (node, node.args)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in constructors
    )
    return declarations


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    directory: Path
    requirements: Path | None
    required_modules: tuple[str, ...] = ()

    @classmethod
    def read(
        cls,
        directory: Path,
        *,
        name: str | None = None,
        default_name: str | None = None,
        modules_dir: Path | None = None,
    ) -> "ModuleSpec":
        """Read literals in BaseModule(...) or super().__init__(...).

        Module management never evaluates expressions, calls constructors or
        imports an entry. Dynamic metadata must be made declarative first.
        During archive validation, modules_dir is the eventual installation
        root; references to this module resolve against its extracted source.
        """
        entry = directory / "main.py"
        try:
            tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
        except (OSError, SyntaxError) as exc:
            raise DependencyError(
                f"Cannot read module declaration {entry}: {exc}"
            ) from exc
        constants = {"__version__": __version__}

        def literal(node):
            if isinstance(node, ast.Name) and node.id in constants:
                return constants[node.id]
            return ast.literal_eval(node)

        for statement in tree.body:
            if (
                isinstance(statement, (ast.Assign, ast.AnnAssign))
                and statement.value is not None
            ):
                try:
                    value = literal(statement.value)
                except (ValueError, TypeError):
                    continue
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value

        declarations = _module_declarations(tree)
        if len(declarations) > 1:
            raise DependencyError(f"Ambiguous module declaration: {entry}")
        metadata = {}
        if declarations:
            call, arguments = declarations[0]
            try:
                if arguments:
                    metadata["name"] = literal(arguments[0])
                for item in call.keywords:
                    if item.arg in {"name", "required_modules", "requirements_txt"}:
                        metadata[item.arg] = literal(item.value)
                    elif item.arg is None:
                        metadata.update(literal(item.value))
            except (ValueError, TypeError) as exc:
                raise DependencyError(
                    f"Module metadata must use literal values: {entry}"
                ) from exc
        declared_name = metadata.get("name", name or default_name or directory.name)
        if name is not None and declared_name != name:
            raise DependencyError(
                f"Module name {declared_name!r} does not match directory {name!r}"
            )
        module_directory(directory.parent, declared_name)
        required = metadata.get("required_modules")
        if required is None:
            required = []
        if not isinstance(required, (list, tuple)) or not all(
            isinstance(item, str) for item in required
        ):
            raise DependencyError(
                f"required_modules must be a list of module names: {entry}"
            )
        declared = metadata.get("requirements_txt")
        if declared is not None and not isinstance(declared, str):
            raise DependencyError(
                f"requirements_txt must be a literal path string: {entry}"
            )
        requirements = (
            (modules_dir or directory.parent) / declared
            if declared is not None
            else directory / "requirements.txt"
        ).resolve()
        if modules_dir is not None:
            installed = (modules_dir / declared_name).resolve()
            if requirements.is_relative_to(installed):
                requirements = directory / requirements.relative_to(installed)
        requirements = requirements.resolve()
        if not requirements.is_file():
            if declared is not None:
                raise DependencyError(
                    f"Module {declared_name}: requirements file not found: {requirements}"
                )
            requirements = None
        return cls(declared_name, directory, requirements, tuple(required))


class ModuleCatalog:
    def __init__(self, directory: Path):
        self.directory = directory

    def names(self) -> list[str]:
        return (
            sorted(
                path.name
                for path in self.directory.iterdir()
                if path.is_dir()
                and not path.name.startswith(".")
                and not path.name.startswith("__")
                and (path / "main.py").is_file()
            )
            if self.directory.is_dir()
            else []
        )

    def select(self, names: tuple[str, ...] | list[str]) -> dict[str, ModuleSpec]:
        selected = {}

        def visit(name):
            if name in selected:
                return
            directory = module_directory(self.directory, name)
            if not (directory / "main.py").is_file():
                raise DependencyError(f"Module not found: {name}")
            spec = ModuleSpec.read(directory, name=name)
            selected[name] = spec
            for dependency in spec.required_modules:
                visit(dependency)

        for name in names:
            visit(name)
        try:
            tuple(
                TopologicalSorter(
                    {
                        name: set(spec.required_modules)
                        for name, spec in selected.items()
                    }
                ).static_order()
            )
        except CycleError as exc:
            raise DependencyError(f"Module dependency cycle: {exc.args[1]}") from exc
        return selected
