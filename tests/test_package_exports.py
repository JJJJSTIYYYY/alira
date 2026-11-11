"""Public package exports work without import cycles or eager business imports."""

import ast
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first_import", [
    "alira.utils.config", "alira.utils.yaml", "alira.shared.application.config",
    "alira.shared.logger", "alira.kernel.module",
    "alira.kernel.lifespan.apixis_core", "alira.kernel.module.registry",
    "alira.kernel.module.runtime", "alira.kernel.module.dependencies.deployment",
    "alira.cli",
])
def test_public_exports_are_complete_and_independent_of_import_order(first_import):
    script = f'''
from importlib import import_module
import sys

import_module({first_import!r})
if {first_import!r}.startswith("alira.utils"):
    assert not any(name.startswith(("alira.shared", "alira.kernel", "alira.modules")) for name in sys.modules)
utils = import_module("alira.utils")
shared = import_module("alira.shared")
kernel = import_module("alira.kernel")
lifespan = import_module("alira.kernel.lifespan")
modules = import_module("alira.kernel.module")
dependencies = import_module("alira.kernel.module.dependencies")
cli = import_module("alira.cli")
commands = import_module("alira.cli.commands")
assert not any(name in sys.modules for name in ("pydantic", "my_llm", "fastapi", "uvicorn"))
assert not any(name.startswith("alira.modules.") for name in sys.modules)
assert kernel.auto_init._services == []
for package in (utils, shared, kernel, lifespan, modules, dependencies, cli, commands):
    assert len(package.__all__) == len(set(package.__all__))
    for name in package.__all__:
        assert getattr(package, name) is not None
assert shared.logger is import_module("alira.shared.logger").logger
assert shared.Logger is import_module("alira.shared.logger").Logger
assert utils.YamlConfig is import_module("alira.utils.config").YamlConfig
assert shared.config is import_module("alira.shared.application.config")
assert kernel.module_manager is import_module("alira.kernel.module").module_manager
assert kernel.lifespan is import_module("alira.kernel.initialize").lifespan
assert kernel.apixis_core is lifespan.apixis_core
assert kernel.DependencyError is modules.DependencyError is dependencies.DependencyError
'''
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(("layer", "forbidden"), [
    ("utils", ("alira.shared", "alira.kernel", "alira.modules")),
    ("shared", ("alira.kernel", "alira.modules")),
    ("kernel", ("alira.modules",)),
])
def test_public_layers_only_import_lower_layers(layer, forbidden):
    root = Path(__file__).resolve().parents[1] / "src/alira" / layer
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                assert not any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden), (
                    path, node.lineno, name,
                )


def test_pure_test_suites_do_not_import_business_implementations_from_fixtures():
    root = Path(__file__).resolve().parents[1]
    script = f'''
import importlib.abc
import os
import sys

class BusinessImportsForbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "alira.modules" or fullname.startswith("alira.modules."):
            raise AssertionError(f"Pure test fixtures imported business code: {{fullname}}")
        if fullname.split(".", 1)[0] in {{"my_llm", "pydantic", "fastapi", "uvicorn", "sqlalchemy", "fastmcp"}}:
            raise AssertionError(f"Pure test fixtures imported an optional dependency: {{fullname}}")

os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.meta_path.insert(0, BusinessImportsForbidden())
import pytest
raise SystemExit(pytest.main([
    "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "-q",
    {str(root / "tests/shared/application/test_version.py")!r},
    {str(root / "tests/utils/test_config.py")!r},
    {str(root / "tests/kernel/lifespan/test_auto_init.py")!r},
]))
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
