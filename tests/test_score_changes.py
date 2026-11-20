"""Score-change issues (scripts/score_changes.py, used by .github/workflows/score-changes.yml).

- snapshot(): the scores the app shows (overall, tier, rank, hazard sub-scores).
- compare()/render_issue(): old and new overall scores, tier changes highlighted.
- baseline(): skipped only when there is no earlier version to score; a version that exists
  but can't be scored is a failure.
- publish(): never opens a second issue for the same comparison.
"""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import scoring, store
from scripts import score_changes as sc

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "score_changes.py"
TIERS = [{"name": "Low", "min": 0}, {"name": "Moderate", "min": 35},
         {"name": "High", "min": 55}, {"name": "Severe", "min": 75}]
A, B = "a" * 40, "b" * 40


def setup_module():
    store.build_from_cache()


def snap(**hubs):
    """A synthetic snapshot: hub=(overall, tier, rank[, hazards])."""
    out = {}
    for hid, v in hubs.items():
        overall, tier, rank = v[:3]
        out[hid] = {"city": hid.title(), "state": "TX", "overall": overall, "tier": tier,
                    "rank": rank, "hazards": v[3] if len(v) > 3 else {"flood": 40.0}}
    return {"hubs": out, "tiers": TIERS}


def render(cmp):
    return sc.render_issue(cmp, before=A, after=B, branch="main", repo="o/r")


# ---------- snapshot = what the app shows ----------

def test_snapshot_matches_the_app():
    s = sc.snapshot()
    ranked = scoring.rank()
    assert [(h, s["hubs"][h]["overall"], s["hubs"][h]["tier"], s["hubs"][h]["rank"])
            for h in [r["hub_id"] for r in ranked]] == \
           [(r["hub_id"], r["overall_score"], r["tier"], r["rank"]) for r in ranked]
    assert s["hubs"]["dallas"] | {"hazards": None} == {
        "city": "Dallas", "state": "TX", "overall": 34.9, "tier": "Low", "rank": 3, "hazards": None}
    assert s["tiers"] == TIERS


# ---------- compare and render ----------

def test_crossing_up_into_moderate_is_highlighted():
    cmp = sc.compare(snap(dallas=(34.9, "Low", 3)), snap(dallas=(35.0, "Moderate", 3)))
    assert cmp["changed"] and [c["hub_id"] for c in cmp["tier_changes"]] == ["dallas"]
    title, body = render(cmp)
    assert title == "Score changes on main: 1 hub, Dallas Low → Moderate"
    assert body.splitlines()[0] == sc.marker(A, B)
    assert "**⚠ Tier change:** Dallas, TX moved from **Low** to **Moderate** (34.9 → 35.0)" in body
    assert "| Dallas, TX | 34.9 | 35.0 | +0.1 | **⚠ Low → Moderate** | 3 | exactly at the Moderate cutoff (35) |" in body


def test_crossing_down_is_highlighted_too():
    cmp = sc.compare(snap(houston=(56.7, "High", 1)), snap(houston=(47.4, "Moderate", 1)))
    title, body = render(cmp)
    assert "Houston High → Moderate" in title
    assert "| Houston, TX | 56.7 | 47.4 | −9.3 | **⚠ High → Moderate** | 1 | 7.6 below High (55) |" in body


def test_change_within_a_tier_opens_an_issue_without_a_tier_highlight():
    cmp = sc.compare(snap(chicago=(34.2, "Low", 4)), snap(chicago=(34.6, "Low", 4)))
    assert cmp["changed"] and not cmp["tier_changes"]
    title, body = render(cmp)
    assert title == "Score changes on main: 1 hub" and "⚠" not in body
    assert "| Chicago, TX | 34.2 | 34.6 | +0.4 | Low | 4 | 0.4 below Moderate (35) |" in body


def test_no_change_means_no_issue():
    s = snap(dallas=(34.9, "Low", 3), denver=(15.4, "Low", 7))
    assert not sc.compare(s, copy.deepcopy(s))["changed"]


def test_hazard_only_change_opens_no_issue_but_is_listed_when_one_opens():
    old = snap(dallas=(34.9, "Low", 3, {"flood": 41.7}), chicago=(34.2, "Low", 4))
    new = snap(dallas=(34.9, "Low", 3, {"flood": 41.9}), chicago=(34.2, "Low", 4))
    assert not sc.compare(old, new)["changed"]
    new["hubs"]["chicago"]["overall"] = 33.9
    cmp = sc.compare(old, new)
    _, body = render(cmp)
    assert "| Dallas, TX | flood | 41.7 | 41.9 |" in body  # in the collapsed hazard section


