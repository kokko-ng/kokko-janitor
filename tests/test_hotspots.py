"""Tests for plugins/kokko-janitor/scripts/hotspots.py against throwaway git repos."""

import importlib.util
import json
import os
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


def git(repo, *args, env=None):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        # Extra vars (e.g. GIT_COMMITTER_DATE) merge over the real environment.
        env={**os.environ, **env} if env else None,
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
        check=False,
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


def test_zero_files_skips_ratchet_and_update(tmp_path):
    # A run that matches no source files must never touch the scorecard:
    # ratcheting all-zero metrics would make every later legitimate run
    # regress forever.
    repo = make_repo(tmp_path, {"README.md": "no source here\n"})
    scorecard = repo / ".janitor" / "scorecard.json"
    scorecard.parent.mkdir()
    text_before = (
        json.dumps({"max_file_loc": 50, "max_fan_in": 2, "max_defs_per_file": 5}) + "\n"
    )
    scorecard.write_text(text_before, encoding="utf-8")

    proc, report = run_script(repo, "--scorecard", str(scorecard), "--update")
    assert proc.returncode == 0
    assert "no source files matched" in proc.stderr
    assert scorecard.read_text(encoding="utf-8") == text_before
    assert "scorecard" not in report


def test_missing_git_is_a_friendly_error(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    # sys.executable is absolute, so python still starts with an empty PATH;
    # the script's own `git` invocations are what must fail cleanly.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        capture_output=True,
        text=True,
        cwd=repo,
        check=False,
        env={**os.environ, "PATH": str(empty_bin)},
    )
    assert proc.returncode == 1
    assert "git not found on PATH" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_update_without_scorecard_is_an_argparse_error(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    proc, _ = run_script(repo, "--update")
    assert proc.returncode == 2
    assert "--update requires --scorecard" in proc.stderr


def test_scorecard_must_be_a_json_object(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    scorecard = repo / "scorecard.json"
    scorecard.write_text("[1, 2, 3]\n", encoding="utf-8")

    proc, _ = run_script(repo, "--scorecard", str(scorecard))
    assert proc.returncode == 1
    assert "scorecard" in proc.stderr
    assert str(scorecard) in proc.stderr
    assert "Traceback" not in proc.stderr


def test_scorecard_values_must_be_numeric(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    scorecard = repo / "scorecard.json"
    scorecard.write_text(json.dumps({"max_file_loc": "big"}) + "\n", encoding="utf-8")

    proc, _ = run_script(repo, "--scorecard", str(scorecard))
    assert proc.returncode == 1
    assert "max_file_loc" in proc.stderr
    assert str(scorecard) in proc.stderr
    assert "Traceback" not in proc.stderr


def test_every_row_has_all_five_signal_keys(tmp_path):
    # Non-Python files must still emit defs/fan_in/fan_out (as 0), so
    # consumers pasting rows as evidence always see the same schema.
    repo = make_repo(tmp_path, {"web/app.ts": "let x = 1;\n"})
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    row = rows_by_path(report)["web/app.ts"]
    for key in ("loc", "defs", "fan_in", "fan_out", "churn"):
        assert key in row
    assert row["defs"] == 0
    assert row["fan_in"] == 0
    assert row["fan_out"] == 0


def test_jest_spec_and_storybook_dirs_are_excluded(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "__tests__/util.test-helper.ts": "let x = 1;\n",
            "src/__tests__/more.ts": "let x = 1;\n",
            "spec/user_helper.rb": "x = 1\n",
            "app/spec/order_helper.rb": "x = 1\n",
            "src/Button.stories.tsx": "let x = 1;\n",
            # Controls: substrings of the new patterns must NOT be dropped.
            "spectrum.py": "x = 1\n",
            "src/inspect_utils.py": "x = 1\n",
        },
    )
    files = hotspots.tracked_source_files(str(repo), hotspots.DEFAULT_EXCLUDES)
    assert "__tests__/util.test-helper.ts" not in files
    assert "src/__tests__/more.ts" not in files
    assert "spec/user_helper.rb" not in files
    assert "app/spec/order_helper.rb" not in files
    assert "src/Button.stories.tsx" not in files
    assert "spectrum.py" in files
    assert "src/inspect_utils.py" in files


def coupling_pairs(report):
    return {tuple(c["files"]): c for c in report["coupling"]}


def make_coupled_repo(tmp_path):
    """Three files co-committed 3 times: a cross-directory pair and a
    same-directory pair, both meeting the default thresholds."""
    files = {
        "a/x.py": "x = {}\n",
        "a/y.py": "y = {}\n",
        "b/z.py": "z = {}\n",
    }
    repo = make_repo(tmp_path, {k: v.format(0) for k, v in files.items()})
    for i in (1, 2):
        write_and_commit(repo, {k: v.format(i) for k, v in files.items()}, f"round {i}")
    return repo


def test_cross_directory_coupling_is_reported(tmp_path):
    repo = make_coupled_repo(tmp_path)
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    pairs = coupling_pairs(report)
    pair = pairs[("a/x.py", "b/z.py")]
    assert pair["co_changes"] == 3
    assert pair["strength"] == 1.0
    assert ("a/y.py", "b/z.py") in pairs


def test_same_directory_coupling_is_excluded(tmp_path):
    # Same-directory co-change is expected cohesion, not a smell — that
    # exclusion is the whole point of the feature.
    repo = make_coupled_repo(tmp_path)
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    assert ("a/x.py", "a/y.py") not in coupling_pairs(report)


def test_coupling_min_shared_raises_the_bar(tmp_path):
    repo = make_coupled_repo(tmp_path)
    proc, report = run_script(repo, "--top", "20", "--coupling-min-shared", "4")
    assert proc.returncode == 0
    assert report["coupling"] == []


def test_ratchet_exits_2_on_fan_in_regression(tmp_path):
    repo = make_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/db.py": "x = 1\n",
            "pkg/user.py": "from pkg.db import x\n",
        },
    )
    scorecard = repo / ".janitor" / "scorecard.json"
    scorecard.parent.mkdir()
    baseline = {"max_file_loc": 100, "max_fan_in": 0, "max_defs_per_file": 100}
    scorecard.write_text(json.dumps(baseline) + "\n", encoding="utf-8")

    proc, report = run_script(repo, "--scorecard", str(scorecard))
    assert proc.returncode == 2
    assert list(report["scorecard"]["regressions"]) == ["max_fan_in"]


