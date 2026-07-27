# Changelog

## 1.2.0 - unreleased

Fixed:

- `hotspots.py` did not exclude a root-level `dist/`, `build/`, `vendor/` or
  `migrations/` directory. The default excludes are written as `*/dist/*`, and
  `git ls-files` emits root-level paths as `dist/bundle.js` with no leading
  directory for the pattern to match against — so committed build output was
  ranked as if it were source, and could crowd real modules out of the top-N
  the design layer judges.

Added:

- `tests/` — 28 pytest cases covering the ranker: LOC and definition counts,
  fan-in/fan-out including relative-import resolution, temporal coupling,
  exclusion handling, the CLI contract, and the scorecard ratchet. There were no
  tests before, and the ratchet is the piece with a committed artefact and a
  non-zero exit code hanging off it: one that fails open lets structure rot
  while reporting a clean run.
- CI. This repo had none. It now runs the same reusable workflow as
  `kokko-cmds`: pre-commit, `claude plugin validate`, marketplace/manifest sync,
  command and skill frontmatter validation, ruff and pytest.
- `.pre-commit-config.yaml`, `.markdownlint.yaml` and `pyproject.toml` (a config
  root for ruff and pytest).
- `scripts/check-marketplace-sync.sh` — asserts `marketplace.json` and
  `plugin.json` agree on version and description. Previously only `kokko-cmds`
  had this, so this repo could publish a marketplace entry pointing at a
  manifest that disagreed with it.
- `scripts/bump.sh` and `scripts/check-version-bumped.sh`.
- `.github/workflows/release.yml` — this repo had no release workflow and no
  tags, so `1.1.0` was never published as a GitHub release.

## 1.1.0

- Added the `multipass` skill for repeated fresh-context passes.

## 1.0.0

- Initial release: janitor plugin with a hotspot-ranked, agent-judged design
  layer.