def test_rank_changes_and_added_or_removed_hubs_are_listed():
    old = snap(dallas=(34.9, "Low", 3), chicago=(34.2, "Low", 4), detroit=(29.3, "Low", 5))
    new = snap(dallas=(33.0, "Low", 4), chicago=(34.2, "Low", 3), boston=(20.0, "Low", 5))
    cmp = sc.compare(old, new)
    assert [c["hub_id"] for c in cmp["changes"]] == ["dallas"]
    _, body = render(cmp)
    assert "| Dallas, TX | 34.9 | 33.0 | −1.9 | Low | 3 → 4 |" in body
    assert "**Hubs added:** Boston, TX (20.0, Low)" in body
    assert "**Hubs removed:** Detroit, TX (29.3, Low)" in body


def test_changed_tier_cutoffs_are_shown():
    new = snap(dallas=(34.9, "Moderate", 3))
    new["tiers"] = [{"name": "Low", "min": 0}, {"name": "Moderate", "min": 34}] + TIERS[2:]
    _, body = render(sc.compare(snap(dallas=(34.9, "Low", 3)), new))
    assert "**Tier cutoffs changed:** Low ≥ 0, Moderate ≥ 35, High ≥ 55, Severe ≥ 75 → Low ≥ 0, Moderate ≥ 34" in body


@pytest.mark.parametrize("score,note", [
    (34.9, "0.1 below Moderate (35)"), (35.0, "exactly at the Moderate cutoff (35)"),
    (36.2, "1.2 above the Moderate cutoff (35)"), (15.4, "19.6 below Moderate (35)"),
    (80.0, "5.0 above the Severe cutoff (75)")])
def test_cutoff_note(score, note):
    assert sc.cutoff_note(score, TIERS) == note


# ---------- scoring another version (the mechanism CI uses for the old worktree) ----------

def copy_app(dest: Path):
    for d in ("app", "data/cache"):
        shutil.copytree(ROOT / d, dest / d, ignore=shutil.ignore_patterns("__pycache__", "*.db"))
    shutil.copy(ROOT / "data" / "hubs.json", dest / "data" / "hubs.json")
    shutil.copy(ROOT / "config.yaml", dest / "config.yaml")


def run_snapshot(checkout: Path, out: Path):
    import os
    env = {**os.environ, "PYTHONPATH": str(checkout)}
    return subprocess.run([sys.executable, str(SCRIPT), "snapshot", "--out", str(out)],
                          cwd=checkout, env=env, capture_output=True, text=True)


def test_another_checkout_is_scored_with_its_own_config(tmp_path):
    other = tmp_path / "other"
    copy_app(other)
    cfg = (other / "config.yaml").read_text(encoding="utf-8")
    assert "  heat: 0.15\n" in cfg
    (other / "config.yaml").write_text(cfg.replace("  heat: 0.15\n", "  heat: 0.45\n"), encoding="utf-8")
    r = run_snapshot(other, tmp_path / "other.json")
    assert r.returncode == 0, r.stderr
    new = json.loads((tmp_path / "other.json").read_text(encoding="utf-8"))
    cmp = sc.compare(sc.snapshot(), new)
    assert cmp["changed"]  # the copy's own weights were used, not this checkout's
    assert new["hubs"]["dallas"]["overall"] != 34.9


# ---------- baseline rules (in a temporary git repo) ----------

def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    """Commits: empty (like the empty main), the real app, then a broken app."""
    repo = tmp_path_factory.mktemp("history")
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty main")
    empty = git(repo, "rev-parse", "HEAD")
    copy_app(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "app")
    good = git(repo, "rev-parse", "HEAD")
    p = repo / "app" / "scoring.py"
    p.write_text(p.read_text(encoding="utf-8") + "\nraise RuntimeError('scoring is broken')\n",
                 encoding="utf-8")
    git(repo, "commit", "-q", "-am", "broken")
    broken = git(repo, "rev-parse", "HEAD")
    return repo, {"empty": empty, "good": good, "broken": broken}


def test_baseline_skips_the_empty_main(history, tmp_path):
    repo, c = history
    status, msg, sha = sc.baseline(c["empty"], repo, tmp_path / "b.json")
    assert (status, sha) == ("skipped", c["empty"]) and "no `app/` directory" in msg


