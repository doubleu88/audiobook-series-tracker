import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parse_mod = _load("parse_pr_changelog")
latest_mod = _load("latest_changelog_entry")
insert_mod = _load("insert_changelog_entry")


def body(major=False, minor=False, patch=False, desc="Did a thing."):
    box = lambda c, n: f"- [{'x' if c else ' '}] {n}"
    return (
        "## Version bump\n\n"
        f"{box(major, 'Major')} (breaking)\n{box(minor, 'Minor')} (feature)\n{box(patch, 'Patch')} (fix)\n\n"
        f"## Changelog description\n<!-- describe it -->\n{desc}\n\n## Notes\nignored\n"
    )


def run_main(mod, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", [mod.__name__ + ".py", *args])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def run_main_maybe_exit(mod, monkeypatch, capsys, *args):
    """For mains that return normally on success."""
    monkeypatch.setattr(sys, "argv", [mod.__name__ + ".py", *args])
    try:
        mod.main()
        code = None
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


# ------------------------------------------------------------ parse()


@pytest.mark.parametrize("kw,expected", [({"major": True}, "major"), ({"minor": True}, "minor"), ({"patch": True}, "patch")])
def test_parse_bump_types(kw, expected):
    bump, desc = parse_mod.parse(body(**kw))
    assert bump == expected
    assert desc == "- Did a thing."


def test_parse_uppercase_x_accepted():
    b = body(patch=True).replace("[x]", "[X]")
    assert parse_mod.parse(b)[0] == "patch"


def test_parse_no_box_checked():
    with pytest.raises(ValueError, match="No version-bump box is checked"):
        parse_mod.parse(body())


def test_parse_multiple_boxes():
    with pytest.raises(ValueError, match=r"More than one.*major, minor"):
        parse_mod.parse(body(major=True, minor=True))


@pytest.mark.xfail(strict=True, reason="empty description followed by another ## section is not rejected: DESCRIPTION_HEADING_RE swallows the next section as the description")
def test_parse_empty_description():
    with pytest.raises(ValueError, match="empty"):
        parse_mod.parse(body(patch=True, desc=""))


@pytest.mark.xfail(strict=True, reason="empty description followed by another ## section is not rejected: DESCRIPTION_HEADING_RE swallows the next section as the description")
def test_parse_whitespace_description():
    with pytest.raises(ValueError, match="empty"):
        parse_mod.parse(body(patch=True, desc="   \n  "))


def test_parse_empty_description_at_eof():
    with pytest.raises(ValueError, match="empty"):
        parse_mod.parse("- [x] Patch\n\n## Changelog description\n\n")


def test_parse_missing_description_heading():
    with pytest.raises(ValueError, match="empty"):
        parse_mod.parse("- [x] Patch\n")


def test_parse_only_comment_description():
    with pytest.raises(ValueError, match="empty"):
        parse_mod.parse("- [x] Patch\n\n## Changelog description\n<!-- nothing -->\n")


def test_parse_existing_bullets_preserved():
    _, desc = parse_mod.parse(body(patch=True, desc="- one\n- two"))
    assert desc == "- one\n- two"


def test_parse_plain_lines_wrapped_as_bullets():
    _, desc = parse_mod.parse(body(patch=True, desc="first\n\n  second  "))
    assert desc == "- first\n- second"


def test_parse_description_at_end_of_body():
    b = "- [x] Minor\n\n## Changelog description\nlast section"
    assert parse_mod.parse(b) == ("minor", "- last section")


def test_parse_unrelated_checkboxes_ignored():
    b = body(patch=True) + "\n- [x] Tests added\n"
    assert parse_mod.parse(b)[0] == "patch"


# ------------------------------------------------------------ bump_version()


@pytest.mark.parametrize(
    "cur,kind,new",
    [
        ("1.2.3", "major", "2.0.0"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("0.0.0", "patch", "0.0.1"),
        ("0.9.9", "minor", "0.10.0"),
        ("9.9.9", "major", "10.0.0"),
    ],
)
def test_bump_version(cur, kind, new):
    assert parse_mod.bump_version(cur, kind) == new


def test_bump_version_unknown_type():
    with pytest.raises(ValueError, match="Unknown bump type"):
        parse_mod.bump_version("1.0.0", "huge")


@pytest.mark.parametrize("bad", ["1.2", "a.b.c", "", "1.2.3.4"])
def test_bump_version_malformed(bad):
    with pytest.raises(ValueError):
        parse_mod.bump_version(bad, "patch")


# ------------------------------------------------------------ parse main()


def test_main_no_args_prints_usage(monkeypatch, capsys):
    code, out, err = run_main(parse_mod, monkeypatch, capsys)
    assert code == 2 and "Usage" in err and out == ""


def test_main_unknown_command(monkeypatch, capsys):
    code, _, err = run_main(parse_mod, monkeypatch, capsys, "frobnicate")
    assert code == 2 and "Usage" in err


@pytest.mark.parametrize("cmd", ["validate", "extract"])
def test_main_wrong_arg_count(monkeypatch, capsys, cmd):
    code, _, err = run_main(parse_mod, monkeypatch, capsys, cmd)
    assert code == 2 and f"usage: parse_pr_changelog.py {cmd}" in err


def test_main_validate_ok(tmp_path, monkeypatch, capsys):
    f = tmp_path / "b.md"
    f.write_text(body(minor=True))
    code, out, err = run_main(parse_mod, monkeypatch, capsys, "validate", str(f))
    assert (code, out, err) == (0, "", "")


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({}, "No version-bump box"),
        ({"major": True, "patch": True}, "More than one"),
        pytest.param(
            {"patch": True, "desc": ""}, "empty",
            marks=pytest.mark.xfail(strict=True, reason="empty description before another section is not rejected"),
        ),
    ],
)
@pytest.mark.parametrize("cmd", ["validate", "extract"])
def test_main_invalid_body(tmp_path, monkeypatch, capsys, cmd, kw, msg):
    f = tmp_path / "b.md"
    f.write_text(body(**kw))
    code, out, err = run_main(parse_mod, monkeypatch, capsys, cmd, str(f))
    assert code == 1 and out == "" and msg in err


