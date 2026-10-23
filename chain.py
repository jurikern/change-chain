#!/usr/bin/env python3
"""Change-chain v0, reverse lookup — 2026-10-20.

Commit SHA in; agent session and policy version out. Each link has one label:
  ESTABLISHED  a machine record shows the link directly
  INFERRED     the link rests on unsigned text, a time window or file mtimes
  MISSING      no record links the two

Sources, all local, read-only:
  git                     the commit, its parents and times
  OTel log export         tool_result events carry vcs.ref.head.revision (HEAD after the tool call)
  Claude Code transcripts commit output "[branch abc1234] subject", or a `git commit` call with the subject
  policy capture          policy_snapshot.py rows: settings digest at session start, and before and after
                          each `git commit` call with HEAD and the repo toplevel (source "hook-head")

Output holds IDs, hashes, times, labels and the commit subject from git. It never prints commands or
transcript text.

Run:
    python3 chain.py lookup <sha> [--repo PATH] [--json]
    python3 chain.py export [--repo PATH] [--since DATE] > chain.jsonl   # one line per commit
    python3 chain.py coverage [--repo PATH] [--since DATE]               # label counts
    python3 chain.py audit [--repo PATH] [--since DATE] [--json]         # subagent, trailer and concurrency checks

Default inputs: <state>/otel.jsonl, <state>/policy.jsonl, <state>/deploys.tsv and ~/.claude/projects.
<state> is $CHANGE_CHAIN_DIR, else $XDG_STATE_HOME/change-chain, else ~/.local/state/change-chain.
The flags --otel, --policy, --deploys, --transcripts and --gh-hosts override each path.
"""
from __future__ import annotations
import argparse, datetime, json, os, re, subprocess, sys

import policy_snapshot

state_dir = policy_snapshot.state_dir


def default_paths():
    """Default input paths. Read at call time, so $CHANGE_CHAIN_DIR, $XDG_STATE_HOME and $HOME apply."""
    d = state_dir()
    home = os.path.expanduser("~")
    return {"otel": os.path.join(d, "otel.jsonl"), "policy": os.path.join(d, "policy.jsonl"),
            "deploys": os.path.join(d, "deploys.tsv"), "transcripts": os.path.join(home, ".claude", "projects"),
            "gh_hosts": os.path.join(home, ".config", "gh", "hosts.yml")}


# A HEAD move counts as the commit only if the tool call ends near the commit time.
COMMIT_WINDOW_S = 120
# A `git commit` call in a transcript matches only inside this window.
TRANSCRIPT_WINDOW_S = 300
COMMIT_OUT = re.compile(r"^\[(?:detached HEAD|[^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] ([^\n]*)", re.M)
COMMIT_CALL = policy_snapshot.COMMIT_CALL
BAD = (AttributeError, KeyError, TypeError, ValueError, IndexError)
LINKS = ("session", "policy", "deployment", "pr", "reviews", "checks")
RANK = {"ESTABLISHED": 0, "INFERRED": 1, "MISSING": 2}


