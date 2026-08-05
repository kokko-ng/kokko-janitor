#!/usr/bin/env bash
# Verify the shared git-safety-core block stays byte-identical across the
# janitor, multipass, and design skills. All three brief subagents with the
# same non-negotiable git rules on purpose; deliberate per-skill deltas
# (push policy, nothing-found rules) live OUTSIDE the markers. Unenforced,
# the copies drift -- one skill banned `git clean`, another did not -- and
# deliberate deltas become indistinguishable from accidents. Mirrors
# check-skill-sync.sh in kokko-cmds, which holds the reference mechanism.
set -euo pipefail

START='<!-- shared:git-safety-core start'
END='<!-- shared:git-safety-core end -->'

FAIL=0
ref=""
ref_file=""
count=0

for f in plugins/kokko-janitor/skills/*/SKILL.md; do
  count=$((count + 1))
  region=$(awk -v s="$START" -v e="$END" \
    'index($0, s)==1 {on=1} on {print} index($0, e)==1 {on=0}' "$f")
  if [ -z "$region" ]; then
    echo "ERROR: $f has no shared:git-safety-core block"
    FAIL=1
    continue
  fi
  if [ -z "$ref" ]; then
    ref="$region"
    ref_file="$f"
    continue
  fi
  if [ "$region" != "$ref" ]; then
    echo "ERROR: shared git-safety-core block in $f differs from $ref_file:"
    diff <(printf '%s\n' "$ref") <(printf '%s\n' "$region") | sed 's/^/  /' | head -20
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "shared git-safety-core block is in sync across $count skills"
fi
exit "$FAIL"
