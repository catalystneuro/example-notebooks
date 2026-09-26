"""Tests for per-entry health tracking (.github/scripts/entry_health.py)."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from entry_health import (  # noqa: E402
    FAILING_AFTER, UNMAINTAINED_DAYS, new_issue_body, update_state,
)
from registry import Entry  # noqa: E402

ENTRIES = [
    Entry("001550/PaganLab", {"name": "001550-paganlab", "maintainers": [{"github": "alice"}]}),
    Entry("000458/AllenInstitute", {"name": "000458-alleninstitute", "maintainers": [{"github": "bob"}]}),
]
T0 = datetime.datetime(2026, 1, 5, 6, tzinfo=datetime.timezone.utc)
WEEK = datetime.timedelta(days=7)


def result(nb, ok, stage="done"):
    return {"notebook": nb, "ok": ok, "stage": stage if ok else "execute",
            "duration_s": 10, "error": None if ok else "Traceback\nValueError: boom"}


PASS = [result("001550/PaganLab/a.ipynb", True), result("000458/AllenInstitute/b.ipynb", True)]
FAIL_A = [result("001550/PaganLab/a.ipynb", False), result("000458/AllenInstitute/b.ipynb", True)]


def run(state, results, when, activity=None):
    return update_state(state, results, ENTRIES, activity or {}, when, "https://run")


def test_all_pass():
    state, outcomes = run({}, PASS, T0)
    assert {o.status for o in outcomes} == {"passing"}
    assert state["entries"]["001550-paganlab"]["last_pass"] == T0.isoformat()


def test_single_failure_is_not_yet_failing():
    state, outcomes = run({}, FAIL_A, T0)
    (a,) = [o for o in outcomes if o.name == "001550-paganlab"]
    assert a.failed and a.status == "passing" and a.consecutive_failures == 1
    assert "ValueError: boom" in state["entries"]["001550-paganlab"]["notebooks"][
        "001550/PaganLab/a.ipynb"]["error"]


def test_consecutive_failures_become_failing_then_recover():
    state = {}
    for i in range(FAILING_AFTER):
        state, outcomes = run(state, FAIL_A, T0 + i * WEEK)
    assert state["entries"]["001550-paganlab"]["status"] == "failing"
    state, _ = run(state, PASS, T0 + FAILING_AFTER * WEEK)
    rec = state["entries"]["001550-paganlab"]
    assert rec["status"] == "passing" and rec["consecutive_failures"] == 0 and rec["first_failure"] is None


def test_long_quiet_failure_becomes_unmaintained():
    state = {}
    weeks = UNMAINTAINED_DAYS // 7 + 2
    for i in range(weeks):
        state, _ = run(state, FAIL_A, T0 + i * WEEK)
    assert state["entries"]["001550-paganlab"]["status"] == "unmaintained"


def test_maintainer_activity_prevents_unmaintained():
    state = {}
    weeks = UNMAINTAINED_DAYS // 7 + 2
    active = {"001550/PaganLab": T0 + 3 * WEEK}      # a commit after the first failure
    for i in range(weeks):
        state, _ = run(state, FAIL_A, T0 + i * WEEK, activity=active)
    assert state["entries"]["001550-paganlab"]["status"] == "failing"


def test_untested_entries_keep_state_and_removed_entries_drop():
    state, _ = run({}, FAIL_A, T0)
    state["entries"]["gone-entry"] = {"status": "passing"}
    only_b = [result("000458/AllenInstitute/b.ipynb", True)]
    state, outcomes = run(state, only_b, T0 + WEEK)
    assert [o.name for o in outcomes] == ["000458-alleninstitute"]
    assert state["entries"]["001550-paganlab"]["consecutive_failures"] == 1
    assert "gone-entry" not in state["entries"]


def test_issue_body_mentions_maintainers_and_marker():
    _, outcomes = run({}, FAIL_A, T0)
    (a,) = [o for o in outcomes if o.failed]
    body = new_issue_body(a, "https://run")
    assert "<!-- registry-entry:001550-paganlab -->" in body and "@alice" in body
