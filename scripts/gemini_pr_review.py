#!/usr/bin/env python3
"""Calls the Gemini API to review a pull request's diff and upserts the
result as a single PR comment, replacing the previous run's comment rather
than piling up a new one on every push.

Shared by gemini-pr-review.yml, which runs this on pull_request_target
opened/synchronize/reopened. Reads the diff via `gh pr diff`, not from a
checked-out fork ref, and passes it to Gemini as inert text content --
nothing from the PR branch is ever executed.

Usage:
  gemini_pr_review.py <pr_number>

Required env vars:
  GEMINI_API_KEY   Gemini API key
  GEMINI_MODEL     Model name, e.g. gemini-3.8-flash (Google renames/retires
                    models periodically; bump this if the API starts
                    rejecting it rather than editing this script)
  GH_TOKEN         Token for `gh` (provided automatically in Actions)
  GITHUB_REPOSITORY  owner/repo (provided automatically in Actions)

On success, writes two lines to $GITHUB_OUTPUT: verdict=<WORTHY|SKIP> and
summary=<one-line summary>, for the workflow's Telegram step to use.
Exits 1 with a message on stderr on any API or parsing failure -- this is
deliberately not swallowed, so a broken review shows up as a failed
workflow run rather than silently posting nothing.
"""
import json
import os
import subprocess
import sys
import urllib.request

MARKER = "<!-- gemini-pr-review -->"
DIFF_CHAR_LIMIT = 60_000
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["WORTHY", "SKIP"]},
        "summary": {"type": "STRING"},
        "review_markdown": {"type": "STRING"},
    },
    "required": ["verdict", "summary", "review_markdown"],
}

PROMPT_TEMPLATE = """You are an automated code reviewer for a pull request on the
"{repo}" GitHub repository. Decide whether this PR is worth a full human-facing
review, then provide one.

Set verdict to SKIP only for changes with no real review value: formatting-only,
whitespace-only, dependency lockfile bumps with no code changes, generated-file-only,
or an empty/no-op diff. Set verdict to WORTHY for everything else, including small
changes that touch actual logic.

Always fill in summary with one or two plain sentences describing what the PR does,
regardless of verdict. Only fill in review_markdown with substantive review content
(bugs, security issues, edge cases, style/consistency with the rest of the diff) when
verdict is WORTHY; for SKIP, review_markdown may just restate why it was skipped.

PR title: {title}

PR description:
{body}

Diff{truncated_note}:
{diff}
"""


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def fetch_pr(pr_number: str) -> dict:
    raw = run(["gh", "pr", "view", pr_number, "--json", "title,body,url"])
    return json.loads(raw)


def fetch_diff(pr_number: str) -> tuple[str, bool]:
    diff = run(["gh", "pr", "diff", pr_number])
    if len(diff) > DIFF_CHAR_LIMIT:
        return diff[:DIFF_CHAR_LIMIT], True
    return diff, False


def call_gemini(prompt: str) -> dict:
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ["GEMINI_MODEL"]
    url = GEMINI_URL_TMPL.format(model=model)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def upsert_comment(pr_number: str, body: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    comments = json.loads(run(["gh", "api", f"repos/{repo}/issues/{pr_number}/comments"]))
    existing = next((c for c in comments if c["body"].startswith(MARKER)), None)
    if existing:
        subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/comments/{existing['id']}", "-X", "PATCH", "-f", f"body={body}"],
            check=True,
        )
    else:
        subprocess.run(["gh", "pr", "comment", pr_number, "--body", body], check=True)


def main() -> None:
    pr_number = sys.argv[1]
    pr = fetch_pr(pr_number)
    diff, truncated = fetch_diff(pr_number)
    truncated_note = " (truncated, PR is larger than the review budget)" if truncated else ""

    prompt = PROMPT_TEMPLATE.format(
        repo=os.environ["GITHUB_REPOSITORY"],
        title=pr["title"],
        body=pr["body"] or "(no description)",
        diff=diff or "(empty diff)",
        truncated_note=truncated_note,
    )

    try:
        result = call_gemini(prompt)
        verdict = result["verdict"]
        summary = result["summary"]
        review_markdown = result["review_markdown"]
    except Exception as exc:  # noqa: BLE001 -- any failure here must fail loudly, not post garbage
        print(f"Gemini review failed: {exc}", file=sys.stderr)
        sys.exit(1)

    comment_body = f"{MARKER}\n## Gemini PR review\n\n**Verdict:** {verdict}\n\n{summary}\n\n{review_markdown}"
    upsert_comment(pr_number, comment_body)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"verdict={verdict}\n")
            f.write(f"summary={summary}\n")


if __name__ == "__main__":
    main()
