"""Management reads declarations as data and never executes module code."""

import pytest

from alira.kernel.module.catalog import ModuleCatalog, ModuleSpec
from alira.kernel.module.dependencies import DependencyError


def entry(root, name, code):
    folder = root / name
    folder.mkdir()
    (folder / "main.py").write_text(code)
    return folder


def test_listing_does_not_parse_entries_or_dependencies(tmp_path):
    entry(tmp_path, "demo", "invalid syntax !!!")
    entry(tmp_path, "pending", "raise RuntimeError('not initialized')")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".staging").mkdir()
    assert ModuleCatalog(tmp_path).names() == ["demo", "pending"]


@pytest.mark.parametrize(
    "code",
    [
        "module = BaseModule('demo', '1', required_modules=['base'])",
        "class Demo(BaseModule): pass\nmodule = Demo('demo', '1', required_modules=['base'])",
        "class Demo(BaseModule):\n    def __init__(self):\n        super().__init__(name='demo', version=__version__, required_modules=['base'])\nmodule=Demo()",
    ],
)
def test_metadata_is_read_without_executing_any_statements(tmp_path, code):
    folder = entry(tmp_path, "demo", "raise RuntimeError('must not execute')\n" + code)
    spec = ModuleSpec.read(folder, name="demo")
    assert spec.name == "demo"
    assert spec.required_modules == ("base",)
    assert spec.requirements is None


def test_requirements_path_and_top_level_constants(tmp_path):
    folder = entry(
        tmp_path,
        "demo",
        "NAME='demo'\nDEPENDENCIES=['base']\nmodule=BaseModule(NAME, '1', required_modules=DEPENDENCIES, requirements_txt='demo/custom.txt')",
    )
    requirements = folder / "custom.txt"
    requirements.write_text("package>=1\n")
    assert ModuleSpec.read(folder).requirements == requirements


@pytest.mark.parametrize(
    "code, message",
    [
        ("module=BaseModule('other','1')", "does not match"),
        ("module=BaseModule(get_name(),'1')", "literal"),
        ("module=BaseModule('demo','1', required_modules='base')", "list"),
        ("module=BaseModule('demo','1', requirements_txt='missing.txt')", "not found"),
        ("a=BaseModule('demo','1')\nb=BaseModule('demo','2')", "Ambiguous"),
    ],
)
def test_invalid_metadata_is_reported_before_any_execution(tmp_path, code, message):
    folder = entry(tmp_path, "demo", code)
    with pytest.raises(DependencyError, match=message):
        ModuleSpec.read(folder, name="demo")


def test_selection_reports_missing_prerequisites_and_cycles(tmp_path):
    entry(tmp_path, "a", "module=BaseModule('a','1',required_modules=['b'])")
    catalog = ModuleCatalog(tmp_path)
    with pytest.raises(DependencyError, match="Module not found: b"):
        catalog.select(["a"])
    entry(tmp_path, "b", "module=BaseModule('b','1',required_modules=['a'])")
    with pytest.raises(DependencyError, match="cycle"):
        catalog.select(["a"])


def test_management_ignores_dynamic_fields_unrelated_to_dependencies(tmp_path):
    folder = entry(
        tmp_path,
        "demo",
        "module=BaseModule('demo', get_version(), description=get_description())",
    )
    assert ModuleSpec.read(folder, name="demo").name == "demo"


def test_archive_default_name_is_independent_of_github_wrapper(tmp_path):
    folder = entry(tmp_path, "repository-commit", "# Pending module\n")
    assert ModuleSpec.read(folder, default_name="plugin").name == "plugin"


@pytest.mark.parametrize(
    "code",
    [
        "from alira.kernel import BaseModule as Module\nmodule=Module('demo','1',required_modules=['missing'])",
        "import alira.kernel.module as modules\nmodule=modules.BaseModule('demo','1',required_modules=['missing'])",
        "from alira.kernel import module as modules\nmodule=modules.BaseModule('demo','1',required_modules=['missing'])",
        "class Demo(BaseModule):\n    def __init__(self):\n        BaseModule.__init__(self, 'demo', '1', required_modules=['missing'])\nmodule=Demo()",
        "DEPENDENCIES: list[str] = ['missing']\nmodule=BaseModule('demo','1',required_modules=DEPENDENCIES)",
    ],
)
def test_declaration_syntax_does_not_hide_missing_prerequisites(tmp_path, code):
    entry(tmp_path, "demo", code)
    with pytest.raises(DependencyError, match="Module not found: missing"):
        ModuleCatalog(tmp_path).select(["demo"])


def test_unrelated_super_calls_are_not_module_declarations(tmp_path):
    folder = entry(
        tmp_path,
        "demo",
        "class Helper:\n    def __init__(self):\n        super().__init__()\n"
        "class Demo(BaseModule):\n    def __init__(self):\n        super().__init__('demo','1', required_modules=['base'])\n"
        "module=Demo()\n",
    )
    assert ModuleSpec.read(folder).required_modules == ("base",)


def test_archive_references_to_other_modules_use_installation_root(tmp_path):
    modules = tmp_path / "modules"
    requirements = modules / "base/deps.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("package==1\n")
    folder = entry(
        tmp_path,
        "extracted",
        "module=BaseModule('demo','1', requirements_txt='demo/../base/deps.txt')",
    )
    assert ModuleSpec.read(folder, modules_dir=modules).requirements == requirements
