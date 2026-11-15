from pathlib import Path

from alira.kernel.module import BaseModule, ModuleStatus


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


async def test_default_health_remains_false_across_lifecycle():
    module = BaseModule("example", "1.0")
    expected = {"health": False, "message": "Health check is not implemented."}

    assert await module.health() == expected
    await module.startup()
    assert await module.health() == expected
    await module.shutdown()
    assert await module.health() == expected


async def test_health_override_reports_owned_resource_state(module_manager_factory):
    class CheckedModule(BaseModule):
        running = False

        async def startup(self) -> None:
            self.running = True

        async def shutdown(self) -> None:
            self.running = False

        async def health(self) -> ModuleStatus:
            return {
                "health": self.running,
                "message": "" if self.running else "Service is not running.",
            }

    module = CheckedModule("example", "1.0")
    manager = module_manager_factory()
    manager.register(module)

    assert (await module.health())["health"] is False
    assert not manager.loaded
    try:
        await manager.load("example")
        assert await module.health() == {"health": True, "message": ""}
        assert manager.loaded == {"example"}
    finally:
        await manager.stop()
    assert await module.health() == {
        "health": False,
        "message": "Service is not running.",
    }
    assert not manager.loaded
