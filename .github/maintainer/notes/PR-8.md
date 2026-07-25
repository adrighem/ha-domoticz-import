# PR:8 - actions/setup-python v7

## Snapshot

- Title: `chore(deps): bump actions/setup-python from 6 to 7 in the github-actions group`
- Author: Dependabot
- State: Closed
- Scope: `.github/workflows/ci.yml`, `.github/workflows/pip-audit.yml`

## Intent

Keep GitHub workflow dependencies current by updating `actions/setup-python` from `v6`
to `v7`.

## Assessment

- Low-risk maintenance update: only two workflow setup-python steps change.
- All PR checks passed on GitHub.
- Local equivalent patch prepared instead of merging the bot branch, matching the
  standing rule for Dependabot PRs.

## Verification

- `python -m pytest` - 16 passed
- `uvx ruff check .` - passed

## Outcome

- Applied the equivalent update directly on `main` in `9aee2bee8ef5a07b44927c30d38f968f192306b9`.
- Closed `PR:8` with a short public note explaining that the update landed on `main`.
