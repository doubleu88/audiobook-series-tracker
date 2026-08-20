# Contributing

## Pull requests

Every PR uses the template that's pre-filled when you open one. Two parts of
it are required and checked automatically (a required status check will fail
and block merging if either is missing):

1. **Version bump** — check exactly one of Major / Minor / Patch, following
   [Semantic Versioning](https://semver.org/): Major is a breaking change,
   Minor is a new backward-compatible feature, Patch is a fix with no new
   capability.
2. **Changelog description** — write it the way you'd want a reader to see
   it in the release notes, not as implementation detail. One bullet per
   notable thing if there's more than one.

On merge, that bump type and description are used to automatically insert
the CHANGELOG.md entry and bump the version — you don't need to (and
shouldn't) edit CHANGELOG.md yourself in the PR. That commit then triggers
tagging and publishing the GitHub Release automatically.

If you're not sure which bump type applies, ask in the PR description —
that's a completely normal thing to be unsure about, and easy to fix by
editing the PR body (the check re-runs on every edit).

## Release automation internals

Every merge triggers `changelog-on-merge.yml`, which commits the new
CHANGELOG.md entry and then explicitly dispatches `release.yml` (rather
than relying on that push to trigger it, since GitHub doesn't chain
workflow triggers off of `GITHUB_TOKEN`-authored pushes).

<!-- validates automation still works with branch protection enabled -->

<!-- validates the code-owner-review ruleset does not break the changelog automation -->
