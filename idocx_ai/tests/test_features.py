"""Run every feature's no-model check under pytest, so one `pytest` call covers all features.

Each script in tests/checks/ scores one feature's tools against the made-up samples (and their
ground_truth.json) in samples/<feature>/. It exits non-zero if any of its checks fail and writes
a readable report to tests/output/<feature>/report.txt. None of them use the network or the model.
"""

import subprocess
import sys
from pathlib import Path

import pytest

CHECKS = sorted((Path(__file__).parent / "checks").glob("*.py"))


@pytest.mark.parametrize("check", CHECKS, ids=lambda p: p.stem)
def test_feature_tools(check: Path) -> None:
    """The feature's check script passes every one of its checks."""
    run = subprocess.run(  # noqa: S603 -- runs our own check script with this interpreter
        [sys.executable, str(check)], capture_output=True, text=True, timeout=300, check=False
    )
    assert run.returncode == 0, f"{check.name} failed:\n{run.stdout[-4000:]}{run.stderr[-2000:]}"
