import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gemini_issue_fix as gif  # noqa: E402
import parse_pr_changelog  # noqa: E402


@pytest.mark.parametrize("path", [
    "app/scraper.py", "tests/test_x.py", "docs/guide.md", "README.md", "app/new/pkg/mod.py",
])
def test_writable_paths_allowed(path):
    assert gif.check_writable(path) is None


@pytest.mark.parametrize("path", [
    ".github/workflows/evil.yml", "requirements.txt", "Dockerfile", "data/app.db",
    "../outside.py", "app/../.github/x.yml", "/etc/passwd", "app\\x.py", "app//x.py",
    "./app/x.py", " app/x.py", "", "CHANGELOG.md", "scripts/telegram_notify.py",
])
def test_unsafe_paths_rejected(path):
    assert gif.check_writable(path) is not None


def test_apply_edits_modifies_and_creates(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mod.py").write_text("a = 1\nb = 2\n")
    changed = gif.apply_edits(tmp_path, [
        {"path": "app/mod.py", "old_text": "b = 2", "new_text": "b = 3"},
        {"path": "tests/test_mod.py", "old_text": "", "new_text": "def test(): pass\n"},
    ])
    assert changed == ["app/mod.py", "tests/test_mod.py"]
    assert (tmp_path / "app" / "mod.py").read_text() == "a = 1\nb = 3\n"
    assert (tmp_path / "tests" / "test_mod.py").read_text() == "def test(): pass\n"


@pytest.mark.parametrize("old,why", [("zzz", "0 times"), ("x", "2 times")])
def test_apply_edits_requires_exactly_one_match_and_writes_nothing(tmp_path, old, why):
    (tmp_path / "app").mkdir()
    target = tmp_path / "app" / "mod.py"
    target.write_text("x = 1\nx = 2\n")
    with pytest.raises(ValueError, match="exactly once"):
        gif.apply_edits(tmp_path, [
            {"path": "tests/ok.py", "old_text": "", "new_text": "pass\n"},
            {"path": "app/mod.py", "old_text": old, "new_text": "y"},
        ])
    assert target.read_text() == "x = 1\nx = 2\n"
    assert not (tmp_path / "tests" / "ok.py").exists()  # atomic: nothing written


def test_apply_edits_rejects_bad_inputs(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mod.py").write_text("a = 1\n")
    with pytest.raises(ValueError, match="no edits"):
        gif.apply_edits(tmp_path, [])
    with pytest.raises(ValueError, match="allowlist"):
        gif.apply_edits(tmp_path, [{"path": ".github/w.yml", "old_text": "", "new_text": "x"}])
    with pytest.raises(ValueError, match="already exists"):
        gif.apply_edits(tmp_path, [{"path": "app/mod.py", "old_text": "", "new_text": "x"}])
    with pytest.raises(ValueError, match="does not exist"):
        gif.apply_edits(tmp_path, [{"path": "app/nope.py", "old_text": "a", "new_text": "b"}])


def test_pr_body_passes_the_repos_own_checklist_parser():
    fix = {"bump": "patch", "changelog": "- Fixed the thing.", "pr_notes": "Changed x."}
    body = gif.pr_body(fix, "36", "It was real.")
    bump, description = parse_pr_changelog.parse(body)
    assert bump == "patch"
    assert description == "- Fixed the thing."
    assert body.startswith("Fixes #36.")


@pytest.mark.parametrize("bump", ["patch", "minor", "major"])
def test_pr_body_checks_exactly_the_chosen_box(bump):
    body = gif.pr_body({"bump": bump, "changelog": "- x", "pr_notes": ""}, "1", "r")
    assert parse_pr_changelog.parse(body)[0] == bump


def _run_main(tmp_path, monkeypatch, gemini_replies, applied_check=None):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mod.py").write_text("value = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(sys, "argv", ["gemini_issue_fix.py", "7"])
    issue = {"number": 7, "title": "Broken", "body": "It is broken", "url": "https://x/7",
             "author": {"login": "bob"}, "labels": [], "comments": []}
    comments = []
    with patch.object(gif, "fetch_issue", return_value=issue), \
            patch.object(gif, "list_repo_files", return_value=["app/mod.py"]), \
            patch.object(gif, "post_issue_comment", side_effect=lambda n, b: comments.append(b)), \
            patch.object(gif, "call_gemini", side_effect=gemini_replies) as gem:
        gif.main()
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    return outputs, comments, gem


def test_main_real_fixable_applies_fix_and_writes_messages(tmp_path, monkeypatch):
    replies = [
        {"verdict": "REAL_FIXABLE", "summary": "Real bug.", "reasoning": "Because.",
         "files_to_read": ["app/mod.py", ".github/workflows/x.yml"]},
        {"pr_title": "Fix value", "bump": "patch", "changelog": "- Fixed value.", "pr_notes": "n",
         "edits": [{"path": "app/mod.py", "old_text": "value = 1", "new_text": "value = 2"}]},
    ]
    outputs, comments, gem = _run_main(tmp_path, monkeypatch, replies)
    assert outputs == {"verdict": "REAL_FIXABLE", "has_changes": "true",
                       "pr_title": "Fix value", "branch": "gemini-issue-7"}
    assert (tmp_path / "app" / "mod.py").read_text() == "value = 2\n"
    assert "Fixes #7." in (tmp_path / "pr_body.md").read_text()
    assert "app/mod.py" in (tmp_path / "telegram_pr.txt").read_text()
    assert comments and comments[0].startswith(gif.MARKER)
    # The disallowed file Gemini asked to read must not have been sent back to it.
    assert ".github" not in gem.call_args_list[1].args[0].split("File contents:")[1]


def test_main_not_real_makes_no_changes(tmp_path, monkeypatch):
    replies = [{"verdict": "NOT_REAL", "summary": "Works as intended.", "reasoning": "r", "files_to_read": []}]
    outputs, comments, gem = _run_main(tmp_path, monkeypatch, replies)
    assert outputs["has_changes"] == "false"
    assert gem.call_count == 1
    assert "No PR was opened" in (tmp_path / "telegram_eval.txt").read_text()
    assert (tmp_path / "app" / "mod.py").read_text() == "value = 1\n"


def test_main_retries_once_when_edits_do_not_apply(tmp_path, monkeypatch):
    bad = {"pr_title": "t", "bump": "patch", "changelog": "- c", "pr_notes": "",
           "edits": [{"path": "app/mod.py", "old_text": "nope", "new_text": "x"}]}
    good = {**bad, "edits": [{"path": "app/mod.py", "old_text": "value = 1", "new_text": "value = 5"}]}
    triage = {"verdict": "REAL_FIXABLE", "summary": "s", "reasoning": "r", "files_to_read": ["app/mod.py"]}
    outputs, _, gem = _run_main(tmp_path, monkeypatch, [triage, bad, good])
    assert outputs["has_changes"] == "true"
    assert "could not be applied" in gem.call_args_list[2].args[0]


def test_main_gives_up_after_two_bad_attempts_without_failing(tmp_path, monkeypatch):
    bad = {"pr_title": "t", "bump": "patch", "changelog": "- c", "pr_notes": "",
           "edits": [{"path": ".github/workflows/x.yml", "old_text": "", "new_text": "x"}]}
    triage = {"verdict": "REAL_FIXABLE", "summary": "s", "reasoning": "r", "files_to_read": []}
    outputs, comments, _ = _run_main(tmp_path, monkeypatch, [triage, bad, bad])
    assert outputs["has_changes"] == "false"
    assert "Needs a human" in comments[0]
    assert "Could not draft a valid patch" in (tmp_path / "telegram_eval.txt").read_text()


def test_main_exits_1_when_gemini_fails(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(tmp_path, monkeypatch, RuntimeError("quota"))
    assert exc.value.code == 1
