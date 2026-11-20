"""Compare hub scores between two versions of the repo, for the "Score changes" workflow.

Scores are computed only from committed files (data cache, config.yaml, scoring code), so the
scores of any commit can be recomputed exactly. The workflow scores the commit before a push
to the deploy branch and the pushed commit, and opens a GitHub issue when a hub's overall score
changed, highlighting tier changes.

  python scripts/score_changes.py snapshot --out after.json        (PYTHONPATH = the checkout)
  python scripts/score_changes.py baseline --before SHA --out before.json
  python scripts/score_changes.py compare before.json after.json --before SHA --after SHA \
      --branch main --repo owner/name --title-file t.txt --body-file b.md
  python scripts/score_changes.py publish --marker M --title-file t.txt --body-file b.md

`app` is imported only inside snapshot(), so the same file can score another checkout: it is
run with PYTHONPATH set to that checkout, and uses only functions every version of the app has
(store.build_from_cache, config.hubs, config.settings, scoring.score_hub).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ZERO_SHA = "0" * 40
LABEL = "score-change"
TIER_LABEL = "tier-change"
LABELS = {LABEL: ("0e8a16", "A hub's overall risk score changed on the deploy branch"),
          TIER_LABEL: ("d93f0b", "A hub moved to a different risk tier")}


# ---------- scoring one version ----------

def snapshot() -> dict:
    """Overall score, tier, rank and hazard sub-scores of every hub in the importable app."""
    from app import config, scoring, store
    store.build_from_cache()
    rows = []
    for h in config.hubs():
        b = scoring.score_hub(h["id"])
        rows.append({"hub_id": b.hub_id, "city": b.city, "state": b.state,
                     "overall": round(b.overall_score, 1), "tier": b.tier,
                     "hazards": {x.hazard: round(x.score, 1) for x in b.hazards}})
    rows.sort(key=lambda r: -r["overall"])  # as scoring.rank(): stable, highest first
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"hubs": {r.pop("hub_id"): r for r in rows},
            "tiers": [dict(t) for t in config.settings()["tiers"]]}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def baseline(before: str, repo: Path, out: Path) -> tuple[str, str, str | None]:
    """Score the "before" commit in a temporary worktree.

    Returns (status, message, sha): "scored" (written to out), "skipped" (no earlier version to
    compare: the branch was just created, or that commit has no app/ at all, like the empty
    main), or "failed" (the commit can't be found, or it has an app whose scores can't be
    calculated)."""
    before = (before or "").strip()
    out = Path(out).resolve()  # the snapshot runs inside the temporary worktree
    if not before or before == ZERO_SHA:
        return "skipped", "The push created the branch: there is no earlier version to compare.", None
    sha = _resolve(repo, before)
    if sha is None:
        return "failed", f"Commit `{before}` isn't in the repository history, so the earlier scores can't be calculated.", None
    if _git(repo, "cat-file", "-e", f"{sha}:app").returncode:
        return ("skipped", f"Commit `{sha[:7]}` has no app (no `app/` directory), so there are no "
                           "earlier scores to compare (e.g. the first merge into an empty main).", sha)
    with tempfile.TemporaryDirectory(prefix="scores-before-") as tmp:
        tree = Path(tmp) / "tree"
        r = _git(repo, "worktree", "add", "--detach", str(tree), sha)
        if r.returncode:
            return "failed", f"Couldn't check out `{sha[:7]}`: {_tail(r.stderr)}", sha
        try:
            env = {**os.environ, "PYTHONPATH": str(tree)}
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "snapshot",
                                "--out", str(out)], cwd=tree, env=env, capture_output=True,
                               text=True)
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree))
    if r.returncode or not out.exists():
        return "failed", (f"The scores of `{sha[:7]}` couldn't be calculated:\n\n```\n"
                          f"{_tail(r.stderr or r.stdout) or 'no output written'}\n```"), sha
    return "scored", f"Scored `{sha[:7]}`.", sha


def _resolve(repo: Path, ref: str) -> str | None:
    """A commit SHA for ref. Also tries origin/<ref> (a manual run's `before` can be a branch,
    which a CI checkout has only as a remote-tracking ref), and fetches a SHA that isn't in the
    clone yet (e.g. the old tip after a force push, which no branch reaches any more)."""
    def rev(name):
        r = _git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else None
    sha = rev(ref) or rev(f"refs/remotes/origin/{ref}")
    if sha is None and _git(repo, "fetch", "--quiet", "--no-tags", "origin", ref).returncode == 0:
        sha = rev(ref) or rev("FETCH_HEAD")
    return sha


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


# ---------- comparing two versions ----------

def compare(old: dict, new: dict) -> dict:
    """Hubs whose overall score changed (with tier and rank), hubs added or removed, and
    hazard sub-score changes. 'changed' is what opens an issue."""
    o, n = old["hubs"], new["hubs"]
    changes, hazard_only = [], []
    for hid in [h for h in n if h in o]:
        a, b = o[hid], n[hid]
        hz = [(k, a["hazards"].get(k), b["hazards"].get(k))
              for k in sorted(set(a["hazards"]) | set(b["hazards"]))
              if a["hazards"].get(k) != b["hazards"].get(k)]
        row = {"hub_id": hid, "name": f"{b['city']}, {b['state']}",
               "old": a["overall"], "new": b["overall"],
               "old_tier": a["tier"], "new_tier": b["tier"],
               "old_rank": a["rank"], "new_rank": b["rank"], "hazards": hz}
        if a["overall"] != b["overall"] or a["tier"] != b["tier"]:
            changes.append(row)
        elif hz:
            hazard_only.append(row)
    changes.sort(key=lambda c: (c["old_tier"] == c["new_tier"], -abs(c["new"] - c["old"])))
    added = [{"hub_id": h, "name": f"{n[h]['city']}, {n[h]['state']}", **n[h]} for h in n if h not in o]
    removed = [{"hub_id": h, "name": f"{o[h]['city']}, {o[h]['state']}", **o[h]} for h in o if h not in n]
    tier_changes = [c for c in changes if c["old_tier"] != c["new_tier"]]
    return {"changes": changes, "tier_changes": tier_changes, "hazard_only": hazard_only,
            "added": added, "removed": removed,
            "tiers_changed": old.get("tiers") != new.get("tiers"),
            "old_tiers": old.get("tiers"), "new_tiers": new.get("tiers"),
            "changed": bool(changes or added or removed)}


def cutoff_note(score: float, tiers: list[dict]) -> str:
    """Where a score sits relative to the nearest tier cutoff, e.g. '0.1 below Moderate (35)'."""
    ts = sorted(tiers, key=lambda t: t["min"])
    own = max((t for t in ts if score >= t["min"]), key=lambda t: t["min"], default=ts[0])
    up = next((t for t in ts if t["min"] > score), None)
    below = (round(score - own["min"], 1), own) if own["min"] > 0 else None
    above = (round(up["min"] - score, 1), up) if up else None
    if below and below[0] == 0:
        return f"exactly at the {own['name']} cutoff ({own['min']:g})"
    if above and (not below or above[0] <= below[0]):
        return f"{above[0]:.1f} below {above[1]['name']} ({above[1]['min']:g})"
    if below:
        return f"{below[0]:.1f} above the {own['name']} cutoff ({own['min']:g})"
    return "no tier cutoff nearby"


def marker(before: str, after: str) -> str:
    return f"<!-- score-changes:{before}..{after} -->"


def _md(text) -> str:
    return str(text).replace("|", "\\|")


def _delta(a: float, b: float) -> str:
    d = round(b - a, 1)
    return f"+{d:.1f}" if d > 0 else (f"−{-d:.1f}" if d < 0 else "0.0")


def render_issue(cmp: dict, *, before: str, after: str, branch: str, repo: str,
                 server: str = "https://github.com", run_url: str | None = None) -> tuple[str, str]:
    """Issue title and Markdown body. The body starts with the comparison marker."""
    base = f"{server}/{repo}"
    n_hubs = len(cmp["changes"]) + len(cmp["added"]) + len(cmp["removed"])
    title = f"Score changes on {branch}: {n_hubs} hub{'s' if n_hubs != 1 else ''}"
    if cmp["tier_changes"]:
        tc = [f"{c['name'].split(',')[0]} {c['old_tier']} → {c['new_tier']}" for c in cmp["tier_changes"]]
        title += ", " + ", ".join(tc[:2]) + (f" (+{len(tc) - 2} more)" if len(tc) > 2 else "")

    out = [marker(before, after), ""]
    for c in cmp["tier_changes"]:
        out.append(f"> **⚠ Tier change:** {_md(c['name'])} moved from **{c['old_tier']}** to "
                   f"**{c['new_tier']}** ({c['old']:.1f} → {c['new']:.1f}).")
    if cmp["tier_changes"]:
        out.append("")
    out += [f"Overall risk scores changed on `{_md(branch)}` between "
            f"[`{before[:7]}`]({base}/commit/{before}) and [`{after[:7]}`]({base}/commit/{after}) "
            f"([diff]({base}/compare/{before}...{after})).", ""]
    tiers = cmp["new_tiers"] or []
    if cmp["changes"]:
        out += ["| Hub | Old | New | Δ | Tier | Rank | Nearest cutoff (new score) |",
                "|---|---|---|---|---|---|---|"]
        for c in cmp["changes"]:
            tier = (f"**⚠ {c['old_tier']} → {c['new_tier']}**" if c["old_tier"] != c["new_tier"]
                    else c["new_tier"])
            rank = (f"{c['old_rank']} → {c['new_rank']}" if c["old_rank"] != c["new_rank"]
                    else str(c["new_rank"]))
            out.append(f"| {_md(c['name'])} | {c['old']:.1f} | {c['new']:.1f} | "
                       f"{_delta(c['old'], c['new'])} | {tier} | {rank} | "
                       f"{cutoff_note(c['new'], tiers) if tiers else ''} |")
        out.append("")
    for key, label in (("added", "Hubs added"), ("removed", "Hubs removed")):
        if cmp[key]:
            out.append(f"**{label}:** " + ", ".join(
                f"{_md(h['name'])} ({h['overall']:.1f}, {h['tier']})" for h in cmp[key]))
            out.append("")
    if cmp["tiers_changed"]:
        fmt = lambda ts: ", ".join(f"{t['name']} ≥ {t['min']:g}" for t in ts or [])  # noqa: E731
        out += [f"**Tier cutoffs changed:** {fmt(cmp['old_tiers'])} → {fmt(cmp['new_tiers'])}", ""]
    hz_rows = [(c, k, a, b) for c in cmp["changes"] + cmp["hazard_only"] for k, a, b in c["hazards"]]
    if hz_rows:
        out += ["<details><summary>Hazard sub-scores that changed</summary>", "",
                "| Hub | Hazard | Old | New |", "|---|---|---|---|"]
        out += [f"| {_md(c['name'])} | {k} | {'' if a is None else f'{a:.1f}'} | "
                f"{'' if b is None else f'{b:.1f}'} |" for c, k, a, b in hz_rows]
        out += ["", "</details>", ""]
    out.append("_Opened by the Score changes workflow"
               + (f" ([run]({run_url}))" if run_url else "") + ". Scores are recomputed from "
               "the committed data, `config.yaml` and scoring code of both commits._")
    return title, "\n".join(out) + "\n"


def summary_no_change(before: str, after: str, branch: str) -> str:
    return (f"No hub's overall score changed on `{branch}` between `{before[:7]}` and "
            f"`{after[:7]}`. No issue opened.\n")


# ---------- publishing (GitHub CLI) ----------

def run_gh(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {_tail(r.stderr)}")
    return r.stdout


def find_reported(issues: list[dict], mark: str) -> dict | None:
    """The issue whose body has the marker as a line of its own (the first line we write)."""
    for issue in issues:
        if any(line.strip() == mark for line in (issue.get("body") or "").splitlines()):
            return issue
    return None


def publish(mark: str, title: str, body: str, tier_changed: bool, gh=run_gh) -> tuple[str, str]:
    """Open the issue unless one with this marker exists (open or closed). Returns
    ("exists", url) or ("created", url)."""
    if mark not in body.splitlines():
        raise ValueError("the issue body must contain the comparison marker on its own line")
    for name, (color, desc) in LABELS.items():
        gh(["label", "create", name, "--color", color, "--description", desc, "--force"])
    # No --label filter: gh would then use GitHub search, which can lag behind a just-opened
    # issue, and an issue whose label someone removed would be missed. The plain listing (most
    # recent 1000 issues, open and closed) is consistent; the marker is matched here.
    issues = json.loads(gh(["issue", "list", "--state", "all", "--limit", "1000",
                            "--json", "number,body,url"]) or "[]")
    existing = find_reported(issues, mark)
    if existing:
        return "exists", existing.get("url") or f"#{existing['number']}"
    labels = [LABEL] + ([TIER_LABEL] if tier_changed else [])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
    try:
        args = ["issue", "create", "--title", title, "--body-file", f.name]
        for label in labels:
            args += ["--label", label]
        url = gh(args).strip()
    finally:
        os.unlink(f.name)
    return "created", url


# ---------- CLI ----------

def _output(**values) -> None:
    """Step outputs for the workflow (GITHUB_OUTPUT), printed too."""
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in values.items():
        v = str(v).lower() if isinstance(v, bool) else str(v)
        print(f"{k}={v}")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--before", default="")
    b.add_argument("--out", required=True)
    b.add_argument("--repo-dir", default=".")
    c = sub.add_parser("compare")
    c.add_argument("old")
    c.add_argument("new")
    for opt in ("--before", "--after", "--branch", "--repo", "--title-file", "--body-file"):
        c.add_argument(opt, required=True)
    c.add_argument("--run-url")
    p = sub.add_parser("publish")
    for opt in ("--marker", "--title-file", "--body-file"):
        p.add_argument(opt, required=True)
    p.add_argument("--tier-changed", default="false")
    a = ap.parse_args(argv)

    if a.cmd == "snapshot":
        Path(a.out).write_text(json.dumps(snapshot(), indent=1), encoding="utf-8")
        return 0
    if a.cmd == "baseline":
        status, message, sha = baseline(a.before, Path(a.repo_dir), Path(a.out))
        _output(status=status, sha=sha or "")
        _summary(("**Score changes: skipped.** " if status == "skipped" else
                  "**Score changes: failed.** " if status == "failed" else "") + message)
        return 1 if status == "failed" else 0
    if a.cmd == "compare":
        old = json.loads(Path(a.old).read_text(encoding="utf-8"))
        new = json.loads(Path(a.new).read_text(encoding="utf-8"))
        cmp = compare(old, new)
        mark = marker(a.before, a.after)
        _output(changed=cmp["changed"], tier_changed=bool(cmp["tier_changes"]), marker=mark)
        if not cmp["changed"]:
            _summary(summary_no_change(a.before, a.after, a.branch))
            return 0
        title, body = render_issue(cmp, before=a.before, after=a.after, branch=a.branch,
                                   repo=a.repo, run_url=a.run_url)
        Path(a.title_file).write_text(title, encoding="utf-8")
        Path(a.body_file).write_text(body, encoding="utf-8")
        _summary(f"### {title}\n\n{body}")
        return 0
    if a.cmd == "publish":
        status, url = publish(a.marker, Path(a.title_file).read_text(encoding="utf-8").strip(),
                              Path(a.body_file).read_text(encoding="utf-8"),
                              a.tier_changed.lower() == "true")
        _summary(f"Opened issue {url}." if status == "created" else
                 f"Already reported in {url}: no new issue opened.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
