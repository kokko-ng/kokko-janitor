---
name: multipass
description: Run repeated fresh-context passes of a skill or prompt file (or an ordered set), sequentially, and report convergence
argument-hint: "<N> <skill-or-prompt> [then <N> <skill-or-prompt> ...] [--pre '<step>']"
---

# Multipass Skill

Run a set of prompts together: N independent passes of each target, fully
sequentially, each pass in a fresh subagent with no context from previous
passes. Independent repetition is a verification tool — a finding that
appears in two independent passes is probably real; a pass that finds
nothing after a pass that fixed everything is evidence of convergence.

## Arguments

Parse `$ARGUMENTS` as an ordered plan:

- `<N> <target>` — N passes of a target. A target is either a slash
  command/skill (`/kokko-janitor:janitor`, `/security py`) or a prompt
  file path (`prompts/deployed-validation.md`)
- `then` chains further targets: `2 prompts/a.md then 2 prompts/b.md`
  runs a1, a2, b1, b2 — strictly in that order
- `--pre '<step>'` — a step to perform ONCE before the first pass (e.g.
  "deploy the code exactly as it currently exists, with no changes")

If the plan is ambiguous, restate your parsed plan and confirm before
running.

## Execution

Run `--pre` first if given. Then, for each pass in order:

1. Spawn ONE new subagent with no memory of previous passes. Brief it
   with: the target to run (skill invocation or "follow all directions in
   <file>"), permission to spawn its own subagents as the target
   requires, and the ground rules below.
2. Wait for the pass to finish completely before starting the next.
   Never run passes in parallel — later passes must see the repo state
   earlier passes produced.
3. After each pass, verify repo hygiene before continuing: tracked tree
   state, no leftover worktrees or temporary branches. Fix or report
   before the next pass.

### Ground rules to pass to every subagent, verbatim

- Never `git stash`, `git clean`, `git reset --hard`, `git rebase`,
  `git restore`, or `git checkout <ref> -- <path>`. A dirty tree that is
  not yours: stop and report.
- Never push unless the target's own instructions explicitly require it.
- Stage explicit individual file paths only — never `git add .` and never
  a directory add.
- Finding NOTHING is a valid, reportable result. Do not manufacture work,
  do not relax tool or test configuration to create findings.

### Context between passes

Passes are independent, but not blind: brief each pass with the few
GROUND FACTS later passes need to avoid re-deriving or contradicting
reality (e.g. "the tree is clean", "generated artifacts under X are
expected and must be left alone", "a previous pass already fixed the lint
baseline — expect little"). Never leak a previous pass's *findings or
conclusions* — only environmental facts. That preserves independence
while preventing wasted passes.

## Final Report

- Per pass: what ran, what it found, what changed (commits/branches)
- **Convergence analysis**: did successive passes of the same target find
  less? A first pass with many findings, a second with few, and a third
  with none is the ideal signature. Identical findings across passes that
  nobody fixed indicate a blocked fix, not noise — flag them.
- Discrepancies: anything one pass claimed that another contradicted
- Repo state at the end: branch, HEAD, cleanliness
