---
name: design
description: Judge whether a module is a god module and produce (or apply) a gated split plan
argument-hint: "<module-path> [--apply]"
---

# Design Check Skill

Judge one module flagged by deterministic hotspot metrics. Deterministic
tools can rank candidates but cannot tell a legitimately large orchestrator
from a god module — that judgment is this skill's job. The output is a
verdict and, for god modules, a concrete split plan. Report-only by
default; `--apply` executes the plan behind strict gates.

## Input

- `<module-path>` - the file to judge
- Metric evidence in the invoking prompt (LOC, defs, fan-in/out, churn,
  score, temporal-coupling partners). If absent, run:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hotspots.py" . --top 20`
  and extract this module's row.

## Step 1: Understand the Module

Read the ENTIRE module — no sampling. Then read a representative sample of
its importers (3-5 files) to see how it is actually used. Identify:

- The distinct responsibilities present (name each in one line)
- Which functions/classes belong to which responsibility
- Shared state or helpers that couple the responsibilities together
- The public API: what importers actually use, vs what is internal

## Step 2: Verdict

**`cohesive`** — one responsibility, or responsibilities so entangled that
splitting adds indirection without clarity. Size alone is NOT a defect. A
529-line module with one clear job is fine. Report the verdict with one
paragraph of reasoning and STOP. This outcome is common and correct —
never invent a split to justify the invocation.

**`god-module`** — two or more separable responsibilities, evidenced by:

- Disjoint groups of functions sharing no helpers or state across groups
- Importers that each use only one slice of the module
- The metric evidence corroborating (high fan-in from unrelated packages,
  churn from unrelated features touching the same file)

## Step 3: Split Plan (god-module only)

Write `design-plan-<module-stem>.md` in the repo root of the worktree:

- **Responsibilities found**, one line each
- **Proposed modules**: name, responsibility, exact list of
  functions/classes that move there
- **Public API preservation**: the original module remains and re-exports
  everything importers use today, so NO importer changes in this pass and
  the external surface is byte-identical
- **Shared internals**: where coupled helpers/state go, and why
- **Risk notes**: import cycles the split could create, test coverage of
  the moved code, anything the judges should scrutinize

Do not edit any source file in report-only mode.

## Step 4: Apply (only with `--apply`)

Gates, in order — a failed gate means STOP, report, do not proceed:

1. **Coverage gate**: measure test coverage of this module. Below 70%,
   first write characterization tests capturing current observed behavior
   (inputs/outputs of the public functions as they ARE, bugs included),
   commit them separately (`test(design): characterize <module>`), and
   only then refactor.
2. **API snapshot**: record the module's public surface before touching it
   (`griffe dump` if available, else `python -c "import m; print(sorted(dir(m)))"`
   or the language equivalent).
3. **Execute the plan exactly**: move code, keep the original module as a
   re-exporting facade. No behavior changes, no opportunistic edits, no
   renames beyond the plan.
4. **Verify**: full test suite passes; API snapshot identical; no new
   import cycles (`import-linter` / `dependency-cruiser` if configured).
5. Commit as `refactor(design): split <module> into <new modules>`,
   staging explicit file paths only.

## Git Safety

Never `git stash`/`reset`/`restore`/`checkout -- <path>`; never push;
stage explicit file paths, never `git add .` or directory adds.

## Report

Verdict, reasoning, plan path (if any), gates passed/failed, commits made
(if any). A `cohesive` verdict with zero changes is a fully successful run.