def test_baseline_skips_a_newly_created_branch(history, tmp_path):
    repo, _ = history
    assert sc.baseline(sc.ZERO_SHA, repo, tmp_path / "b.json")[0] == "skipped"


def test_baseline_scores_an_existing_version(history, tmp_path):
    repo, c = history
    status, _, sha = sc.baseline(c["good"], repo, tmp_path / "b.json")
    assert (status, sha) == ("scored", c["good"])
    old = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert not sc.compare(old, sc.snapshot())["changed"]  # same app, data and config
    assert "tree" not in git(repo, "worktree", "list")  # temporary worktree removed


def test_baseline_fails_when_an_existing_version_cannot_be_scored(history, tmp_path):
    repo, c = history
    status, msg, _ = sc.baseline(c["broken"], repo, tmp_path / "b.json")
    assert status == "failed" and "scoring is broken" in msg
    assert not (tmp_path / "b.json").exists()


def test_baseline_fails_for_a_commit_missing_from_the_history(history, tmp_path):
    repo, _ = history
    status, msg, _ = sc.baseline("deadbeef" * 5, repo, tmp_path / "b.json")
    assert status == "failed" and "isn't in the repository history" in msg


def test_baseline_cli_exit_codes_and_outputs(history, tmp_path, monkeypatch):
    repo, c = history
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    args = ["baseline", "--repo-dir", str(repo), "--out", str(tmp_path / "b.json"), "--before"]
    assert sc.main(args + [c["empty"]]) == 0
    assert sc.main(args + [c["broken"]]) == 1          # a red run, not a silent skip
    assert "status=skipped" in out.read_text() and "status=failed" in out.read_text()
    assert "Score changes: failed." in (tmp_path / "summary.md").read_text()


# ---------- publish: no duplicate issues ----------

class FakeGh:
    def __init__(self, issues):
        self.issues, self.calls = issues, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/o/r/issues/99\n"
        return ""

    def created(self):
        return [a for a in self.calls if a[:2] == ["issue", "create"]]


def body_for(before=A, after=B):
    return sc.marker(before, after) + "\n\nScores changed.\n"


def test_existing_issue_with_the_same_marker_is_not_duplicated():
    gh = FakeGh([{"number": 7, "url": "https://github.com/o/r/issues/7", "body": body_for()}])
    assert sc.publish(sc.marker(A, B), "t", body_for(), False, gh=gh) == ("exists", "https://github.com/o/r/issues/7")
    assert gh.created() == []
    listing = next(a for a in gh.calls if a[:2] == ["issue", "list"])
    assert listing[listing.index("--state") + 1] == "all"  # closed issues count too
    # no --label filter: gh would answer it from GitHub search, which can lag, and an issue
    # whose label someone removed would be missed
    assert "--label" not in listing


def test_an_issue_whose_label_was_removed_still_counts():
    gh = FakeGh([{"number": 7, "url": "u7", "labels": [], "body": body_for()}])
    assert sc.publish(sc.marker(A, B), "t", body_for(), False, gh=gh) == ("exists", "u7")


def test_a_different_comparison_opens_an_issue_with_labels():
    gh = FakeGh([{"number": 7, "url": "u7", "body": body_for(after="c" * 40)}])
    assert sc.publish(sc.marker(A, B), "Score changes", body_for(), True, gh=gh) == (
        "created", "https://github.com/o/r/issues/99")
    (create,) = gh.created()
    assert create[create.index("--title") + 1] == "Score changes"
    assert [create[i + 1] for i, a in enumerate(create) if a == "--label"] == ["score-change", "tier-change"]
    assert any(a[:2] == ["label", "create"] and "--force" in a for a in gh.calls)


def test_marker_must_be_its_own_line_to_count():
    mention = {"number": 8, "url": "u8", "body": f"See {sc.marker(A, B)} for details"}
    gh = FakeGh([mention])
    assert sc.publish(sc.marker(A, B), "t", body_for(), False, gh=gh)[0] == "created"


