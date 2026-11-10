from pathlib import Path

from alira.kernel.module import BaseModule


def test_declaration_is_independent_of_requirements_file_existence(tmp_path):
    module = BaseModule(
        "example", "1.0", required_modules=["database"],
        requirements_txt=tmp_path / "not-yet-validated.txt",
    )
    assert module.name == "example"
    assert module.required_modules == ["database"]
    assert isinstance(module.requirements_txt, Path)
    assert not hasattr(module, "install")
    assert not hasattr(module, "uninstall")
