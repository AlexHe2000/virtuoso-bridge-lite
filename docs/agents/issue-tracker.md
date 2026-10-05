# Issue tracker: GitHub

Issues and specs live in `Arcadia-1/virtuoso-bridge-lite`. Use `gh` for GitHub
operations, with `--repo Arcadia-1/virtuoso-bridge-lite` when outside this clone.

- Read: `gh issue view NUMBER --comments`; fetch labels with `--json`.
- Discover: `gh issue list --state open --json number,title,labels`;
  read the body/comments of selected issues afterward.
- Publish: `gh issue create --title TITLE --body-file FILE` or
  `gh issue comment NUMBER --body-file FILE`.
- Change labels: `gh issue edit NUMBER --add-label LABEL --remove-label LABEL`.
- Close after verifying the outcome: `gh issue close NUMBER --comment MESSAGE`.

## Pull requests as a triage surface

When the user requests review of open issues and PRs, discover both surfaces:
`gh issue list --state open` and `gh pr list --state open`. A skill running only
automatic issue triage discovers issues, not PRs. For a selected PR use
`gh pr view NUMBER`, `gh pr diff NUMBER`, `gh pr checks NUMBER`, and
`gh pr review NUMBER` within the authorized scope.
GitHub shares the issue/PR number space; resolve an ambiguous number with
`gh pr view`, falling back to `gh issue view`.

When a skill says to publish a ticket, create a GitHub issue. When it says to
fetch a ticket, read its body, comments, labels, and current state.

## Issue disposition when publishing a PR

Decide whether each related issue is fully resolved or remains open for deferred
work. Use `Related to #NUMBER` for partial work. Reserve GitHub closing keywords
for an issue whose acceptance criteria are all met: GitHub can recognize them
even inside a negated sentence. Inspect linked issues before merging and verify
their state afterward. This check does not authorize a merge on its own.
