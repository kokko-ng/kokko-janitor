# kokko-janitor

A Claude Code plugin that makes a codebase clean and keeps it that way.

Two layers, because deterministic tools and LLM judgment are good at
different things:

1. **Lint layer** (deterministic). Runs quality checks — security, types,
   complexity, dead code, docs, architecture — in parallel git worktrees,
   one subagent per check, fixes everything found, and merges the results
   as reviewable `--no-ff` merges of small logical commits.
2. **Design layer** (deterministic finds, LLM judges, tests gate). Linters
   cannot see a god module: a 2,000-line file of individually clean
   functions passes every check. This layer ranks modules by god-module
   risk using objective signals (LOC, definition count, import fan-in/out,
   git churn, cross-package temporal coupling), then has agents judge only
   the top candidates. A 3-agent judge panel votes on every proposed
   split; structural changes are report-only by default and, when applied,
   are gated behind characterization-test coverage, a public-API snapshot,
   and the full test suite.

A committed scorecard (`.janitor/scorecard.json`) ratchets structural
metrics — max file size, max fan-in, max definitions per file — so the
codebase can only get better between runs, and regressions are loud.

## Install

```
/plugin marketplace add kokko-ng/kokko-janitor
/plugin install kokko-janitor@kokko-ng-kokko-janitor
```

The lint layer invokes the check skills from
[kokko-cmds](https://github.com/kokko-ng/kokko-cmds)' `kokko-code-quality`
plugin — install that too, or run with the design layer only.

## Usage

```
/kokko-janitor:janitor                      # lint fixes + design report
/kokko-janitor:multipass 3 /kokko-janitor:janitor   # 3 fresh-context passes, convergence report
/kokko-janitor:multipass 2 prompts/deployed-validation.md then 2 prompts/aesthetics.md
/kokko-janitor:janitor --apply-design       # also apply approved splits
/kokko-janitor:janitor --no-design          # lint layer only
/kokko-janitor:janitor --top 5 --max-rounds 3   # deeper, iterative
/kokko-janitor:design src/big_module.py     # judge one module directly
```

The hotspot ranker is a plain script and useful on its own:

```
python3 plugins/kokko-janitor/scripts/hotspots.py . --top 10
python3 plugins/kokko-janitor/scripts/hotspots.py . --scorecard .janitor/scorecard.json --update
```

Stdlib-only Python; needs `git` on PATH. Exit 2 means a scorecard
regression.

## Multipass

`multipass` runs repeated independent passes of any skill or prompt file
(sequentially, each in a fresh subagent) and reports convergence: a first
pass that finds much, a second that finds little, and a third that finds
nothing is evidence the codebase is actually clean — the same finding
surviving multiple passes unfixed is evidence of a blocker. Chain targets
with `then` to run ordered sets, and `--pre '...'` for a one-time setup
step (e.g. deploy before validating). Pairs with the `tailor` skill from
kokko-cmds' kokko-validation plugin, which writes repo-specific prompt
files to `prompts/`.

## Design principles

- **Deterministic finds, LLM judges, tests gate.** Metrics rank
  candidates; agents decide god-module vs cohesive; nothing structural
  merges without passing its gates. "Cohesive — no action" is an expected
  verdict, not a failure.
- **Report-first.** A bad lint fix costs nothing; a bad module split costs
  a day. Design changes need `--apply-design` plus a majority of judges.
- **Never manufacture work.** Clean checks report clean. Tool
  configurations are never relaxed to create findings.
- **Git safety.** No stash/reset/restore against dirty trees, no history
  rewrites, no pushes, explicit-file-path staging only. Dirty tree means
  stop and report, not tidy and proceed.

## License

MIT