@pytest.mark.parametrize("cmd", ["validate", "extract"])
def test_main_empty_description_at_eof(tmp_path, monkeypatch, capsys, cmd):
    f = tmp_path / "b.md"
    f.write_text("- [x] Patch\n\n## Changelog description\n")
    code, out, err = run_main(parse_mod, monkeypatch, capsys, cmd, str(f))
    assert code == 1 and out == "" and "empty" in err


def test_main_extract_ok(tmp_path, monkeypatch, capsys):
    f = tmp_path / "b.md"
    f.write_text(body(major=True, desc="- a\n- b"))
    code, out, err = run_main(parse_mod, monkeypatch, capsys, "extract", str(f))
    assert code == 0 and err == ""
    assert out == "major\n- a\n- b\n"


def test_main_missing_body_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["p", "validate", str(tmp_path / "nope.md")])
    with pytest.raises(FileNotFoundError):
        parse_mod.main()


def test_main_bump_ok(monkeypatch, capsys):
    code, out, _ = run_main(parse_mod, monkeypatch, capsys, "bump", "1.4.2", "minor")
    assert code == 0 and out == "1.5.0\n"


def test_main_bump_wrong_args(monkeypatch, capsys):
    code, _, err = run_main(parse_mod, monkeypatch, capsys, "bump", "1.0.0")
    assert code == 2 and "usage: parse_pr_changelog.py bump" in err


def test_main_bump_invalid_type_raises(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["p", "bump", "1.0.0", "huge"])
    with pytest.raises(ValueError):
        parse_mod.main()


def test_parse_script_via_subprocess(tmp_path):
    f = tmp_path / "b.md"
    f.write_text(body(patch=True))
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "parse_pr_changelog.py"), "extract", str(f)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0 and r.stdout == "patch\n- Did a thing.\n"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "parse_pr_changelog.py"), "bump", "0.1.0", "patch"],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == "0.1.1"


# ------------------------------------------------------------ latest_changelog_entry


CHANGELOG = """# Changelog

Intro paragraph.

## [1.2.0] - 2026-01-02

- Added B
- Added C

## [1.1.0] - 2026-01-01

- Added A
"""


