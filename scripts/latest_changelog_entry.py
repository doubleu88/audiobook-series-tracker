#!/usr/bin/env python3
"""Extracts the topmost "## [X.Y.Z] - YYYY-MM-DD" section from CHANGELOG.md.
Used by .github/workflows/release.yml to auto-tag and auto-release whenever
that entry's version doesn't have a matching git tag yet — this is a plain
script (not inlined in the workflow YAML) so it's testable locally and
readable without wrestling with shell quoting.

Prints the version (no "v" prefix) on stdout, and writes the entry's body
(everything below the header, up to the next "## [" section or end of file)
to the path given as the second argument, for use as release notes.
"""
import re
import sys

ENTRY_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}\s*\n(.*?)(?=\n## \[|\Z)", re.S | re.M)


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: latest_changelog_entry.py CHANGELOG.md notes_output_path", file=sys.stderr)
        sys.exit(2)

    changelog_path, notes_path = sys.argv[1], sys.argv[2]
    content = open(changelog_path).read()

    match = ENTRY_RE.search(content)
    if not match:
        print("no changelog entry found (expected a '## [X.Y.Z] - YYYY-MM-DD' header)", file=sys.stderr)
        sys.exit(1)

    version, body = match.groups()
    with open(notes_path, "w") as f:
        f.write(body.strip() + "\n")

    print(version)


if __name__ == "__main__":
    main()
