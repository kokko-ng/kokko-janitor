"""Tests for plugins/kokko-janitor/scripts/hotspots.py against throwaway git repos."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "kokko-janitor"
    / "scripts"
    / "hotspots.py"
)

spec = importlib.util.spec_from_file_location("hotspots", SCRIPT)
hotspots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotspots)


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write_and_commit(repo, files, "initial")
    return repo


def write_and_commit(repo, files, message):
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "--", *files)
    git(repo, "commit", "-q", "-m", message)


def run_script(repo, *args, cwd=None):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), *args],
        capture_output=True,
        text=True,
        cwd=cwd or repo,
    )
    report = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc, report


def rows_by_path(report):
    return {r["path"]: r for r in report["top"]}


def test_exclusions_are_path_anchored(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "latest_prices.py": "x = 1\n",
            "contest.py": "x = 1\n",
            "attestation.py": "x = 1\n",
            "tests/test_foo.py": "x = 1\n",
            "test_bar.py": "x = 1\n",
            "pkg/foo_test.py": "x = 1\n",
            "web/app.spec.ts": "let x = 1;\n",
        },
    )
    files = hotspots.tracked_source_files(str(repo), hotspots.DEFAULT_EXCLUDES)
    assert "latest_prices.py" in files
    assert "contest.py" in files
    assert "attestation.py" in files
    assert "tests/test_foo.py" not in files
    assert "test_bar.py" not in files
    assert "pkg/foo_test.py" not in files
    assert "web/app.spec.ts" not in files


def test_relative_import_resolution(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/db.py": "x = 1\n",
            "pkg/api/__init__.py": "",
            "pkg/api/tasks.py": "from ..db import x\n",
        },
    )
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert rows["pkg/api/tasks.py"]["fan_out"] == 1
    assert rows["pkg/db.py"]["fan_in"] == 1


def test_src_layout_fan_in_out(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/mod.py": "x = 1\n",
            "src/pkg/user.py": "from pkg.mod import x\n",
        },
    )
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert rows["src/pkg/user.py"]["fan_out"] == 1
    assert rows["src/pkg/mod.py"]["fan_in"] == 1


def test_ratchet_exits_2_on_regression(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\nx = 2\nx = 3\n"})
    scorecard = repo / ".janitor" / "scorecard.json"
    scorecard.parent.mkdir()
    baseline = {"max_file_loc": 1, "max_fan_in": 0, "max_defs_per_file": 0}
    scorecard.write_text(json.dumps(baseline) + "\n", encoding="utf-8")

    proc, report = run_script(repo, "--scorecard", str(scorecard))
    assert proc.returncode == 2
    assert "max_file_loc" in report["scorecard"]["regressions"]


def test_update_does_not_overwrite_on_regression(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\nx = 2\nx = 3\n"})
    scorecard = repo / ".janitor" / "scorecard.json"
    scorecard.parent.mkdir()
    baseline = {"max_file_loc": 1, "max_fan_in": 0, "max_defs_per_file": 0}
    text_before = json.dumps(baseline) + "\n"
    scorecard.write_text(text_before, encoding="utf-8")

    proc, _ = run_script(repo, "--scorecard", str(scorecard), "--update")
    assert proc.returncode == 2
    assert scorecard.read_text(encoding="utf-8") == text_before


def test_scorecard_relative_path_resolves_against_repo_root(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc, report = run_script(
        repo, "--scorecard", ".janitor/scorecard.json", "--update", cwd=elsewhere
    )
    assert proc.returncode == 0
    assert (repo / ".janitor" / "scorecard.json").is_file()
    assert not (elsewhere / ".janitor").exists()
    saved = json.loads((repo / ".janitor" / "scorecard.json").read_text())
    assert saved == report["scorecard"]["current"]


def test_non_ascii_filename_churn_is_counted(tmp_path):
    repo = make_repo(tmp_path, {"café.py": "x = 1\n"})
    write_and_commit(repo, {"café.py": "x = 2\n"}, "second touch")

    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert rows["café.py"]["churn"] == 2


def test_rename_keeps_churn(tmp_path):
    repo = make_repo(tmp_path, {"old_name.py": "x = 1\n" * 5})
    write_and_commit(repo, {"old_name.py": "x = 2\n" * 5}, "touch")
    git(repo, "mv", "old_name.py", "new_name.py")
    git(repo, "commit", "-q", "-m", "rename")

    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert "old_name.py" not in rows
    assert rows["new_name.py"]["churn"] == 3


def test_candidate_flag_at_thresholds(tmp_path):
    at_loc = "x = 1\n" * 400
    under_loc = "x = 1\n" * 399
    at_defs = "".join(f"def f{i}():\n    pass\n" for i in range(30))
    under_defs = "".join(f"def f{i}():\n    pass\n" for i in range(29))
    repo = make_repo(
        tmp_path,
        {
            "at_loc.py": at_loc,
            "under_loc.py": under_loc,
            "at_defs.py": at_defs,
            "under_defs.py": under_defs,
        },
    )
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert rows["at_loc.py"]["candidate"] is True
    assert rows["under_loc.py"]["candidate"] is False
    assert rows["at_defs.py"]["candidate"] is True
    assert rows["under_defs.py"]["candidate"] is False


def test_score_uses_fixed_signal_vector(tmp_path):
    # A non-Python file must not out-rank by dilution: its score is computed
    # over the same five-signal vector with defs/fan_in/fan_out zero-filled.
    repo = make_repo(
        tmp_path,
        {
            "big.py": "".join(f"def f{i}():\n    pass\n" for i in range(40)),
            "small.ts": "let x = 1;\n" * 8,
        },
    )
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert rows["big.py"]["score"] > rows["small.ts"]["score"]
    assert rows["small.ts"]["score"] < 1.0
