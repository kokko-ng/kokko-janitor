---
name: janitor
description: Orchestrate lint fixes and god-module refactoring via subagents and git worktrees. Use when the user asks to clean up, tidy, de-lint, or refactor a codebase, mentions god modules or hotspots, or wants code quality fixed across the whole repo.
argument-hint: "[target-branch] [--langs py,js,dotnet] [--checks security,types,...] [--no-design] [--top N] [--apply-design] [--max-rounds N]"
---

# Janitor Skill

Make a codebase clean and keep it that way. Two layers:

1. **Lint layer** (deterministic): run the quality-check skills in parallel
   worktrees, fix everything they find, merge.
2. **Design layer** (judged): rank modules by god-module risk with
   deterministic metrics, have agents judge the top candidates and propose
   splits, gate any structural change behind tests and an API-surface check.

A metrics scorecard ratchets structural quality so it can only improve
across runs.

## Arguments

Parse `$ARGUMENTS` for:

- `target-branch` - Branch to merge fixes into (default: current branch)
- `--langs` - Languages to check (default: auto-detect all present)
- `--checks` - Lint checks to run (default: all)
- `--no-design` - Skip the design layer entirely
- `--top N` - Design candidates to judge (default: 3). This slices the
  Phase 0 output; the hotspot script itself always runs with `--top 20`
- `--apply-design` - Apply approved design refactors. Without this flag the
  design layer is REPORT-ONLY: it produces split plans, never edits
- `--max-rounds N` - Convergence rounds (default: 1; each extra round
  re-ranks hotspots after merging and continues if candidates remain)

## Lint Checks

Provided by the `kokko-code-quality` plugin (required for the lint layer):
`security`, `types`, `complexity`, `deadcode`, `docs`, `architecture`.
`architecture` supports py and js only.

## Git Safety (non-negotiable, pass to every subagent)

- Dirty working tree at any point where a clean one is required: STOP and
  report. Never `git stash`, `git reset`, `git restore`, or
  `git checkout -- <path>` to clear it — uncommitted tracked work destroyed
  that way is unrecoverable.
- Stage explicit file paths only. Never `git add .` or a directory add
  (directory adds sweep in untracked files).
- Never push. Never rewrite history.

## Workflow

Read `references/worktree-workflow.md` for worktree mechanics, merge
strategy, conflict handling, and error recovery.

### Phase 0: Hotspot Ranking (deterministic)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hotspots.py" . --top 20 \
  --scorecard .janitor/scorecard.json
```

Always run with `--top 20`; the user-facing `--top N` argument (default
3) selects how many of the returned `candidate: true` files Phase 2
judges.

Produces a ranked list of god-module candidates (LOC, defs, fan-in/out,
churn, composite score) plus cross-package temporal-coupling pairs, and
compares structural metrics against the scorecard baseline (exit 2 means
the codebase regressed since the last run — report this prominently).

Keep the JSON output; it feeds Phase 2 prompts and the final report.

### Phase 1: Lint Layer

1. Verify clean working tree; detect languages; create
   `WORKTREE_BASE=$(mktemp -d)`
2. One worktree + branch per language/check pair:
   `git worktree add $WORKTREE_BASE/<lang>-<check> -b janitor/<lang>-<check>`
3. One subagent per worktree, all in parallel. The total MUST equal
   languages x checks — never collapse languages into one subagent. Each
   subagent runs `/<check> <lang>`, fixes ALL issues found, commits in
   small logical `fix(<check>): ...` commits staging explicit file paths.
   A check that finds nothing is a valid result — commit nothing, report
   "clean". Never manufacture work or relax tool configuration to create
   something to fix.

### Phase 2: Design Layer

Skip if `--no-design`, or if Phase 0 found no file with
`candidate: true` (the absolute loc/defs gate — the normalized score
only orders candidates and always puts some file near 1.0).

1. For each of the first N `candidate: true` files (N from `--top`,
   default 3), create a worktree + branch
   (`janitor/design-<module>`) and spawn a subagent running
   `/design <path>` with the candidate's metric evidence pasted into the
   prompt. The design skill produces a verdict (`god-module` or
   `cohesive`) and, for god modules, a concrete split plan written to
   `.janitor/design-plan-<stem>.md` inside the worktree. "Cohesive — no
   action" is a valid and common outcome.
2. **Judge panel**: for each `god-module` verdict, spawn 3 independent
   subagents that each read the module and the proposed plan and vote
   ACCEPT or REJECT ("would this split genuinely improve the design, or is
   it churn?"). Majority rules. Rejected plans are still included in the
   final report, marked rejected.
3. **Collect plans BEFORE any cleanup**: copy every
   `.janitor/design-plan-*.md` from the design worktrees into the main
   checkout's `.janitor/` directory. `git worktree remove --force`
   destroys worktree contents — a plan not copied out first is lost. The
   copies are gitignored untracked artifacts; leave them for the user.
4. Only with `--apply-design`: for each ACCEPTED plan, the design worktree
   subagent applies it under the design skill's apply-mode gates
   (characterization-test coverage, public API preservation, full test
   suite). Without the flag, plans land in the report only.

### Phase 3: Merge and Validate

1. Merge lint branches first (smallest changesets first), then any applied
   design branches, all `--no-ff`, preserving individual commits.
2. Resolve conflicts preserving both intents; stage only the conflicted
   files by explicit path.
3. Final validation on the merged result: run every lint tool that ran in
   Phase 1 plus the full test suite. For applied design changes, also
   verify the public API surface is unchanged (`griffe dump` before/after
   when available). Any regression: fix it before finishing; never merge a
   failing state.
4. Note: test runs may regenerate tracked build artifacts (dbt `target/`,
   bundler output) polluted with ephemeral test values. Never commit
   those; report them and suggest gitignoring the artifact paths.

### Phase 4: Ratchet and Converge

1. Re-run the Phase 0 script with `--update` to tighten
   `.janitor/scorecard.json` to the new (better) values, and commit the
   scorecard change if the file is tracked or the user wants it tracked.
2. If `--max-rounds` allows another round AND Phase 2/3 landed changes AND
   `candidate: true` files remain, loop back to Phase 0. Stop when a round
   lands nothing (converged).

### Cleanup

Remove all worktrees and delete all `janitor/*` branches (skip-merge
branches with zero commits are simply deleted).

## Final Report

- Per-check lint results (including "clean")
- Hotspot top 20 with scores and candidate flags; scorecard delta
  (improved / regressed / held)
- Design verdicts, judge votes, plans (applied or proposed), rejections
- Temporal-coupling pairs worth a human look
- Anything skipped and why

## Success Criteria

- All lint issues fixed, all merges clean, final validation passes
- Design layer: every candidate has a verdict; no structural change merged
  without passing its gates; report-only unless `--apply-design`
- Scorecard never regresses silently