def test_ratchet_exits_2_on_defs_regression(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "def f():\n    pass\n"})
    scorecard = repo / ".janitor" / "scorecard.json"
    scorecard.parent.mkdir()
    baseline = {"max_file_loc": 100, "max_fan_in": 100, "max_defs_per_file": 0}
    scorecard.write_text(json.dumps(baseline) + "\n", encoding="utf-8")

    proc, report = run_script(repo, "--scorecard", str(scorecard))
    assert proc.returncode == 2
    assert list(report["scorecard"]["regressions"]) == ["max_defs_per_file"]


def test_extra_exclude_glob_excludes(tmp_path):
    repo = make_repo(tmp_path, {"gen/schema.py": "x = 1\n", "kept.py": "x = 1\n"})
    proc, report = run_script(repo, "--top", "20", "--exclude", "gen/*")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    assert "gen/schema.py" not in rows
    assert "kept.py" in rows
    assert report["files_analysed"] == 1


def test_since_restricts_churn_window(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    old_date = {
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00 +0000",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00 +0000",
    }
    for rel, content in {"old.py": "x = 1\n", "new.py": "y = 1\n"}.items():
        (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "add", "--", "old.py", "new.py")
    git(repo, "commit", "-q", "-m", "ancient", env=old_date)
    write_and_commit(repo, {"new.py": "y = 2\n"}, "recent")

    proc, report = run_script(repo, "--top", "20", "--since", "2023-01-01")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    # The ancient commit falls outside the window; only new.py's recent
    # touch counts.
    assert rows["old.py"]["churn"] == 0
    assert rows["new.py"]["churn"] == 1


def test_unparseable_python_warns_but_does_not_abort(tmp_path):
    repo = make_repo(tmp_path, {"broken.py": "def broken(:\n", "fine.py": "x = 1\n"})
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    assert "cannot parse broken.py" in proc.stderr
    rows = rows_by_path(report)
    assert rows["broken.py"]["defs"] == 0
    assert rows["broken.py"]["loc"] == 1
    assert "fine.py" in rows


def test_from_dot_import_names_resolve(tmp_path):
    # `from . import db` imports MODULES by name — the names list, not the
    # module attribute, is what must resolve for fan-in/fan-out.
    repo = make_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/db.py": "x = 1\n",
            "pkg/user.py": "from . import db\n",
        },
    )
    proc, report = run_script(repo, "--top", "20")
    assert proc.returncode == 0
    rows = rows_by_path(report)
    # fan_out counts both edges: `from . import db` imports the package
    # (pkg/__init__.py) and the named module (pkg/db.py).
    assert rows["pkg/user.py"]["fan_out"] == 2
    assert rows["pkg/db.py"]["fan_in"] == 1
    assert rows["pkg/__init__.py"]["fan_in"] == 1
