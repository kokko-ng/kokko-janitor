#!/usr/bin/env python3
"""Rank modules by god-module risk using deterministic signals.

Signals per file:
  loc      non-blank lines of code
  defs     top-level and nested function/class definitions (Python only)
  fan_in   number of project files importing this module (Python only)
  fan_out  number of project modules this file imports (Python only)
  churn    commits touching this file (git log)

The composite score is the mean of each file's available signals after
normalising every signal to [0, 1] against the repo-wide maximum. Python
files get all five signals; other languages get loc + churn, which still
catches the huge file everyone edits every sprint.

Also reports temporal coupling: pairs of high-churn files that repeatedly
change in the same commit but live in different directories.

Scorecard ratchet: --scorecard PATH compares absolute structural metrics
(max loc, max fan-in, max defs per file) against a stored baseline, exits 2
on regression, and with --update tightens the baseline to current values.
Normalised scores are relative to a single run and are never ratcheted.

Stdlib only. Requires python3 and git on PATH.
"""

import argparse
import ast
import fnmatch
import json
import os
import subprocess
import sys
from collections import defaultdict
from itertools import combinations

SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".rb", ".java"}
DEFAULT_EXCLUDES = [
    "*test*", "*conftest*", "*/migrations/*", "*/target/*", "*/dist/*",
    "*/build/*", "*/node_modules/*", "*/.venv/*", "*/vendor/*", "*.min.js",
]


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def excluded(path, excludes):
    """True if a repo-relative path matches any exclude glob.

    Matched against a leading-slash form as well as the bare path. `git
    ls-files` emits `dist/bundle.js` with no leading directory, and the
    `*/dist/*` style pattern cannot match that -- fnmatch's `*` will happily
    match an empty string, but only where there is something to match against.
    Without this a committed root-level dist/, build/ or vendor/ was ranked as
    if it were source.
    """
    return any(
        fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, pat)
        for pat in excludes
    )


def tracked_source_files(root, excludes):
    out = []
    for path in sh(["git", "ls-files"], root).splitlines():
        if os.path.splitext(path)[1] not in SOURCE_EXTS:
            continue
        if excluded(path, excludes):
            continue
        out.append(path)
    return out


def count_loc(root, path):
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def py_structure(root, path):
    """Return (defs, imported module names) for a Python file.

    Relative imports are resolved against the file's own package, so
    `from ..db import x` in pkg/api/tasks.py yields `pkg.db`.
    """
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return 0, set()
    defs = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               for n in ast.walk(tree))
    # Package of this file: a/b/c.py lives in package a.b; a/b/__init__.py IS a.b.
    pkg_parts = os.path.dirname(path).split("/") if os.path.dirname(path) else []
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0:
                if n.module:
                    imports.add(n.module)
            else:
                base = pkg_parts[: len(pkg_parts) - (n.level - 1)]
                if len(base) == len(pkg_parts) - (n.level - 1):
                    prefix = ".".join(base)
                    if n.module:
                        prefix = f"{prefix}.{n.module}" if prefix else n.module
                    if prefix:
                        imports.add(prefix)
                    # `from . import db` — the imported NAMES are the modules.
                    if not n.module:
                        for a in n.names:
                            imports.add(f"{prefix}.{a.name}" if prefix else a.name)
    return defs, imports


def module_name(path):
    """Dotted module name for a repo-relative Python path."""
    mod = path[:-3] if path.endswith(".py") else path
    mod = mod.replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def churn_and_coupling(root, files, since):
    """Commits-touching-file counts, plus co-change pairs for high-churn files."""
    args = ["git", "log", "--format=%H", "--name-only"]
    if since:
        args.append(f"--since={since}")
    fileset = set(files)
    churn = defaultdict(int)
    commits = []
    current = set()
    for line in sh(args, root).splitlines():
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current:
                commits.append(current)
            current = set()
        elif line and line in fileset:
            churn[line] += 1
            current.add(line)
    if current:
        commits.append(current)

    top = set(sorted(churn, key=churn.get, reverse=True)[:40])
    pairs = defaultdict(int)
    for commit_files in commits:
        for a, b in combinations(sorted(commit_files & top), 2):
            # Same-directory co-change is expected; cross-package coupling is the smell.
            if os.path.dirname(a) != os.path.dirname(b):
                pairs[(a, b)] += 1
    coupling = [
        {"files": [a, b], "co_changes": n,
         "strength": round(n / min(churn[a], churn[b]), 2)}
        for (a, b), n in pairs.items()
        if n >= 3 and n / min(churn[a], churn[b]) >= 0.5
    ]
    coupling.sort(key=lambda c: (-c["strength"], -c["co_changes"]))
    return churn, coupling[:20]


