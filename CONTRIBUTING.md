# Contributing

## Local checks

```bash
python3 -m pytest tests/ -q        # test suite (real throwaway git repos, no mocks)
pre-commit install                 # once; then hooks run on every commit
pre-commit run --all-files         # ruff + ruff-format, markdownlint, shellcheck, hygiene
bash scripts/check-marketplace-sync.sh
bash scripts/check-git-rules-sync.sh   # shared git-safety block across the three skills
```

Dev dependencies are pinned in `requirements-dev.txt`
(`pip install -r requirements-dev.txt`). Ruff is configured in
`pyproject.toml` and runs via its pre-commit hook — there is no separate
CI job for it.

`plugins/kokko-janitor/scripts/hotspots.py` is stdlib-only by design
(the README promises it); do not add runtime dependencies. New or
changed script behavior gets a test in `tests/test_hotspots.py`,
following the house style there: real git repos via the helpers, the
script run as a subprocess, JSON parsed, no mocks.

## Release flow (CI-gated)

1. Bump the version in BOTH `.claude-plugin/marketplace.json` and
   `plugins/kokko-janitor/.claude-plugin/plugin.json` (they must agree —
   `scripts/check-marketplace-sync.sh` enforces it), then merge to `main`.
2. CI runs on `main` (pre-commit, actionlint, pytest, plugin validation,
   sync check).
3. When CI succeeds, `.github/workflows/release.yml` fires via
   `workflow_run` and creates the `v<version>` GitHub release. It is the
   sole publisher; never run `gh release create` by hand.
   `workflow_dispatch` with an explicit tag exists for recovery.

## Shared infrastructure

The shared infra here (release workflow, marketplace sync script,
pre-commit config, gitignore) follows
[kokko-cmds](https://github.com/kokko-ng/kokko-cmds), which holds the
reference copies. When changing any of it, keep the two repos convergent.
