"""Tests for the notebook test harness (.github/scripts/run_notebook.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_notebook import helper_target  # noqa: E402


@pytest.mark.parametrize("cmd, expected", [
    ("curl -sL -o plot_utils.py https://example.org/a/plot_utils.py", "plot_utils.py"),
    ("curl --create-dirs -sL -o helpers/stream.py https://example.org/helpers/stream.py",
     "helpers/stream.py"),
])
def test_helper_target_reads_the_output_path(tmp_path, cmd, expected):
    assert helper_target(cmd, tmp_path) == tmp_path / expected


def test_helper_target_is_none_without_an_output_flag(tmp_path):
    assert helper_target("wget https://example.org/a/plot_utils.py", tmp_path) is None
