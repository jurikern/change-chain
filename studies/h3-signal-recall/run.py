"""H3 run (prereg-h3.md). Read-only on every other repo.

Writes results.csv (counts only) and results.json (counts only) in this directory.
It keeps no commit message, no e-mail address and no path in any output.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import glob
import json
import os
import subprocess
import sys

import signals as s

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN = os.environ.get("H3_CHAIN", "chain.py")  # path to change-chain chain.py
POLICY = os.environ.get("H3_POLICY", "policy.jsonl")  # hook capture (policy rows)
SINCE = "2026-10-21"
START = dt.datetime(2026, 10, 21, 7, 0, 0, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 10, 23, 3, 0, 0, tzinfo=dt.timezone.utc)
SIGS = ["S1", "S2", "S3", "U", "S2L", "S3L", "UL"]
LABELS = ["ESTABLISHED", "INFERRED", "MISSING"]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def frame():
    roots = [r for r in os.environ.get("H3_ROOTS", "").split(os.pathsep) if r]  # folders that hold the repos
    cands = set(p for r in roots for p in glob.glob(os.path.join(r, "*")) + glob.glob(os.path.join(r, ".[a-z]*")))
    try:
        with open(POLICY) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("toplevel") and r.get("ts", "") >= "2026-10-21T07:00:00":
                    cands.add(r["toplevel"])
    except OSError:
        pass
    repos = sorted(p for p in cands if os.path.exists(os.path.join(p, ".git"))
                   and os.path.basename(p) != os.path.basename(HERE))
    return repos


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True, env=ENV).stdout


def main():
    repos = frame()
    names = s.public_repo_names(repos)
    per_sha = {}  # sha -> {"labels": [...], "repo": first repo, "parents": [...]}
    errors = 0
    for repo in repos:
        p = subprocess.run([sys.executable, CHAIN, "export", "--repo", repo, "--since", SINCE],
                           capture_output=True, text=True, env=ENV)
        if p.returncode != 0:
            errors += 1
            continue
        for line in p.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            c = row["commit"]
            ct = dt.datetime.fromisoformat(c["commit_time"])
            if not (START <= ct < END):
                continue
            e = per_sha.setdefault(c["sha"], {"labels": [], "repo": repo, "parents": c["parents"]})
            e["labels"].append(row["session"]["label"])

    counts = collections.Counter()
    misses = {"U": collections.Counter(), "UL": collections.Counter()}
    per_repo = collections.Counter()
    label_disagree = 0
    for sha, e in per_sha.items():
        label = s.best_label(e["labels"])
        if len(set(e["labels"])) > 1:
            label_disagree += 1
        out = git(e["repo"], "log", "-1", "--format=%ae%x00%B", sha)
        email, _, msg = out.partition("\x00")
        m = s.match_all(email, msg)
        counts[(label, "n")] += 1
        per_repo[(names[e["repo"]], label)] += 1
        for k in SIGS:
            counts[(label, k)] += int(m[k])
        if label == "ESTABLISHED":
            for u in ("U", "UL"):
                if not m[u]:
                    misses[u][s.miss_reason(msg, e["parents"])] += 1
            if len(e["parents"]) >= 2:
                counts[("ESTABLISHED", "merges")] += 1
        del email, msg, out

    rows = []
    for label in LABELS:
        n = counts[(label, "n")]
        for k in SIGS:
            kk = counts[(label, k)]
            lo, hi = s.wilson(kk, n)
            rows.append({"label": label, "signal": k, "k": kk, "n": n,
                         "rate": round(kk / n, 4) if n else "",
                         "wilson_lo": round(lo, 4) if lo is not None else "",
                         "wilson_hi": round(hi, 4) if hi is not None else ""})
    with open(os.path.join(HERE, "results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["label", "signal", "k", "n", "rate", "wilson_lo", "wilson_hi"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(HERE, "miss-reasons.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["union", "reason", "count"])
        for u in ("U", "UL"):
            for r in ("M1", "M2", "M3", "M4", "M5"):
                w.writerow([u, r, misses[u][r]])
    summary = {
        "window": [START.isoformat(), END.isoformat()], "repos_in_frame": len(repos),
        "repos_with_error": errors, "unique_commits": len(per_sha),
        "label_disagreements_across_worktrees": label_disagree,
        "n": {l: counts[(l, "n")] for l in LABELS},
        "established_merges": counts[("ESTABLISHED", "merges")],
        "miss_reasons": {u: dict(misses[u]) for u in misses},
        "per_repo": {f"{r}|{l}": v for (r, l), v in sorted(per_repo.items())},
        "rows": rows,
    }
    with open(os.path.join(HERE, "results.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1))


if __name__ == "__main__":
    main()
