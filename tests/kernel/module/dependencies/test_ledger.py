"""Internal dependency state failures produce actionable public CLI messages."""

import subprocess
import sys

import pytest

from alira.kernel.module.dependencies import DependencyError
from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.kernel.module.dependencies.uv import EMPTY_LOCK


@pytest.mark.parametrize(
    "damage",
    ["missing-json", "missing-lock", "json", "lock", "mismatch", "schema"],
)
def test_invalid_records_hide_storage_and_parser_details(tmp_path, damage):
    ledger = DependencyLedger(tmp_path / "private-state")
    state = {"format": 1, "modules": {}}
    ledger.write(state, EMPTY_LOCK)
    if damage == "missing-json":
        ledger.path.unlink()
    elif damage == "missing-lock":
        ledger.lockfile.unlink()
    elif damage == "json":
        ledger.path.write_text("{invalid-json")
    elif damage == "lock":
        ledger.write(state, b"invalid-toml")
    elif damage == "mismatch":
        ledger.lockfile.write_bytes(EMPTY_LOCK + b"# changed\n")
    else:
        ledger.write({"format": 1, "modules": []}, EMPTY_LOCK)

    expected = "Module dependencies need to be synchronized.\nRepair: uv run alira sync"
    with pytest.raises(DependencyError) as caught:
        ledger.read()
    assert str(caught.value) == expected
    assert caught.value.__cause__ is not None

    # Exercise the actual CLI error handler, including suppression of the
    # original exception's traceback and filesystem/parser details.
    script = f"""
from pathlib import Path
from alira.kernel.module import module_manager
from alira.kernel.module.dependencies.ledger import DependencyLedger
from alira.main import main
module_manager._dependencies.ledger = DependencyLedger(Path({str(ledger.directory)!r}))
main(["remove", "demo"])
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == expected + "\n"
