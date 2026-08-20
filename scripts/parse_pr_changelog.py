#!/usr/bin/env python3
"""Parses the PR-template-shaped body of a pull request to extract a semver
bump type (major/minor/patch) and a CHANGELOG.md description.

Shared by two workflows so the parsing rules only exist in one place:
- pr-checklist.yml (validates the body, fails the PR check if malformed)
- changelog-on-merge.yml (extracts the values to build the changelog entry)

Usage:
  parse_pr_changelog.py validate <body_file>
      Exits 0 if the body has exactly one checked bump-type box and a
      non-empty description; exits 1 with a human-readable reason on stderr
      otherwise. Prints nothing on success.

  parse_pr_changelog.py extract <body_file>
      On a valid body, prints two lines to stdout: the bump type, then the
      description (description may itself be multi-line — it's everything
      after the first line). Exits 1 (same validation) if invalid.

  parse_pr_changelog.py bump <current_version> <bump_type>
      Prints the next semver version (no "v" prefix) for the given bump type.
"""
import re
import sys

BUMP_TYPES = ("major", "minor", "patch")
CHECKBOX_RE = re.compile(r"^- \[( |x|X)\] (Major|Minor|Patch)", re.M)
DESCRIPTION_HEADING_RE = re.compile(
    r"## Changelog description\s*\n(?:<!--.*?-->\s*\n)?(.*?)(?=\n## |\Z)", re.S
)


def parse(body: str) -> tuple[str, str]:
    """Returns (bump_type, description). Raises ValueError with a clear
    message if the body doesn't have exactly one checked box or a non-empty
    description."""
    checked = [m.group(2).lower() for m in CHECKBOX_RE.finditer(body) if m.group(1).strip().lower() == "x"]
    if len(checked) == 0:
        raise ValueError(
            "No version-bump box is checked. Check exactly one of Major/Minor/Patch "
            "in the PR description."
        )
    if len(checked) > 1:
        raise ValueError(
            f"More than one version-bump box is checked ({', '.join(checked)}). "
            "Check exactly one."
        )
    bump_type = checked[0]

    match = DESCRIPTION_HEADING_RE.search(body)
    description = match.group(1).strip() if match else ""
    if not description:
        raise ValueError(
            'The "## Changelog description" section is empty. Write what this '
            "change does — that text becomes the CHANGELOG.md entry."
        )

    # Match the project's existing changelog style: a bullet list. If the
    # author didn't already format it that way, wrap each non-empty line in
    # "- " rather than rejecting otherwise-good descriptions over formatting.
    lines = description.splitlines()
    if not any(line.strip().startswith("-") for line in lines):
        description = "\n".join(f"- {line.strip()}" for line in lines if line.strip())

    return bump_type, description


def bump_version(current: str, bump_type: str) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    if bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown bump type: {bump_type!r}")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    command = sys.argv[1]

    if command in ("validate", "extract"):
        if len(sys.argv) != 3:
            print(f"usage: parse_pr_changelog.py {command} <body_file>", file=sys.stderr)
            sys.exit(2)
        body = open(sys.argv[2]).read()
        try:
            bump_type, description = parse(body)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        if command == "extract":
            print(bump_type)
            print(description)
        sys.exit(0)

    if command == "bump":
        if len(sys.argv) != 4:
            print("usage: parse_pr_changelog.py bump <current_version> <bump_type>", file=sys.stderr)
            sys.exit(2)
        print(bump_version(sys.argv[2], sys.argv[3]))
        sys.exit(0)

    print(__doc__, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
