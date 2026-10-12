"""Tests for the merge bot's decision logic (.github/scripts/merge_bot.py)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from merge_bot import (  # noqa: E402
    ChangedFile, approvers_comment, check_problems, evaluate,
)

MAIN = {
    "001550/PaganLab": {
        "name": "001550-paganlab",
        "maintainers": [{"github": "alice"}],
        "collections": ["dandi"],
    },
    "000458/AllenInstitute": {
        "name": "000458-alleninstitute",
        "maintainers": [{"github": "bob"}],
        "collections": ["dandi"],
    },
}
COLLECTIONS = {
    "dandi": {"curators": ["carol"]},
    "general": {"curators": ["core-person"]},
    "journal-x": {"curators": ["dave"]},
}


def refusals(changed, actor, head=None, core=False):
    ev = evaluate(changed, MAIN, COLLECTIONS, head or {})
    return ev.refusals(actor, core)


def test_maintainer_can_merge_own_entry():
    changed = [ChangedFile("001550/PaganLab/01_behavior_demo.ipynb", "modified")]
    assert refusals(changed, "alice") == []
    assert refusals(changed, "ALICE") == []          # logins are case-insensitive


def test_other_users_cannot():
    changed = [ChangedFile("001550/PaganLab/01_behavior_demo.ipynb", "modified")]
    assert refusals(changed, "bob")
    assert refusals(changed, "mallory")


def test_every_touched_entry_needs_the_actor():
    changed = [ChangedFile("001550/PaganLab/a.ipynb", "modified"),
               ChangedFile("000458/AllenInstitute/b.ipynb", "modified")]
    (reason,) = refusals(changed, "alice")
    assert "000458-alleninstitute" in reason


def test_files_outside_entries_need_core():
    changed = [ChangedFile("001550/PaganLab/a.ipynb", "modified"),
               ChangedFile(".github/workflows/lint-registry.yml", "modified")]
    (reason,) = refusals(changed, "alice")
    assert "outside any entry" in reason
    assert refusals(changed, "anyone", core=True) == []


def test_moving_a_file_out_of_an_entry_needs_both_sides():
    changed = [ChangedFile("000458/AllenInstitute/a.ipynb", "renamed",
                           previous_path="001550/PaganLab/a.ipynb")]
    assert refusals(changed, "alice")                 # not a maintainer of 000458
    assert refusals(changed, "bob")                   # not a maintainer of 001550


def test_maintainers_are_read_from_main_not_the_pr():
    # The PR adds mallory as a maintainer; mallory still can't merge it.
    head = {"001550/PaganLab": {"name": "001550-paganlab",
                                "maintainers": [{"github": "alice"}, {"github": "mallory"}],
                                "collections": ["dandi"]}}
    changed = [ChangedFile("001550/PaganLab/notebook.yaml", "modified")]
    assert refusals(changed, "mallory", head)
    assert refusals(changed, "alice", head) == []


def test_joining_a_collection_needs_its_curator():
    head = {"001550/PaganLab": {"name": "001550-paganlab",
                                "maintainers": [{"github": "alice"}],
                                "collections": ["dandi", "journal-x"]}}
    changed = [ChangedFile("001550/PaganLab/notebook.yaml", "modified")]
    (reason,) = refusals(changed, "alice", head)
    assert "journal-x" in reason
    # dave curates journal-x but doesn't maintain the entry.
    assert refusals(changed, "dave", head)


def test_new_entry_needs_a_curator():
    head = {"001999/NewLab": {"name": "001999-newlab",
                              "maintainers": [{"github": "erin"}],
                              "collections": ["dandi"]}}
    changed = [ChangedFile("001999/NewLab/notebook.yaml", "added"),
               ChangedFile("001999/NewLab/demo.ipynb", "added")]
    assert refusals(changed, "erin", head)             # its own maintainer can't self-admit
    assert refusals(changed, "carol", head) == []       # the dandi curator can


def test_new_entry_without_collections_goes_to_general():
    head = {"lab/x": {"name": "lab-x", "maintainers": [{"github": "erin"}]}}
    changed = [ChangedFile("lab/x/notebook.yaml", "added"), ChangedFile("lab/x/a.ipynb", "added")]
    assert refusals(changed, "core-person", head) == []
    assert refusals(changed, "carol", head)


def test_new_entry_in_unknown_collection_needs_core():
    head = {"lab/x": {"name": "lab-x", "collections": ["nope"]}}
    changed = [ChangedFile("lab/x/notebook.yaml", "added")]
    (reason,) = refusals(changed, "carol", head)
    assert "unknown collection" in reason


def test_unreadable_new_metadata_needs_core():
    changed = [ChangedFile("lab/x/notebook.yaml", "added")]
    assert refusals(changed, "carol", {"lab/x": None})


def test_deleting_an_entry_needs_core():
    changed = [ChangedFile("001550/PaganLab/notebook.yaml", "removed"),
               ChangedFile("001550/PaganLab/01_behavior_demo.ipynb", "removed")]
    (reason,) = refusals(changed, "alice")
    assert "deleting entry" in reason


def test_approvers_comment_mentions_maintainers_who_can_merge():
    changed = [ChangedFile("001550/PaganLab/a.ipynb", "modified")]
    body = approvers_comment(evaluate(changed, MAIN, COLLECTIONS, {}))
    assert "@alice" in body and "/merge" in body


def test_approvers_comment_does_not_ping_when_core_is_needed():
    changed = [ChangedFile("001550/PaganLab/a.ipynb", "modified"), ChangedFile("README.md", "modified")]
    body = approvers_comment(evaluate(changed, MAIN, COLLECTIONS, {}))
    assert "@alice" not in body and "`alice`" in body and "core maintainer" in body


# --- checks ------------------------------------------------------------------

def test_checks_all_green():
    runs = [{"name": "lint", "status": "completed", "conclusion": "success"},
            {"name": "cleanup", "status": "completed", "conclusion": "skipped"}]
    assert check_problems(runs, []) == []


def test_checks_pending_or_failed():
    runs = [{"name": "lint", "status": "in_progress", "conclusion": None},
            {"name": "Test x.ipynb", "status": "completed", "conclusion": "failure"}]
    problems = check_problems(runs, [{"context": "ci/other", "state": "error"}])
    assert len(problems) == 3


def test_bot_jobs_do_not_gate_themselves():
    runs = [{"name": "registry-bot-merge", "status": "in_progress", "conclusion": None}]
    assert check_problems(runs, []) == []


def test_quiet_mode_never_pings(monkeypatch):
    import registry
    monkeypatch.setattr(registry, "QUIET", True)
    changed = [ChangedFile("001550/PaganLab/a.ipynb", "modified")]
    body = approvers_comment(evaluate(changed, MAIN, COLLECTIONS, {}))
    assert "@alice" not in body and "`alice`" in body
