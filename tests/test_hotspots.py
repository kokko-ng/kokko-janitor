"""Tests for the hotspot ranker.

hotspots.py is the deterministic half of the janitor: it decides which modules
the LLM judges even look at. A silent regression here does not produce a wrong
answer, it produces a *plausible* one -- the wrong files get judged and the
report reads exactly as it should. Nothing tested it before.

The scorecard ratchet gets the most attention, because it is the piece with a
committed artefact and a non-zero exit code hanging off it: a ratchet that
fails open lets structure rot while reporting a clean run.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOTSPOTS = Path(__file__).resolve().parents[1] / "plugins/kokko-janitor/scripts/hotspots.py"


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def run(repo, *args, expect_code=0):
    proc = subprocess.run(
        [sys.executable, str(HOTSPOTS), str(repo), *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == expect_code, f"exit {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout)


def write(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


# ---------------------------------------------------------------------------
# Basic analysis
# ---------------------------------------------------------------------------

def test_empty_repo_reports_nothing(repo):
    write(repo, "README.md", "# nothing to analyse\n")
    commit(repo)
    out = run(repo)
    assert out["files_analysed"] == 0
    assert out["top"] == []


def test_untracked_files_are_ignored(repo):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    write(repo, "untracked.py", "y = 2\n")
    out = run(repo)
    assert [f["path"] for f in out["top"]] == ["a.py"]


def test_loc_counts_ignore_blank_lines(repo):
    write(repo, "a.py", "x = 1\n\n\n\ny = 2\n")
    commit(repo)
    out = run(repo)
    assert out["top"][0]["loc"] == 2


def test_test_files_are_excluded_by_default(repo):
    write(repo, "app.py", "x = 1\n")
    write(repo, "test_app.py", "\n".join(f"line{i} = {i}" for i in range(200)))
    commit(repo)
    out = run(repo)
    assert [f["path"] for f in out["top"]] == ["app.py"]


def test_non_source_extensions_are_skipped(repo):
    write(repo, "a.py", "x = 1\n")
    write(repo, "data.json", "{}\n")
    write(repo, "notes.md", "hello\n")
    commit(repo)
    assert [f["path"] for f in run(repo)["top"]] == ["a.py"]


def test_the_biggest_busiest_file_ranks_first(repo):
    write(repo, "small.py", "x = 1\n")
    write(repo, "god.py", "\n".join(f"def f{i}(): pass" for i in range(100)))
    commit(repo)
    for i in range(5):
        write(repo, "god.py", "\n".join(f"def f{j}(): pass" for j in range(101 + i)))
        commit(repo, f"churn {i}")
    out = run(repo)
    assert out["top"][0]["path"] == "god.py"
    assert out["top"][0]["score"] > out["top"][-1]["score"]


def test_top_limits_the_report(repo):
    for i in range(10):
        write(repo, f"m{i}.py", f"x = {i}\n")
    commit(repo)
    assert len(run(repo, "--top", "3")["top"]) == 3
    assert run(repo, "--top", "3")["files_analysed"] == 10


def test_extra_excludes_are_honoured(repo):
    write(repo, "keep.py", "x = 1\n")
    write(repo, "nested/generated/thing.py", "y = 2\n")
    commit(repo)
    out = run(repo, "--exclude", "*/generated/*")
    assert [f["path"] for f in out["top"]] == ["keep.py"]


def test_a_root_level_excluded_directory_is_also_excluded(repo):
    """`*/dist/*` must exclude `dist/bundle.js`, which git ls-files emits with
    no leading directory for it to match against."""
    write(repo, "src/app.ts", "const x = 1;\n")
    write(repo, "dist/bundle.js", "\n".join(f"var x{i} = {i};" for i in range(500)))
    commit(repo)
    assert [f["path"] for f in run(repo)["top"]] == ["src/app.ts"]


# ---------------------------------------------------------------------------
# Python structure: defs, fan-in, fan-out
# ---------------------------------------------------------------------------

def test_definitions_are_counted_including_nested(repo):
    write(repo, "a.py", "class C:\n    def m(self): pass\ndef top(): pass\n")
    commit(repo)
    assert run(repo)["top"][0]["defs"] == 3


def test_fan_in_counts_importers(repo):
    write(repo, "db.py", "x = 1\n")
    write(repo, "a.py", "import db\n")
    write(repo, "b.py", "import db\n")
    commit(repo)
    rows = {f["path"]: f for f in run(repo)["top"]}
    assert rows["db.py"]["fan_in"] == 2
    assert rows["a.py"]["fan_out"] == 1


def test_external_imports_do_not_count_as_fan_out(repo):
    write(repo, "a.py", "import os\nimport requests\n")
    commit(repo)
    assert run(repo)["top"][0]["fan_out"] == 0


def test_relative_imports_resolve_against_the_package(repo):
    write(repo, "pkg/__init__.py", "")
    write(repo, "pkg/db.py", "x = 1\n")
    write(repo, "pkg/api/__init__.py", "")
    write(repo, "pkg/api/tasks.py", "from ..db import x\n")
    commit(repo)
    rows = {f["path"]: f for f in run(repo)["top"]}
    assert rows["pkg/db.py"]["fan_in"] == 1
    assert rows["pkg/api/tasks.py"]["fan_out"] == 1


def test_a_file_importing_itself_is_not_counted(repo):
    write(repo, "a.py", "import a\n")
    commit(repo)
    assert run(repo)["top"][0]["fan_out"] == 0


def test_a_syntax_error_does_not_abort_the_run(repo):
    write(repo, "broken.py", "def (((\n")
    write(repo, "fine.py", "x = 1\n")
    commit(repo)
    out = run(repo)
    assert out["files_analysed"] == 2


def test_non_python_sources_get_loc_and_churn_only(repo):
    write(repo, "app.ts", "const x = 1;\n")
    commit(repo)
    row = run(repo)["top"][0]
    assert row["loc"] == 1
    assert "churn" in row
    assert "defs" not in row


# ---------------------------------------------------------------------------
# Temporal coupling
# ---------------------------------------------------------------------------

def test_cross_directory_co_change_is_reported(repo):
    write(repo, "api/routes.py", "x = 1\n")
    write(repo, "ui/view.py", "y = 1\n")
    commit(repo)
    for i in range(5):
        write(repo, "api/routes.py", f"x = {i}\n")
        write(repo, "ui/view.py", f"y = {i}\n")
        commit(repo, f"paired {i}")
    pairs = [tuple(c["files"]) for c in run(repo)["coupling"]]
    assert ("api/routes.py", "ui/view.py") in pairs


def test_same_directory_co_change_is_not_reported(repo):
    write(repo, "api/a.py", "x = 1\n")
    write(repo, "api/b.py", "y = 1\n")
    commit(repo)
    for i in range(5):
        write(repo, "api/a.py", f"x = {i}\n")
        write(repo, "api/b.py", f"y = {i}\n")
        commit(repo, f"paired {i}")
    assert run(repo)["coupling"] == []


# ---------------------------------------------------------------------------
# Scorecard ratchet -- the part with a committed artefact and an exit code.
# ---------------------------------------------------------------------------

def test_first_run_writes_a_baseline(repo, tmp_path):
    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(10)))
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    assert json.loads(card.read_text())["max_file_loc"] == 10


def test_growth_beyond_the_baseline_exits_2(repo, tmp_path):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")

    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(50)))
    commit(repo, "grow")
    out = run(repo, "--scorecard", str(card), expect_code=2)
    assert "max_file_loc" in out["scorecard"]["regressions"]


def test_a_regression_does_not_loosen_the_baseline(repo, tmp_path):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    before = card.read_text()

    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(50)))
    commit(repo, "grow")
    run(repo, "--scorecard", str(card), "--update", expect_code=2)
    assert card.read_text() == before, "the ratchet must never widen on a regression"


def test_improvement_tightens_the_baseline(repo, tmp_path):
    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(50)))
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    assert json.loads(card.read_text())["max_file_loc"] == 50

    write(repo, "a.py", "x = 1\n")
    commit(repo, "shrink")
    run(repo, "--scorecard", str(card), "--update")
    assert json.loads(card.read_text())["max_file_loc"] == 1


def test_staying_flat_exits_0(repo, tmp_path):
    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(10)))
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    run(repo, "--scorecard", str(card), expect_code=0)


def test_without_update_the_baseline_is_untouched(repo, tmp_path):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    before = card.read_text()

    write(repo, "a.py", "")
    write(repo, "b.py", "y = 1\n")
    commit(repo, "change")
    run(repo, "--scorecard", str(card))
    assert card.read_text() == before


def test_no_scorecard_flag_means_no_scorecard_in_the_output(repo):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    assert "scorecard" not in run(repo)


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------

def test_a_non_git_directory_fails_loudly(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOTSPOTS), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "git cannot read" in proc.stderr


def test_output_is_valid_json_on_the_scorecard_failure_path(repo, tmp_path):
    write(repo, "a.py", "x = 1\n")
    commit(repo)
    card = tmp_path / "scorecard.json"
    run(repo, "--scorecard", str(card), "--update")
    write(repo, "a.py", "\n".join(f"x{i} = {i}" for i in range(50)))
    commit(repo, "grow")
    # run() already json.loads() the output; exit 2 must still emit a full report
    out = run(repo, "--scorecard", str(card), expect_code=2)
    assert out["top"]
    assert out["scorecard"]["current"]["max_file_loc"] == 50


def test_stdlib_only(repo):
    """The README promises stdlib-only. A stray dependency breaks every
    consumer that runs it with a bare python3."""
    source = HOTSPOTS.read_text()
    third_party = {"requests", "numpy", "pandas", "click", "rich", "yaml", "pydantic"}
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1].split(".")[0]
            assert module not in third_party, f"non-stdlib import: {line}"
