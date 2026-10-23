#!/usr/bin/env python3
"""Build a synthetic example dataset for chain.py. All data is made up; nothing is read from this machine.

    python3 examples/make_demo.py [--out DIR]

DIR must be absent or empty. The default is a new temp dir. The script writes:
  DIR/repo/                 git repo with 2 commits (2026-10-20 10:00 and 11:00 UTC)
  DIR/home/.claude/         a settings.json for the policy digest
  DIR/projects/             an empty transcript dir
  DIR/state/policy.jsonl    hook rows: session start, and before and after the commit call of commit 2
  DIR/state/otel.jsonl      OTel tool_result events of the same session
  DIR/state/deploys.tsv     a start and an end row for commit 2
Commit 1 has no session record. It shows the MISSING labels.
"""
import argparse, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import policy_snapshot  # noqa: E402

SESSION = "demo-session-0001"
TIMES = ("2026-10-20T10:00:00+00:00", "2026-10-20T11:00:00+00:00")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _git(repo, *args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="demo",
             GIT_AUTHOR_EMAIL="demo@example.invalid", GIT_COMMITTER_NAME="demo",
             GIT_COMMITTER_EMAIL="demo@example.invalid", **env)
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True, env=e).stdout.strip()


def _otel(events):
    """One OTel log export line with tool_result events: (time, tool, tool_use_id, HEAD or None)."""
    recs = []
    for i, (ts, tool, tid, rev) in enumerate(events):
        a = {"session.id": SESSION, "event.name": "tool_result", "event.timestamp": ts, "event.sequence": i,
             "tool_name": tool, "tool_use_id": tid, "success": "true"}
        if rev:
            a["vcs.ref.head.revision"] = rev
        recs.append({"attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in a.items()]})
    return json.dumps({"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}})


def build(out):
    out = os.path.realpath(out)
    repo, home, state = (os.path.join(out, n) for n in ("repo", "home", "state"))
    os.makedirs(repo)
    os.makedirs(os.path.join(out, "projects"))
    _write(os.path.join(home, ".claude", "settings.json"), '{"permissions": {"allow": ["Read"]}}\n')
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    shas = []
    for i, t in enumerate(TIMES):
        with open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as f:
            f.write(f"line {i}\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"demo commit {i + 1}", GIT_AUTHOR_DATE=t, GIT_COMMITTER_DATE=t)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    a, b = shas
    files = policy_snapshot.snapshot_files(repo, home, os.path.join(out, "no-managed-settings.json"))
    dg = policy_snapshot.digest(files)
    base = {"session_id": SESSION, "cwd": repo, "files": files, "digest": dg}
    rows = [dict(base, ts="2026-10-20T10:30:00+00:00", event="SessionStart", source="startup", tool_use_id=None),
            dict(base, ts="2026-10-20T10:59:58+00:00", event="PreToolUse", source=None, tool_use_id="toolu_demo_2",
                 head=a, toplevel=repo),
            dict(base, ts="2026-10-20T11:00:02+00:00", event="PostToolUse", source=None, tool_use_id="toolu_demo_2",
                 head=b, toplevel=repo)]
    _write(os.path.join(state, "policy.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))
    _write(os.path.join(state, "otel.jsonl"), _otel([
        ("2026-10-20T10:40:00Z", "Read", "toolu_demo_1", None),
        ("2026-10-20T10:50:00Z", "Bash", "toolu_demo_0", a),
        ("2026-10-20T11:00:02Z", "Bash", "toolu_demo_2", b)]) + "\n")
    _write(os.path.join(state, "deploys.tsv"),
           f"start\t2026-10-20T11:05:00+00:00\t{b[:7]}\tcm-demo-0001\tpod-demo-a\n"
           f"end\t2026-10-20T11:05:00+00:00\t2026-10-20T11:06:00+00:00\tcm-demo-0001\tpod-demo-b\n")
    return {"out": out, "repo": repo, "state": state, "transcripts": os.path.join(out, "projects"), "shas": shas}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="output dir; absent or empty (default: a new temp dir)")
    a = ap.parse_args(argv)
    if a.out:
        if os.path.exists(a.out) and os.listdir(a.out):
            print(f"{a.out} is not empty; nothing written", file=sys.stderr)
            return 1
        os.makedirs(a.out, exist_ok=True)
        out = a.out
    else:
        out = tempfile.mkdtemp(prefix="change-chain-demo-")
    d = build(out)
    chain_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chain.py")
    flags = f"--repo {d['repo']} --transcripts {d['transcripts']} --gh-hosts {os.path.join(d['out'], 'no-gh-hosts.yml')}"
    print(f"demo data: {d['out']}")
    print(f"commits: {d['shas'][0][:12]} (no session record), {d['shas'][1][:12]} (agent commit, deployed)")
    print("run:")
    print(f"  CHANGE_CHAIN_DIR={d['state']} python3 {chain_py} demo {flags}")
    print(f"  CHANGE_CHAIN_DIR={d['state']} python3 {chain_py} coverage {flags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