def analyse(root, excludes, since):
    files = tracked_source_files(root, excludes)
    if not files:
        return {"files": [], "coupling": []}

    rows = {p: {"path": p, "loc": count_loc(root, p)} for p in files}

    imports_of = {}
    for p in files:
        if p.endswith(".py"):
            defs, imports = py_structure(root, p)
            rows[p]["defs"] = defs
            imports_of[p] = imports

    # Fan-in/out resolved against project module names only.
    mod_to_path = {module_name(p): p for p in imports_of}
    fan_in = defaultdict(int)
    for p, imports in imports_of.items():
        internal = set()
        for imp in imports:
            # `from pkg.mod import x` may reference pkg.mod or pkg.mod.x's parent.
            for candidate in (imp, imp.rsplit(".", 1)[0]):
                target = mod_to_path.get(candidate)
                if target and target != p:
                    internal.add(target)
                    break
        rows[p]["fan_out"] = len(internal)
        for target in internal:
            fan_in[target] += 1
    for p in imports_of:
        rows[p]["fan_in"] = fan_in[p]

    churn, coupling = churn_and_coupling(root, files, since)
    for p in files:
        rows[p]["churn"] = churn.get(p, 0)

    signals = ("loc", "defs", "fan_in", "fan_out", "churn")
    peaks = {s: max((r.get(s, 0) for r in rows.values()), default=0) for s in signals}
    for r in rows.values():
        present = [s for s in signals if s in r and peaks[s] > 0]
        r["score"] = round(sum(r[s] / peaks[s] for s in present) / max(len(present), 1), 3)

    ranked = sorted(rows.values(), key=lambda r: -r["score"])
    return {"files": ranked, "coupling": coupling}


def ratchet(report, path, update):
    """Compare absolute structural metrics against the stored baseline."""
    pyfiles = [r for r in report["files"] if "defs" in r] or report["files"]
    current = {
        "max_file_loc": max((r["loc"] for r in report["files"]), default=0),
        "max_fan_in": max((r.get("fan_in", 0) for r in pyfiles), default=0),
        "max_defs_per_file": max((r.get("defs", 0) for r in pyfiles), default=0),
    }
    baseline = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            baseline = json.load(f)

    regressions = {k: (baseline[k], v) for k, v in current.items()
                   if k in baseline and v > baseline[k]}
    if update and not regressions:
        tightened = {k: min(v, baseline.get(k, v)) for k, v in current.items()}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tightened, f, indent=2)
            f.write("\n")
    return current, baseline, regressions


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".", help="repo root (default: cwd)")
    ap.add_argument("--top", type=int, default=10, help="files to report (default 10)")
    ap.add_argument("--since", help="limit churn window, e.g. '18 months ago'")
    ap.add_argument("--exclude", action="append", default=[],
                    help="extra exclude glob (repeatable)")
    ap.add_argument("--scorecard", help="path to scorecard baseline JSON")
    ap.add_argument("--update", action="store_true",
                    help="tighten the scorecard to current values (no-op on regression)")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    try:
        sh(["git", "rev-parse", "--git-dir"], root)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: git cannot read {root}: {e.stderr.strip()}")

    report = analyse(root, DEFAULT_EXCLUDES + args.exclude, args.since)
    out = {"root": root, "top": report["files"][: args.top],
           "coupling": report["coupling"], "files_analysed": len(report["files"])}

    exit_code = 0
    if args.scorecard:
        current, baseline, regressions = ratchet(report, args.scorecard, args.update)
        out["scorecard"] = {"current": current, "baseline": baseline,
                            "regressions": {k: {"baseline": b, "current": c}
                                            for k, (b, c) in regressions.items()}}
        if regressions:
            exit_code = 2

    json.dump(out, sys.stdout, indent=2)
    print()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
