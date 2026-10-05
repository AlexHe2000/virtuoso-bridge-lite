# Manual traffic maintenance

Read this when the user requests a traffic update. GitHub retains only 14 days
of traffic data. Aim for at most 10 days between authorized updates; missing
days outside that window cannot be reconstructed.

`scripts/track_traffic.py` merges the rolling API window into
`stats/clones.json` and `stats/views.json`. Use a real authenticated user token
from `gh`: an Actions `GITHUB_TOKEN` cannot access these traffic endpoints.
Keep credentials out of output, commits and documentation.

From the repository, using the project environment:

```bash
GH_TOKEN=$(gh auth token) OWNER=Arcadia-1 REPO=virtuoso-bridge-lite \
    uv run python scripts/track_traffic.py
```

Review the `stats/` diff for accidental deletions or invented gap filling.
Commit/push only when authorized; stage the traffic files explicitly so unrelated
working-tree changes are preserved. Record the actual update date.

There is no automatic traffic polling. Reading this guide or editing other
documentation does not authorize a traffic run or a scheduled task.
