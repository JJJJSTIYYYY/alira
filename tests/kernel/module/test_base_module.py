from pathlib import Path

from alira.kernel.module import BaseModule


def test_declaration_is_independent_of_requirements_file_existence(tmp_path):
    module = BaseModule(
        "example",
        "1.0",
        required_modules=["database"],
        requirements_txt=tmp_path / "not-yet-validated.txt",
    )
    assert module.name == "example"
    assert module.required_modules == ["database"]
    assert isinstance(module.requirements_txt, Path)
    assert not hasattr(module, "install")
    assert not hasattr(module, "uninstall")


async def test_startup_only_module_can_be_stopped(module_manager_factory, tmp_path):
    class StartupOnly(BaseModule):
        async def startup(self):
            self.running = True

    module = StartupOnly("startup-only", "1.0")
    manager = module_manager_factory(tmp_path)
    manager.register(module)
    await manager.load("startup-only")
    assert module.running
    await manager.stop()
    assert not manager.loaded
    assert not manager.dependencies