def test_latest_extracts_top_entry(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    notes = tmp_path / "notes.md"
    code, out, _ = run_main_maybe_exit(latest_mod, monkeypatch, capsys, str(cl), str(notes))
    assert code is None
    assert out == "1.2.0\n"
    assert notes.read_text() == "- Added B\n- Added C\n"


def test_latest_single_entry_to_eof(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [0.1.0] - 2025-05-05\n\n- only\n")
    notes = tmp_path / "n.md"
    run_main_maybe_exit(latest_mod, monkeypatch, capsys, str(cl), str(notes))
    assert notes.read_text() == "- only\n"


def test_latest_no_entry(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\nNothing here\n")
    notes = tmp_path / "n.md"
    code, out, err = run_main(latest_mod, monkeypatch, capsys, str(cl), str(notes))
    assert code == 1 and out == "" and "no changelog entry found" in err
    assert not notes.exists()


def test_latest_ignores_malformed_header(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [1.0] - 2026-01-01\n- x\n")
    code, _, _ = run_main(latest_mod, monkeypatch, capsys, str(cl), str(tmp_path / "n"))
    assert code == 1


@pytest.mark.parametrize("args", [[], ["only-one"], ["a", "b", "c"]])
def test_latest_wrong_arg_count(monkeypatch, capsys, args):
    code, _, err = run_main(latest_mod, monkeypatch, capsys, *args)
    assert code == 2 and "usage" in err


def test_latest_subprocess(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    notes = tmp_path / "notes.md"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "latest_changelog_entry.py"), str(cl), str(notes)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0 and r.stdout == "1.2.0\n"
    assert "Added B" in notes.read_text()


# ------------------------------------------------------------ insert_changelog_entry


def test_insert_adds_entry_above_first(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("\n- New thing\n\n")
    code, out, _ = run_main_maybe_exit(insert_mod, monkeypatch, capsys, str(cl), "minor", str(desc), "2026-02-03")
    assert code is None and out == "1.3.0\n"
    text = cl.read_text()
    assert text.startswith("# Changelog\n\nIntro paragraph.\n\n## [1.3.0] - 2026-02-03\n\n- New thing\n\n## [1.2.0]")
    assert text.endswith("- Added A\n")
    assert text.count("## [") == 3


@pytest.mark.parametrize("kind,ver", [("major", "2.0.0"), ("patch", "1.2.1")])
def test_insert_bump_kinds(tmp_path, monkeypatch, capsys, kind, ver):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("- x")
    _, out, _ = run_main_maybe_exit(insert_mod, monkeypatch, capsys, str(cl), kind, str(desc), "2026-02-03")
    assert out.strip() == ver


def test_insert_result_roundtrips_with_latest(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("- Shiny")
    run_main_maybe_exit(insert_mod, monkeypatch, capsys, str(cl), "patch", str(desc), "2026-03-04")
    capsys.readouterr()
    notes = tmp_path / "n.md"
    _, out, _ = run_main_maybe_exit(latest_mod, monkeypatch, capsys, str(cl), str(notes))
    assert out == "1.2.1\n" and notes.read_text() == "- Shiny\n"


def test_insert_no_existing_entry(tmp_path, monkeypatch, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n")
    desc = tmp_path / "d.md"
    desc.write_text("- x")
    code, out, err = run_main(insert_mod, monkeypatch, capsys, str(cl), "patch", str(desc), "2026-01-01")
    assert code == 1 and out == "" and "Could not find" in err
    assert cl.read_text() == "# Changelog\n"


def test_insert_bad_date(tmp_path, monkeypatch):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("- x")
    monkeypatch.setattr(sys, "argv", ["i", str(cl), "patch", str(desc), "03/04/2026"])
    with pytest.raises(ValueError):
        insert_mod.main()
    assert cl.read_text() == CHANGELOG


def test_insert_bad_bump_type_leaves_file(tmp_path, monkeypatch):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("- x")
    monkeypatch.setattr(sys, "argv", ["i", str(cl), "huge", str(desc), "2026-01-01"])
    with pytest.raises(ValueError):
        insert_mod.main()
    assert cl.read_text() == CHANGELOG


@pytest.mark.parametrize("n", [0, 1, 3, 5])
def test_insert_wrong_arg_count(monkeypatch, capsys, n):
    code, _, err = run_main(insert_mod, monkeypatch, capsys, *(["x"] * n))
    assert code == 2 and "Usage" in err


def test_insert_subprocess(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG)
    desc = tmp_path / "d.md"
    desc.write_text("- via subprocess")
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "insert_changelog_entry.py"), str(cl), "minor", str(desc), "2026-05-06"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert r.returncode == 0 and r.stdout == "1.3.0\n"
    assert "## [1.3.0] - 2026-05-06\n\n- via subprocess\n" in cl.read_text()
