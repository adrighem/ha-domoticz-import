# Workflow

## Implementation Loop

1. Select the next unchecked task from the active track.
2. Add or update tests that describe the intended behavior.
3. Confirm new tests fail for the expected reason when practical.
4. Implement the smallest coherent change.
5. Refactor only where it improves clarity without expanding scope.
6. Run focused tests with a minimal allowlisted environment.
7. Run the relevant compatibility, lint, and integration suites.
8. Update the track plan and documentation.
9. Commit with a Conventional Commit message and push the branch.
10. Monitor pull-request checks and fix root causes until green.

## Environment Safety

Commands that may capture inherited environment variables must run with
`env -i` and only the required variables. Credentials, tokens, cookies,
authorization headers, and credential-bearing URLs must never appear in logs,
test output, artifacts, comments, or committed files.

## Phase Completion

Each phase ends with a manual verification task. Automated checks may be
completed first, but the phase is not marked complete until the user confirms
the documented live behavior. Summarize the completed phase and the next action
at that point.

## Git

- Preserve unrelated user changes.
- Use one coherent commit per independently reviewable change where practical.
- Use `Refs #43` style commit-body footers only when work is tied to an issue.
- Do not merge the release pull request until the planned release scope is
  complete.
