#!/usr/bin/env python3
"""Inserts a new "## [X.Y.Z] - YYYY-MM-DD" entry at the top of CHANGELOG.md
(right after the file's intro paragraph / before the first existing entry),
computing X.Y.Z by bumping the current topmost entry's version according to
bump_type. Used by changelog-on-merge.yml.

Usage: insert_changelog_entry.py CHANGELOG.md <bump_type> <description_file> <date YYYY-MM-DD>

Prints the new version (no "v" prefix) to stdout.
"""
import datetime
import re
import sys

from parse_pr_changelog import bump_version

TOP_ENTRY_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", re.M)


def main() -> None:
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    changelog_path, bump_type, description_path, date_str = sys.argv[1:5]
    datetime.date.fromisoformat(date_str)  # validates format, raises if malformed

    content = open(changelog_path).read()
    match = TOP_ENTRY_RE.search(content)
    if not match:
        print("Could not find an existing '## [X.Y.Z] - YYYY-MM-DD' entry to bump from.", file=sys.stderr)
        sys.exit(1)

    current_version = match.group(1)
    new_version = bump_version(current_version, bump_type)
    description = open(description_path).read().strip()

    new_entry = f"## [{new_version}] - {date_str}\n\n{description}\n\n"
    updated = content[: match.start()] + new_entry + content[match.start() :]

    with open(changelog_path, "w") as f:
        f.write(updated)

    print(new_version)


if __name__ == "__main__":
    main()