def test_rerunning_the_same_push_end_to_end(tmp_path, monkeypatch):
    """compare -> publish twice for the same commit pair: one issue."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text(json.dumps(snap(dallas=(34.9, "Low", 3))))
    new.write_text(json.dumps(snap(dallas=(35.0, "Moderate", 3))))
    files = ["--title-file", str(tmp_path / "t"), "--body-file", str(tmp_path / "b")]
    assert sc.main(["compare", str(old), str(new), "--before", A, "--after", B, "--branch", "main",
                    "--repo", "o/r", *files]) == 0
    outputs = dict(line.split("=", 1) for line in (tmp_path / "o").read_text().splitlines())
    assert outputs["changed"] == "true" and outputs["tier_changed"] == "true"
    store_ = []
    def gh(args):
        if args[:2] == ["issue", "list"]:
            return json.dumps(store_)
        if args[:2] == ["issue", "create"]:
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            store_.append({"number": len(store_) + 1, "url": f"u{len(store_) + 1}", "body": body})
            return store_[-1]["url"]
        return ""
    body = (tmp_path / "b").read_text(encoding="utf-8")
    first = sc.publish(outputs["marker"], "t", body, True, gh=gh)
    second = sc.publish(outputs["marker"], "t", body, True, gh=gh)
    assert first == ("created", "u1") and second == ("exists", "u1") and len(store_) == 1


def test_publish_refuses_a_body_without_the_marker():
    with pytest.raises(ValueError):
        sc.publish(sc.marker(A, B), "t", "no marker here", False, gh=FakeGh([]))


# ---------- review follow-ups: branch names, force pushes, paths, moved modules ----------

@pytest.fixture(scope="module")
def remote_and_clone(tmp_path_factory):
    """An origin with a branch and an unreachable commit (like the old tip after a force
    push), and a clone that has neither as a local branch or object."""
    base = tmp_path_factory.mktemp("remote")
    origin, clone = base / "origin", base / "clone"
    origin.mkdir()
    git(origin, "init", "-q")
    copy_app(origin)
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "app")
    git(origin, "branch", "feature")
    git(origin, "checkout", "-q", "-b", "gone")
    cfg = origin / "config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("  heat: 0.15\n", "  heat: 0.45\n"),
                   encoding="utf-8")
    git(origin, "commit", "-q", "-am", "old tip, later force-pushed away")
    lost = git(origin, "rev-parse", "HEAD")
    git(origin, "checkout", "-q", "-")
    git(origin, "branch", "-D", "gone")
    git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")  # as GitHub allows
    # --no-local: transfer only reachable objects, as a real fetch from GitHub does
    subprocess.run(["git", "clone", "-q", "--no-local", str(origin), str(clone)], check=True)
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", lost]).returncode != 0
    return clone, lost


def test_a_branch_name_resolves_through_the_remote_tracking_ref(remote_and_clone, tmp_path):
    clone, _ = remote_and_clone
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet",
                           "refs/heads/feature"]).returncode != 0  # only origin/feature exists
    status, _, sha = sc.baseline("feature", clone, tmp_path / "b.json")
    assert status == "scored" and sha == git(clone, "rev-parse", "origin/feature")


def test_a_force_pushed_away_commit_is_fetched_and_scored(remote_and_clone, tmp_path):
    clone, lost = remote_and_clone
    status, _, sha = sc.baseline(lost, clone, tmp_path / "b.json")
    assert (status, sha) == ("scored", lost)
    old = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert sc.compare(old, sc.snapshot())["changed"]  # that version's own weights were used


def test_a_relative_out_path_is_kept_after_the_worktree_is_removed(history, tmp_path, monkeypatch):
    repo, c = history
    monkeypatch.chdir(tmp_path)
    assert sc.main(["baseline", "--repo-dir", str(repo), "--before", c["good"], "--out", "rel.json"]) == 0
    assert json.loads((tmp_path / "rel.json").read_text(encoding="utf-8"))["hubs"]["dallas"]["overall"] == 34.9


def test_moved_scoring_module_is_still_scored_and_a_missing_one_fails(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    copy_app(repo)
    (repo / "app" / "scoring").mkdir()
    (repo / "app" / "scoring.py").rename(repo / "app" / "scoring" / "__init__.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "scoring as a package")
    moved = git(repo, "rev-parse", "HEAD")
    status, _, _ = sc.baseline(moved, repo, tmp_path / "m.json")
    assert status == "scored"  # an existing app is never skipped
    git(repo, "rm", "-q", "-r", "app/scoring")
    git(repo, "commit", "-q", "-m", "no scoring module")
    status, msg, _ = sc.baseline(git(repo, "rev-parse", "HEAD"), repo, tmp_path / "n.json")
    assert status == "failed" and "couldn't be calculated" in msg