def _ts(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return datetime.datetime.fromtimestamp(s, datetime.timezone.utc)
    t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    # A time with no zone is read as UTC.
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _ts_req(s):
    """A required time in an input row. None is a bad value, so the caller skips the row."""
    t = _ts(s)
    if t is None:
        raise ValueError("no time")
    return t


def _ts_or_none(s):
    try:
        return _ts(s)
    except BAD:
        return None


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


# --- git -------------------------------------------------------------------------------

def commit_info(repo, sha):
    full = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").strip()
    h, parents, at, ct, subject = _git(repo, "log", "-1", "--format=%H%x00%P%x00%aI%x00%cI%x00%s", full).split("\x00")
    return {"sha": h, "parents": parents.split(), "author_time": at, "commit_time": ct.strip(),
            "subject": subject.rstrip("\n")}


def repo_paths(repo):
    """Toplevel paths of every live worktree of this repo."""
    out = _git(repo, "worktree", "list", "--porcelain")
    paths = {os.path.realpath(l[9:]) for l in out.splitlines() if l.startswith("worktree ")}
    return paths or {os.path.realpath(_git(repo, "rev-parse", "--show-toplevel").strip())}


def _in_repo(cwd, paths):
    if not cwd:
        return False
    c = os.path.realpath(cwd)
    return any(c == p or c.startswith(p + os.sep) for p in paths)


# --- otel ------------------------------------------------------------------------------

def otel_tool_results(path):
    """tool_result events with a HEAD revision, grouped by session and sorted."""
    by_session = {}
    if not os.path.exists(path):
        return by_session
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            try:
                records = [r for rl in rec["payload"]["resourceLogs"] for sl in rl["scopeLogs"]
                           for r in sl["logRecords"]]
            except BAD:
                continue
            for r in records:
                try:
                    a = {x["key"]: next(iter(x["value"].values()), None) for x in r.get("attributes", [])}
                    t = _ts_req(a["event.timestamp"])
                    seq = int(float(a.get("event.sequence") or 0))
                except BAD:
                    continue
                if a.get("event.name") != "tool_result" or not a.get("vcs.ref.head.revision") \
                        or not a.get("session.id"):
                    continue
                by_session.setdefault(a["session.id"], []).append({
                    "ts": a["event.timestamp"], "t": t, "seq": seq, "rev": a["vcs.ref.head.revision"],
                    "tool": a.get("tool_name"), "tool_use_id": a.get("tool_use_id")})
    for evs in by_session.values():
        evs.sort(key=lambda e: (e["t"], e["seq"]))
    return by_session


def otel_activity(path):
    """Every tool_result event per session (time, tool, HEAD revision if any), sorted. No command text, no IDs."""
    by_session = {}
    if not os.path.exists(path):
        return by_session
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                records = [r for rl in rec["payload"]["resourceLogs"] for sl in rl["scopeLogs"]
                           for r in sl["logRecords"]]
            except BAD:
                continue
            for r in records:
                try:
                    a = {x["key"]: next(iter(x["value"].values()), None) for x in r.get("attributes", [])}
                    t = _ts_req(a["event.timestamp"])
                    seq = int(float(a.get("event.sequence") or 0))
                except BAD:
                    continue
                if a.get("event.name") != "tool_result" or not a.get("session.id"):
                    continue
                by_session.setdefault(a["session.id"], []).append(
                    {"t": t, "seq": seq, "tool": a.get("tool_name") or "unknown", "rev": a.get("vcs.ref.head.revision")})
    for evs in by_session.values():
        evs.sort(key=lambda e: (e["t"], e["seq"]))
    return by_session


def outside_git(activity):
    """Tool calls per session with no commit seen in the call. Always INFERRED.

    A call shows a commit if it carries a HEAD revision that differs from the last revision seen in the session.
    OTel holds no command text: a quiet `git commit -q` shows no revision and counts as outside git. The count
    is an upper bound for work outside git. It is not a record of what the calls did.
    """
    out = {}
    for sid, evs in activity.items():
        last, seen, by_tool = None, 0, {}
        for e in evs:
            by_tool[e["tool"]] = by_tool.get(e["tool"], 0) + 1
            if e["rev"] and e["rev"] != last:
                seen += 1
            if e["rev"]:
                last = e["rev"]
        out[sid] = {"tool_calls": len(evs), "commit_seen": seen, "outside_git": len(evs) - seen,
                    "by_tool": by_tool, "first": evs[0]["t"].isoformat(), "last": evs[-1]["t"].isoformat(),
                    "label": "INFERRED"}
    return out


def otel_links(commit, by_session):
    """A session links to the commit if HEAD moved parent -> commit between two of its tool calls.

    ESTABLISHED only if the moving call is Bash and ends 0..COMMIT_WINDOW_S after the commit time:
    the commit then happened inside that call. Otherwise HEAD may have moved between calls (a human
    commit, a pull), so the link is INFERRED.
    """
    links, seen = [], []
    ct = _ts(commit["commit_time"])
    for sid, evs in by_session.items():
        moved = False
        for prev, ev in zip([None] + evs[:-1], evs):
            if ev["rev"] != commit["sha"]:
                continue
            if prev is not None and prev["rev"] in commit["parents"]:
                gap = (ev["t"] - ct).total_seconds()
                ok = ev["tool"] == "Bash" and 0 <= gap <= COMMIT_WINDOW_S
                links.append({"session": sid, "label": "ESTABLISHED" if ok else "INFERRED", "source": "otel",
                              "evidence": f"HEAD moved {prev['rev'][:12]} -> {commit['sha'][:12]} by the end of "
                                          f"{ev['tool']} call {ev['tool_use_id']}; call ended {gap:.0f} s "
                                          "after the commit time",
                              "ts": ev["ts"], "tool_use_id": ev["tool_use_id"], "cwd": None})
                moved = True
                break
        if not moved and any(e["rev"] == commit["sha"] for e in evs):
            seen.append(sid)
    return links, seen


# --- transcripts -----------------------------------------------------------------------

def hook_links(commit, rows, paths):
    """A session links to the commit if its hook rows show HEAD move parent -> commit inside one call.

    ESTABLISHED only if the PreToolUse row of the call has HEAD = a parent (none for a root commit), the
    PostToolUse row has HEAD = the commit, both in a worktree of the repo, and the call ended
    0..COMMIT_WINDOW_S after the commit time. A Post row with HEAD = the commit but no such Pre row
    (amend, several commits in one call, no Pre row) or outside the window is INFERRED.
    """
    links = []
    ct = _ts(commit["commit_time"])
    for sid, snaps in rows.items():
        pre = {s.get("tool_use_id"): s for s in snaps if s.get("event") == "PreToolUse"}
        for post in snaps:
            if post.get("event") != "PostToolUse" or post.get("head") != commit["sha"] \
                    or post.get("toplevel") not in paths:
                continue
            before = pre.get(post.get("tool_use_id"))
            moved = before is not None and before.get("toplevel") == post["toplevel"] and (
                before.get("head") in commit["parents"] if commit["parents"] else before.get("head") is None)
            gap = (_ts(post["ts"]) - ct).total_seconds()
            ok = moved and 0 <= gap <= COMMIT_WINDOW_S
            start = (before.get("head") or "none")[:12] if before else "unknown"
            links.append({"session": sid, "label": "ESTABLISHED" if ok else "INFERRED", "source": "hook-head",
                          "evidence": f"hook HEAD {start} -> {commit['sha'][:12]} across Bash call "
                                      f"{post.get('tool_use_id')}; call ended {gap:.0f} s after the commit time"
                                      + ("" if moved else "; HEAD before the call is not a parent"),
                          "ts": post["ts"], "tool_use_id": post.get("tool_use_id"), "cwd": post["toplevel"]})
    return links


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


class TranscriptIndex:
    """One pass over all transcripts. Keeps commit outputs and `git commit` calls, no other text."""

    def __init__(self, root):
        self.outputs = []   # (session, ts, cwd, short_sha, subject, agent)
        self.calls = []     # (session, ts, cwd, tool_use_id, command, agent)
        if not os.path.isdir(root):
            return
        for dirpath, _, files in os.walk(root):
            for name in files:
                if name.endswith(".jsonl"):
                    self._scan(os.path.join(dirpath, name))

    def _scan(self, path):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with f:
            for line in f:
                if "commit" not in line and "] " not in line:
                    continue
                try:
                    self._record(json.loads(line))
                except BAD:
                    continue

    def _record(self, rec):
        sid, ts, cwd = rec.get("sessionId"), _ts_or_none(rec.get("timestamp")), rec.get("cwd")
        if not sid or ts is None:
            return
        # A subagent transcript carries the parent's sessionId plus its own agentId.
        agent = str(rec.get("agentId") or "sidechain") if rec.get("isSidechain") else None
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash":
                cmd = (c.get("input") or {}).get("command")
                if isinstance(cmd, str) and COMMIT_CALL.search(cmd):
                    self.calls.append((sid, ts, cwd, c.get("id"), cmd, agent))
            elif c.get("type") == "tool_result":
                for s in _strings(c.get("content")):
                    for m in COMMIT_OUT.finditer(s):
                        self.outputs.append((sid, ts, cwd, m.group(1), m.group(2).strip(), agent))

    def links(self, commit, paths):
        links, ct = [], _ts(commit["commit_time"])
        subj = commit["subject"]
        # The subject must be the whole message line: after a quote or a line start, before a quote or a line end.
        subj_re = re.compile(r"(?:^|['\"])" + re.escape(subj) + r"(?:$|['\"])", re.M) if subj else None
        for sid, ts, cwd, short, subject, agent in self.outputs:
            gap = abs((ts - ct).total_seconds())
            if commit["sha"].startswith(short) and subject == subj and gap <= TRANSCRIPT_WINDOW_S:
                links.append({"session": sid, "label": "INFERRED", "source": "transcript-output",
                              "evidence": f"commit output with SHA {short} and the same subject, {gap:.0f} s "
                                          "from commit time (unsigned text)",
                              "ts": ts.isoformat(), "tool_use_id": None, "cwd": cwd, "agent": agent})
        for sid, ts, cwd, tool_use_id, cmd, agent in self.calls:
            if subj_re is None or not subj_re.search(cmd):
                continue
            gap = abs((ts - ct).total_seconds())
            if gap > TRANSCRIPT_WINDOW_S:
                continue
            if not (_in_repo(cwd, paths) or _names_repo(cmd, paths)):
                continue
            links.append({"session": sid, "label": "INFERRED", "source": "transcript-call",
                          "evidence": f"git commit call {tool_use_id} with the subject in a repo worktree; "
                                      f"{gap:.0f} s from commit time",
                          "ts": ts.isoformat(), "tool_use_id": tool_use_id, "cwd": cwd, "agent": agent})
        return links


def _names_repo(cmd, paths):
    """True if the command runs git in, or cd's into, one of the repo paths (not a path with the same prefix)."""
    return any(re.search(r"(?:-C|\bcd)\s+['\"]?" + re.escape(p) + r"(?:/[^\s'\"]*)?['\"]?(?=\s|$|[;&|)])", cmd)
               for p in paths)


# --- policy ----------------------------------------------------------------------------

def policy_rows(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                r["t"] = _ts_req(r["ts"])
                if r.get("session_id") and r.get("digest"):
                    rows.setdefault(r["session_id"], []).append(r)
            except BAD:
                continue
    for rs in rows.values():
        rs.sort(key=lambda r: r["t"])
    return rows


def _unchanged_since(files, when):
    """True if every present settings file has an mtime at or before `when`."""
    return all(f["mtime"] is None or _ts(f["mtime"]) <= when for f in files)


def policy_link(link, commit, rows, cwd, home=None, managed=None):
    """Policy version for the session link.

    ESTABLISHED only from a PreToolUse snapshot of the same tool call (tool_use_id). A snapshot at
    session start, or settings mtimes read now, are INFERRED: settings can change during a session,
    and a deleted or restored file hides its history.
    """
    ct = _ts(commit["commit_time"])
    snaps = rows.get(link["session"], [])
    if link.get("tool_use_id"):
        for s in snaps:
            if s.get("event") == "PreToolUse" and s.get("tool_use_id") == link["tool_use_id"]:
                return {"digest": s["digest"], "label": "ESTABLISHED", "source": "policy-capture",
                        "evidence": f"snapshot at the commit call {s['tool_use_id']} ({s['ts']})"}
    before = [s for s in snaps if _ts_or_none(s.get("ts")) is not None and _ts(s["ts"]) <= ct]
    if before:
        s = before[-1]
        return {"digest": s["digest"], "label": "INFERRED", "source": "policy-capture",
                "evidence": f"last snapshot before the commit at {s['ts']} ({s.get('event') or s.get('source')}); "
                            "settings may have changed between snapshot and commit"}
    current = policy_snapshot.snapshot_files(cwd, home, managed)
    if _unchanged_since(current, ct):
        return {"digest": policy_snapshot.digest(current), "label": "INFERRED", "source": "settings-mtime",
                "evidence": "no snapshot for the session; every settings file now present is older than the "
                            "commit (mtime evidence; a deleted or restored file is not seen)"}
    return {"digest": None, "label": "MISSING", "source": None,
            "evidence": "no snapshot for the session, and a settings file changed after the commit"}


# --- deployments -----------------------------------------------------------------------

MIN_DEPLOY_SHA = 7


def deploy_rows(path):
    """Deploy log rows as deployments, sorted by start time. Bad rows are skipped.

    start: time, short commit, ConfigMap hash, pod.  end: start time, end time, ConfigMap hash, pod.
    check: time, result text; the user agent `datapce-deploy-check/<sha>` names the commit.
    """
    starts, ends, checks = [], [], []
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            try:
                if p[0] == "start" and len(p) >= 5 and re.fullmatch(r"[0-9a-f]{7,40}", p[2]):
                    starts.append({"t": _ts(p[1]), "ts": p[1], "sha": p[2], "configmap": p[3], "pod": p[4]})
                elif p[0] == "end" and len(p) >= 5:
                    ends.append({"t": _ts(p[2]), "configmap": p[3], "pod": p[4], "start_t": _ts(p[1])})
                elif p[0] == "check" and len(p) >= 3:
                    m = re.search(r"datapce-deploy-check/([0-9a-f]{7,40})", p[2])
                    checks.append({"t": _ts(p[1]), "sha": m.group(1) if m else None, "ok": p[2].startswith("ok")})
            except BAD:
                continue
    deps = []
    ends.sort(key=lambda e: (e["start_t"], e["t"]))  # the earliest end that can follow a start, not file order
    for st in sorted(starts, key=lambda d: d["t"]):
        end = next((e for e in ends if e["configmap"] == st["configmap"] and e["start_t"] >= st["t"]
                    and e["t"] >= st["t"]), None)
        chk = next((c for c in checks if c["sha"] == st["sha"] and c["t"] >= st["t"]), None)
        deps.append(dict(st, end=end, check=chk))
    orphans = [c for c in checks if not any(d["sha"] == c["sha"] for d in deps)]
    return deps + [{"orphan_check": c} for c in orphans]


def deployment_link(commit, deps):
    """commit -> deployment.

    ESTABLISHED: a start row names the commit (7 or more hex chars, a prefix of the SHA) and an end row has the
    same ConfigMap hash and does not end before the start. INFERRED: a start row without a matching end row
    (the latest start of the commit is a later redeploy), or only a check row names the commit. MISSING: no row.
    The deploy log is an unsigned local file, as the hook capture is.
    """
    full = commit["sha"]
    mine = [d for d in deps if d.get("sha") and len(d["sha"]) >= MIN_DEPLOY_SHA and full.startswith(d["sha"])]
    if not mine:
        orphan = [d["orphan_check"] for d in deps if "orphan_check" in d
                  and len(d["orphan_check"]["sha"] or "") >= MIN_DEPLOY_SHA and full.startswith(d["orphan_check"]["sha"])]
        if orphan:
            return {"label": "INFERRED", "configmap": None, "pod": None, "time": orphan[-1]["t"].isoformat(),
                    "checked": orphan[-1]["ok"], "count": 0,
                    "evidence": "a check row names the commit; no start row"}
        return {"label": "MISSING", "configmap": None, "pod": None, "time": None, "checked": False, "count": 0,
                "evidence": "no deploy log row names this commit"}
    d = mine[-1]
    checked = bool(d["check"] and d["check"]["ok"])
    if d["end"]:
        return {"label": "ESTABLISHED", "configmap": d["configmap"], "pod": d["end"]["pod"], "time": d["ts"],
                "checked": checked, "count": len(mine),
                "evidence": f"deploy log: start at {d['ts']}, end row with ConfigMap {d['configmap'][:12]}"
                            + ("; check ok" if checked else "; no check row")}
    return {"label": "INFERRED", "configmap": d["configmap"], "pod": d["pod"], "time": d["ts"],
            "checked": checked, "count": len(mine),
            "evidence": f"deploy log: start at {d['ts']}, no end row with the same ConfigMap hash"}


# --- completeness ----------------------------------------------------------------------

def _window(times):
    times = sorted(t for t in times if t is not None)
    return {"rows": len(times), "first": times[0].isoformat() if times else None,
            "last": times[-1].isoformat() if times else None}


def source_windows(otel, transcripts, policy, deploys):
    """Rows and first/last time of each source. A commit outside a window cannot have a link from that source."""
    return {
        "otel": _window([e.get("t") or _ts_or_none(e.get("ts")) for evs in otel.values() for e in evs]),
        "transcripts": _window([x[1] for x in transcripts.outputs] + [x[1] for x in transcripts.calls]),
        "policy": _window([r.get("t") or _ts_or_none(r.get("ts")) for rs in policy.values() for r in rs]),
        "deploys": _window([d["t"] if "t" in d else d["orphan_check"]["t"] for d in deploys]),
    }


def _in_window(w, when):
    return w["rows"] > 0 and _ts(w["first"]) <= when <= _ts(w["last"])


def completeness(windows, commit):
    ct = _ts(commit["commit_time"])
    return {k: dict(w, commit_in_window=_in_window(w, ct)) for k, w in windows.items()}


# --- forge (pull requests, reviews, checks) --------------------------------------------

def forge_state(repo, gh_hosts=None):
    """Why PR, review and check links are MISSING. Local reads only: remote names, and whether a gh login file exists.

    The remote URL and the gh login file content are never read into output. `gh` is never run.
    """
    gh_hosts = default_paths()["gh_hosts"] if gh_hosts is None else gh_hosts
    if not _git(repo, "remote").split():
        return "no remote"
    if not os.path.exists(gh_hosts):
        return "gh not authenticated"
    return "gh not queried"


FORGE_LINKS = (("pr", "commit -> PR"), ("reviews", "PR -> reviews"), ("checks", "PR -> checks"))


def forge_links(reason):
    return {k: {"label": "MISSING", "reason": reason, "evidence": f"no PR record: {reason}"} for k, _ in FORGE_LINKS}


# --- chain -----------------------------------------------------------------------------

def lookup(repo, sha, otel=None, transcripts=None, policy=None, home=None, managed=None, deploys=None, windows=None, gh_hosts=None, forge=None, outside=None):
    dp = default_paths()
    otel = otel if otel is not None else otel_tool_results(dp["otel"])
    transcripts = transcripts if transcripts is not None else TranscriptIndex(dp["transcripts"])
    policy = policy if policy is not None else policy_rows(dp["policy"])
    deploys = deploys if deploys is not None else deploy_rows(dp["deploys"])
    commit = commit_info(repo, sha)
    paths = repo_paths(repo)
    o_links, seen = otel_links(commit, otel)
    candidates = hook_links(commit, policy, paths) + o_links + transcripts.links(commit, paths)
    gaps, link_gaps = [], []   # `gaps` keeps its v0 meaning; links added in v1 go to `link_gaps`
    if not candidates:
        session = {"session": None, "label": "MISSING", "source": None,
                   "evidence": "no OTel HEAD move and no transcript record for this commit"}
        pol = {"digest": None, "label": "MISSING", "source": None, "evidence": "no session"}
        gaps.append("commit -> session")
    else:
        ct = _ts(commit["commit_time"])
        candidates.sort(key=lambda l: (RANK[l["label"]], abs((_ts(l["ts"]) - ct).total_seconds())))
        session = dict(candidates[0])
        top = {c["session"] for c in candidates if c["label"] == "ESTABLISHED"}
        if len(top) > 1:
            session["label"] = "INFERRED"
            session["evidence"] += f"; {len(top)} sessions show the same HEAD move"
            gaps.append("commit -> session: conflicting sessions")
        snap_cwd = next((s["cwd"] for s in policy.get(session["session"], []) if s.get("cwd")), None)
        cwd = snap_cwd or session.get("cwd") or _git(repo, "rev-parse", "--show-toplevel").strip()
        pol = policy_link(session, commit, policy, cwd, home, managed)
    if pol["label"] == "MISSING" and session["label"] != "MISSING":
        gaps.append("session -> policy")
    dep = deployment_link(commit, deploys)
    if dep["label"] == "MISSING":
        link_gaps.append("commit -> deployment")
    forge = forge if forge is not None else forge_state(repo, gh_hosts)
    fl = forge_links(forge)
    link_gaps += [f"{name}: {forge}" for k, name in FORGE_LINKS]
    others = sorted({c["session"] for c in candidates} - {session["session"]})
    return {"commit": commit, "session": session, "policy": pol, "deployment": dep, **fl, "link_gaps": link_gaps, "forge": forge,
            "outside_git": (outside or {}).get(session["session"]),
            "completeness": completeness(windows or source_windows(otel, transcripts, policy, deploys), commit),
            "other_sessions": others, "sessions_on_top": sorted(seen), "gaps": gaps}


def render(r):
    c, s, p = r["commit"], r["session"], r["policy"]
    rows = [("commit", c["sha"][:12], "ESTABLISHED", f"git: {c['commit_time']} \"{c['subject'][:60]}\""),
            ("session", s["session"] or "-", s["label"], s["evidence"]),
            ("policy", (p["digest"] or "-")[:16], p["label"], p["evidence"])]
    d = r["deployment"]
    o = r.get("outside_git")
    if o:
        rows.append(("outside", "-", o["label"], f"session made {o['tool_calls']} tool calls; {o['outside_git']} "
                     f"showed no commit (OTel; {', '.join(f'{k} {v}' for k, v in sorted(o['by_tool'].items()))})"))
    rows.append(("deploy", (d["configmap"] or "-")[:16], d["label"], d["evidence"]))
    for k, name in FORGE_LINKS:
        rows.append((k, "-", r[k]["label"], r[k]["evidence"]))
    w = max(len(x[1]) for x in rows)
    out = [f"{k:<8} {v:<{w}}  {lab:<11}  {ev}" for k, v, lab, ev in rows]
    if r["other_sessions"]:
        out.append(f"other candidate sessions: {', '.join(r['other_sessions'])}")
    if r["gaps"]:
        out.append(f"gaps: {', '.join(r['gaps'])}")
    if r.get("completeness"):
        held = [k for k, w in r["completeness"].items() if w["commit_in_window"]]
        out.append("source windows holding the commit time: " + (", ".join(held) or "none"))
    if r.get("link_gaps"):
        out.append(f"link gaps: {', '.join(r['link_gaps'])}")
    return "\n".join(out)


def demo(repo, src, sha=None):
    """Print the full chain and the gaps of one deployed commit: the latest ESTABLISHED deployment found in the repo."""
    if sha is None:
        for d in sorted((d for d in src["deploys"] if d.get("sha") and d["end"]), key=lambda d: d["t"], reverse=True):
            try:
                sha = commit_info(repo, d["sha"])["sha"]
                break
            except (subprocess.CalledProcessError, ValueError):
                continue
    if sha is None:
        print("no deployed commit found in this repo and deploy log")
        return 1
    r = lookup(repo, sha, **src)
    print(f"demo commit: {r['commit']['sha'][:12]}")
    print(render(r))
    gaps = r["gaps"] + r["link_gaps"]
    print(f"gap count: {len(gaps)}")
    return 0


def commits_since(repo, since):
    # Branches and tags only: --all would also count stash commits.
    # A bare date means local midnight: git alone reads "2026-10-21" as that day at the current clock time.
    if since and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        since += "T00:00:00"
    args = ["rev-list", "--branches", "--tags"] + ([f"--since={since}"] if since else [])
    return _git(repo, *args).split()


def main(argv=None):
    dp = default_paths()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lookup", "export", "coverage", "demo", "audit"):
        p = sub.add_parser(name)
        if name == "lookup":
            p.add_argument("sha")
            p.add_argument("--json", action="store_true")
        elif name == "audit":
            p.add_argument("--since")
            p.add_argument("--json", action="store_true")
            p.add_argument("--hash-repos", action="store_true", help="show the repo as a short hash")
            p.add_argument("--top", type=int, default=None, help="SHAs and rows per list (default all)")
        elif name == "demo":
            p.add_argument("--sha")
        else:
            p.add_argument("--since")
        p.add_argument("--repo", default=".")
        p.add_argument("--otel", default=dp["otel"], help="OTel log export, JSON lines")
        p.add_argument("--policy", default=dp["policy"], help="policy_snapshot.py capture")
        p.add_argument("--deploys", default=dp["deploys"], help="deploy log, TSV (optional)")
        p.add_argument("--transcripts", default=dp["transcripts"], help="Claude Code transcript dir")
        p.add_argument("--gh-hosts", default=dp["gh_hosts"], help="gh login file; only its presence is read")
    a = ap.parse_args(argv)
    if a.cmd == "audit":
        import audit
        try:
            r = audit.run(a.repo, a.since, a.transcripts, a.otel, a.policy, a.deploys, a.gh_hosts, a.hash_repos)
        except (subprocess.CalledProcessError, OSError) as e:
            # No git stderr here: it can hold the repo path.
            print(f"chain.py: error: git failed ({type(e).__name__}); is --repo a git repo?", file=sys.stderr)
            return 2
        print(json.dumps(r, indent=1) if a.json else audit.render(r, a.top))
        return 0
    src = dict(otel=otel_tool_results(a.otel), transcripts=TranscriptIndex(a.transcripts),
               policy=policy_rows(a.policy), deploys=deploy_rows(a.deploys))
    try:
        return _run(a, src)
    except subprocess.CalledProcessError as e:
        # A bad SHA or a path that is not a repo: one error line, exit 2 (as argparse), no traceback.
        why = (e.stderr or "").strip().splitlines()
        print(f"chain.py: error: git {e.cmd[3] if len(e.cmd) > 3 else ''} failed: "
              f"{why[0] if why else 'exit ' + str(e.returncode)}", file=sys.stderr)
        return 2


def _run(a, src):
    src["forge"] = forge_state(a.repo, a.gh_hosts)
    src["outside"] = outside_git(otel_activity(a.otel))
    if a.cmd == "lookup":
        r = lookup(a.repo, a.sha, **src)
        print(json.dumps(r, indent=2) if a.json else render(r))
        return 0
    if a.cmd == "demo":
        return demo(a.repo, src, a.sha)
    windows = source_windows(src["otel"], src["transcripts"], src["policy"], src["deploys"])
    results = [lookup(a.repo, sha, windows=windows, **src) for sha in commits_since(a.repo, a.since)]
    if a.cmd == "export":
        for r in results:
            print(json.dumps(r, separators=(",", ":")))
        return 0
    counts, inwin = {}, {}
    for r in results:
        for k, w in r["completeness"].items():
            inwin[k] = inwin.get(k, 0) + w["commit_in_window"]
        for link in LINKS:
            key = (link, r[link]["label"])
            counts[key] = counts.get(key, 0) + 1
    n = len(results)
    merges = sum(len(r["commit"]["parents"]) > 1 for r in results)
    times = sorted(_ts(r["commit"]["commit_time"]) for r in results)
    print(f"population: refs=branches+tags commits={n} merges={merges} repo={os.path.basename(os.path.realpath(a.repo))}")
    print(f"period: since={a.since or '-'} first={times[0].isoformat() if times else '-'} "
          f"last={times[-1].isoformat() if times else '-'}")
    for k, w in windows.items():
        print(f"source {k:<12} rows={w['rows']} first={w['first'] or '-'} last={w['last'] or '-'}")
    print("in window: " + ", ".join(f"{k} {inwin.get(k, 0)} of {n}" for k in windows))
    print(f"forge: {src['forge']}")
    tot = lambda k: sum(v[k] for v in src["outside"].values())
    print(f"outside git (INFERRED): sessions={len(src['outside'])} tool_calls={tot('tool_calls')} "
          f"commit_seen={tot('commit_seen')} outside_git={tot('outside_git')}")
    print(f"commits: {n}")
    for link in LINKS:
        for lab in RANK:
            k = counts.get((link, lab), 0)
            print(f"{link:<10} {lab:<11} {k:>5}  {100 * k / n if n else 0:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
