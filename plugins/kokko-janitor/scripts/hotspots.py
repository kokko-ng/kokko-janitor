#!/usr/bin/env python3
"""Rank modules by god-module risk using deterministic signals.

Signals per file:
  loc      non-blank lines of code
  defs     top-level and nested function/class definitions (Python only)
  fan_in   number of project files importing this module (Python only)
  fan_out  number of project modules this file imports (Python only)
  churn    commits touching this file (git log, rename-aware)

The composite score is computed over a FIXED five-signal vector: every
signal is normalised to [0, 1] against the repo-wide maximum, missing
signals count as 0, and the mean is taken over all five. That keeps ranks
comparable across languages (a two-signal .ts file cannot out-score a
five-signal .py file by dilution). The score orders candidates only; the
absolute `candidate` flag (loc >= --candidate-loc or defs >=
--candidate-defs) is the gate for design review.

Also reports temporal coupling: pairs of high-churn files that repeatedly
change in the same commit but live in different directories.

Scorecard ratchet: --scorecard PATH (relative paths resolve against the
target repo root) compares absolute structural metrics (max loc, max
fan-in, max defs per file) against a stored baseline, exits 2 on
regression, and with --update tightens the baseline to current values.
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
import tempfile
from collections import defaultdict
from itertools import combinations

SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".go", ".rb", ".java"}
# Path-anchored test patterns: never bare "*test*", which drops
# latest_prices.py, contest.py, attestation.py and friends.
DEFAULT_EXCLUDES = [
    "tests/*",
    "*/tests/*",
    "test/*",
    "*/test/*",
    "test_*.py",
    "*/test_*.py",
    "*_test.*",
    "*.test.*",
    "*.spec.*",
    "*.stories.*",
    "__tests__/*",
    "*/__tests__/*",
    "spec/*",
    "*/spec/*",
    "conftest.py",
    "*/conftest.py",
    "*/migrations/*",
    "*/target/*",
    "*/dist/*",
    "*/build/*",
    "*/node_modules/*",
    "*/.venv/*",
    "*/vendor/*",
    "*.min.js",
]
# Common non-package source roots stripped when indexing module names, so
# src/pkg/mod.py also resolves imports written as `import pkg.mod`.
STRIP_ROOTS = ("src", "lib", "app")
SIGNALS = ("loc", "defs", "fan_in", "fan_out", "churn")
SCORECARD_KEYS = ("max_file_loc", "max_fan_in", "max_defs_per_file")


def sh(args: list[str], cwd: str) -> str:
    """Run a command and return its stdout, raising on non-zero exit."""
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def tracked_source_files(root: str, excludes: list[str]) -> list[str]:
    """List tracked files with a source extension, minus excluded globs."""
    out = []
    # Explicit "\n" split, not splitlines(): core.quotepath=off makes git
    # emit raw filename bytes, and splitlines() also breaks on \x0b, \x0c,
    # \x85 etc., which would bisect a filename containing them.
    for path in sh(["git", "-c", "core.quotepath=off", "ls-files"], root).split("\n"):
        if not path:
            continue
        if os.path.splitext(path)[1] not in SOURCE_EXTS:
            continue
        if any(fnmatch.fnmatch(path, pat) for pat in excludes):
            continue
        out.append(path)
    return out


def count_loc(root: str, path: str) -> int:
    """Count non-blank lines; unreadable files count 0 rather than aborting."""
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def py_structure(root: str, path: str) -> tuple[int, set[str]]:
    """Return (defs, imported module names) for a Python file.

    Relative imports are resolved against the file's own package, so
    `from ..db import x` in pkg/api/tasks.py yields `pkg.db`.
    """
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError) as e:
        print(f"warning: cannot parse {path}: {e}", file=sys.stderr)
        return 0, set()
    defs = sum(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in ast.walk(tree)
    )
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


def module_name(path: str) -> str:
    """Dotted module name for a repo-relative Python path."""
    mod = path.removesuffix(".py")
    mod = mod.replace("/", ".")
    return mod.removesuffix(".__init__")


def module_keys(path: str) -> list[str]:
    """All dotted keys a path should be indexed under.

    src/pkg/mod.py is importable as pkg.mod, so index it under both
    src.pkg.mod and pkg.mod (same for lib/ and app/ roots).
    """
    keys = [module_name(path)]
    parts = path.split("/")
    if len(parts) > 1 and parts[0] in STRIP_ROOTS:
        keys.append(module_name("/".join(parts[1:])))
    return keys


def churn_and_coupling(
    root: str,
    files: list[str],
    since: str | None,
    churn_top: int,
    min_shared: int,
    min_strength: float,
    top_pairs: int,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    """Commits-touching-file counts, plus co-change pairs for high-churn files.

    Commit boundaries use an explicit NUL sentinel (never "line looks like a
    hash", which breaks on SHA-256 repos and hex-named files). Renames are
    detected (-M) and folded onto the present-day path so moved files keep
    their churn.
    """
    args = [
        "git",
        "-c",
        "core.quotepath=off",
        "log",
        "--format=%x00%H",
        "--name-status",
        "-M",
    ]
    if since:
        args.append(f"--since={since}")
    fileset = set(files)
    churn = defaultdict(int)
    commits = []
    current = set()
    renamed_to = {}  # historical path -> present-day path

    def resolve(path: str) -> str:
        return renamed_to.get(path, path)

    # git log walks newest to oldest, so a rename old->new seen now means
    # older commits touching `old` belong to new's present-day path.
    # Explicit "\n" split, not splitlines(): core.quotepath=off makes git
    # emit raw filename bytes, and splitlines() also breaks on \x0b, \x0c,
    # \x85 etc., which would bisect a filename containing them.
    for line in sh(args, root).split("\n"):
        if line.startswith("\x00"):
            if current:
                commits.append(current)
            current = set()
            continue
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        # Copy statuses (C...) never appear: -C/--find-copies is not passed
        # to git log above, so copied files are not detected as related.
        if status[:1] == "R" and len(parts) == 3:
            old, new = parts[1], parts[2]
            present = resolve(new)
            renamed_to[old] = present
            if present in fileset:
                churn[present] += 1
                current.add(present)
        elif len(parts) == 2:
            present = resolve(parts[1])
            if present in fileset:
                churn[present] += 1
                current.add(present)
    if current:
        commits.append(current)

    top = set(sorted(churn, key=churn.get, reverse=True)[:churn_top])
    pairs = defaultdict(int)
    for commit_files in commits:
        for a, b in combinations(sorted(commit_files & top), 2):
            # Same-directory co-change is expected; cross-package coupling is the smell.
            if os.path.dirname(a) != os.path.dirname(b):
                pairs[(a, b)] += 1
    coupling = []
    for (a, b), n in pairs.items():
        if n < min_shared:
            continue
        strength = n / min(churn[a], churn[b])
        if strength < min_strength:
            continue
        coupling.append(
            {"files": [a, b], "co_changes": n, "strength": round(strength, 2)}
        )
    # `files` as the final key keeps ties deterministic across runs.
    coupling.sort(key=lambda c: (-c["strength"], -c["co_changes"], c["files"]))
    return churn, coupling[:top_pairs]


def analyse(
    root: str, excludes: list[str], since: str | None, opts: argparse.Namespace
) -> dict[str, list[dict[str, object]]]:
    """Collect all signals for every source file and rank by composite score.

    Every row carries the full five-signal vector (0-filled where a signal
    does not apply, e.g. defs/fan-in/fan-out for non-Python files) so
    consumers that paste rows as evidence always see a stable schema.
    """
    files = tracked_source_files(root, excludes)
    if not files:
        return {"files": [], "coupling": []}

    rows = {
        p: {
            "path": p,
            "loc": count_loc(root, p),
            "defs": 0,
            "fan_in": 0,
            "fan_out": 0,
            "churn": 0,
        }
        for p in files
    }

    imports_of = {}
    for p in files:
        if p.endswith(".py"):
            defs, imports = py_structure(root, p)
            rows[p]["defs"] = defs
            imports_of[p] = imports

    # Fan-in/out resolved against project module names only. Each path is
    # indexed under all its keys (src/-stripped variants included).
    mod_to_path = {}
    for p in imports_of:
        for key in module_keys(p):
            other = mod_to_path.get(key)
            if other is not None and other != p:
                print(
                    f"warning: module key {key!r} maps to both {other} and {p}; "
                    "keeping the first",
                    file=sys.stderr,
                )
                continue
            mod_to_path[key] = p
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

    churn, coupling = churn_and_coupling(
        root,
        files,
        since,
        opts.churn_top,
        opts.coupling_min_shared,
        opts.coupling_min_strength,
        opts.coupling_top,
    )
    for p in files:
        rows[p]["churn"] = churn.get(p, 0)

    # Fixed signal vector, 0-filled: comparable ranks across languages.
    peaks = {s: max((r[s] for r in rows.values()), default=0) for s in SIGNALS}
    for r in rows.values():
        total = sum(r[s] / peaks[s] for s in SIGNALS if peaks[s] > 0)
        r["score"] = round(total / len(SIGNALS), 3)
        # Absolute gate — the normalised score orders candidates only.
        r["candidate"] = (
            r["loc"] >= opts.candidate_loc or r["defs"] >= opts.candidate_defs
        )

    # Path as secondary key keeps equal-score ordering deterministic.
    ranked = sorted(rows.values(), key=lambda r: (-r["score"], r["path"]))
    return {"files": ranked, "coupling": coupling}


def load_baseline(path: str) -> dict[str, int | float]:
    """Load and validate the scorecard baseline, or {} when absent.

    A malformed scorecard (hand-edited, merge-conflicted) must fail with a
    clear message naming the file — not an AttributeError/TypeError
    traceback from deep inside the ratchet comparison.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        baseline = json.load(f)
    if not isinstance(baseline, dict):
        sys.exit(
            f"error: scorecard {path} must contain a JSON object, "
            f"got {type(baseline).__name__}"
        )
    for key in SCORECARD_KEYS:
        value = baseline.get(key)
        # bool is an int subclass, but `true` here means a corrupted file.
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            sys.exit(f"error: scorecard {path}: {key} must be a number, got {value!r}")
    return baseline


def ratchet(
    report: dict[str, list[dict[str, object]]], path: str, update: bool
) -> tuple[dict[str, int], dict[str, int | float], dict[str, tuple[int | float, int]]]:
    """Compare absolute structural metrics against the stored baseline."""
    # Rows always carry the full signal vector (0-filled for non-Python
    # files), so the maxima over all files equal the maxima over Python ones.
    current = {
        "max_file_loc": max((r["loc"] for r in report["files"]), default=0),
        "max_fan_in": max((r["fan_in"] for r in report["files"]), default=0),
        "max_defs_per_file": max((r["defs"] for r in report["files"]), default=0),
    }
    baseline = load_baseline(path)

    regressions = {
        k: (baseline[k], v)
        for k, v in current.items()
        if k in baseline and v > baseline[k]
    }
    if update and not regressions:
        tightened = {k: min(v, baseline.get(k, v)) for k, v in current.items()}
        target_dir = os.path.dirname(path) or "."
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(tightened, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    return current, baseline, regressions


def main() -> None:
    """CLI entry point: analyse the repo, print JSON, apply the ratchet."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=".", help="repo root (default: cwd)")
    ap.add_argument("--top", type=int, default=10, help="files to report (default 10)")
    ap.add_argument("--since", help="limit churn window, e.g. '18 months ago'")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="extra exclude glob (repeatable)",
    )
    ap.add_argument(
        "--candidate-loc",
        type=int,
        default=400,
        help="absolute LOC threshold for the candidate flag (default 400)",
    )
    ap.add_argument(
        "--candidate-defs",
        type=int,
        default=30,
        help="absolute defs-per-file threshold for the candidate flag (default 30)",
    )
    ap.add_argument(
        "--churn-top",
        type=int,
        default=40,
        help="highest-churn files considered for temporal coupling (default 40)",
    )
    ap.add_argument(
        "--coupling-min-shared",
        type=int,
        default=3,
        help="minimum co-changes for a coupling pair to be reported (default 3)",
    )
    ap.add_argument(
        "--coupling-min-strength",
        type=float,
        default=0.5,
        help="minimum coupling strength (co-changes / min churn) reported (default 0.5)",
    )
    ap.add_argument(
        "--coupling-top",
        type=int,
        default=20,
        help="coupling pairs to report (default 20)",
    )
    ap.add_argument(
        "--scorecard",
        help="path to scorecard baseline JSON (relative paths resolve "
        "against the repo root)",
    )
    ap.add_argument(
        "--update",
        action="store_true",
        help="tighten the scorecard to current values (no-op on regression)",
    )
    args = ap.parse_args()
    if args.update and not args.scorecard:
        ap.error("--update requires --scorecard")

    root = os.path.abspath(args.root)
    try:
        sh(["git", "rev-parse", "--git-dir"], root)
    except FileNotFoundError:
        sys.exit("error: git not found on PATH")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: git cannot read {root}: {e.stderr.strip()}")

    report = analyse(root, DEFAULT_EXCLUDES + args.exclude, args.since, args)
    out = {
        "root": root,
        "top": report["files"][: args.top],
        "coupling": report["coupling"],
        "files_analysed": len(report["files"]),
    }

    exit_code = 0
    if args.scorecard and not report["files"]:
        # An empty analysis (wrong root, over-broad excludes) would ratchet
        # every baseline to zero and make all future legitimate runs fail;
        # never let a zero-file run touch or judge the scorecard.
        print(
            "warning: no source files matched; skipping scorecard check and update",
            file=sys.stderr,
        )
    elif args.scorecard:
        scorecard = args.scorecard
        if not os.path.isabs(scorecard):
            scorecard = os.path.join(root, scorecard)
        current, baseline, regressions = ratchet(report, scorecard, args.update)
        out["scorecard"] = {
            "current": current,
            "baseline": baseline,
            "regressions": {
                k: {"baseline": b, "current": c} for k, (b, c) in regressions.items()
            },
        }
        if regressions:
            exit_code = 2

    json.dump(out, sys.stdout, indent=2)
    print()
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError:
        # Reached when git vanishes after the initial rev-parse probe (or a
        # later git subcommand is missing); keep the same one-line message.
        sys.exit("error: git not found on PATH")
    except subprocess.CalledProcessError as e:
        cmd = " ".join(e.cmd) if isinstance(e.cmd, (list, tuple)) else str(e.cmd)
        sys.exit(f"error: command failed ({cmd}): {(e.stderr or '').strip()}")
    except json.JSONDecodeError as e:
        sys.exit(f"error: invalid JSON: {e}")
