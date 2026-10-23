"""change-chain audit: who made each commit, and where the attribution is at risk. Read-only, local.

For each commit on branches and tags (optionally since a date), the audit reports:
  - the commit -> session label (ESTABLISHED / INFERRED / MISSING, from chain.lookup)
  - whether the session link comes from a subagent transcript (isSidechain + agentId)
  - whether the commit message has an agent trailer (Co-Authored-By or "Generated with" naming an agent)
  - the agent actors (session main thread or subagent) that wrote transcript records in a worktree
    of the repo within WINDOW_S seconds of the commit time, and how many worktrees they were in

Output holds short SHAs, UTC times, labels, short session ids and counts. It never holds commit subjects,
command text, transcript text or paths.
"""
from __future__ import annotations
import datetime, json, os, re

import chain

WINDOW_S = 300
AGENTS = r"(?:Claude|Codex|Copilot|Cursor|Gemini|Aider|OpenCode|Cline|Devin|Jules|Amp|Windsurf|Pi)"
TRAILER = re.compile(r"^(?:Co-Authored-By:[^\n]*\b" + AGENTS + r"\b|[^\n]*Generated (?:with|by) \[?"
                     + AGENTS + r")", re.M | re.I)
TRAILER_TEXT = ("Co-Authored-By: <name with Claude, Codex, Copilot, Cursor, Gemini, Aider, OpenCode, Cline, "
                "Devin, Jules, Amp, Windsurf or Pi>; or a line 'Generated with/by <same names>'")


class ActivityIndex:
    """Agent actors in time: (session, agent or None, ts, cwd) for each transcript record with a cwd."""

    def __init__(self, root, since=None):
        self.recs = []
        cut = since.timestamp() - 86400 if since else None
        if not os.path.isdir(root):
            return
        for dirpath, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if cut and os.path.getmtime(path) < cut:
                        continue   # a file not written since the cut holds no record after it
                    f = open(path, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with f:
                    for line in f:
                        if '"cwd"' not in line:
                            continue
                        try:
                            r = json.loads(line)
                            ts = chain._ts_or_none(r.get("timestamp"))
                            sid, cwd = r.get("sessionId"), r.get("cwd")
                        except chain.BAD:
                            continue
                        if not sid or ts is None or not isinstance(cwd, str):
                            continue
                        agent = r.get("agentId") if r.get("isSidechain") else None
                        self.recs.append((ts, sid, agent, cwd))
        self.recs.sort(key=lambda x: x[0])
        self.first = self.recs[0][0] if self.recs else None
        self.last = self.recs[-1][0] if self.recs else None

    def near(self, when, worktrees):
        """Actors with a record in a repo worktree within WINDOW_S of `when`: {(session, agent): worktree}."""
        import bisect
        lo = bisect.bisect_left(self.recs, (when - datetime.timedelta(seconds=WINDOW_S),))
        hi = bisect.bisect_right(self.recs, (when + datetime.timedelta(seconds=WINDOW_S), "￿"))
        out = {}
        for ts, sid, agent, cwd in self.recs[lo:hi]:
            wt = _worktree(cwd, worktrees)
            if wt:
                out.setdefault((sid, agent), set()).add(wt)
        return out


def _worktree(cwd, worktrees):
    c = os.path.realpath(cwd)
    best = [p for p in worktrees if c == p or c.startswith(p + os.sep)]
    return max(best, key=len) if best else None


def trailers(repo, shas):
    """sha -> True if the commit message has an agent trailer."""
    out = {}
    if not shas:
        return out
    log = chain._git(repo, "log", "--no-walk=unsorted", "--format=%H%x00%B%x1e", *shas)
    for chunk in log.split("\x1e"):
        chunk = chunk.strip("\n")
        if "\x00" in chunk:
            h, body = chunk.split("\x00", 1)
            out[h] = bool(TRAILER.search(body))
    return out


def run(repo, since, transcripts_root, otel_path=None, policy_path=None, deploys_path=None, gh_hosts=None):
    dp = chain.default_paths()
    src = dict(otel=chain.otel_tool_results(otel_path or dp["otel"]),
               transcripts=chain.TranscriptIndex(transcripts_root),
               policy=chain.policy_rows(policy_path or dp["policy"]),
               deploys=chain.deploy_rows(deploys_path or dp["deploys"]))
    src["forge"] = chain.forge_state(repo, gh_hosts or dp["gh_hosts"])
    shas = chain.commits_since(repo, since)
    since_t = chain._ts(since + "T00:00:00" if since and len(since) == 10 else since) if since else None
    act = ActivityIndex(transcripts_root, since_t)
    windows = chain.source_windows(src["otel"], src["transcripts"], src["policy"], src["deploys"])
    worktrees = chain.repo_paths(repo)
    trl = trailers(repo, shas)
    res = {"repo": os.path.basename(os.path.realpath(repo)), "since": since, "window_s": WINDOW_S,
           "transcript_window": [act.first.isoformat() if act.first else None,
                                 act.last.isoformat() if act.last else None],
           "worktrees": len(worktrees), "commits": len(shas), "in_window": 0, "out_of_window": 0,
           "linked": {"ESTABLISHED": 0, "INFERRED": 0}, "missing": 0, "subagent": [],
           "trailer": 0, "trailer_no_session": [], "session_no_trailer": [], "multi_candidate": [],
           "multi_actor": [], "multi_worktree": [], "rows": []}
    for sha in shas:
        r = chain.lookup(repo, sha, windows=windows, **src)
        c, s = r["commit"], r["session"]
        ct = chain._ts(c["commit_time"])
        short = c["sha"][:12]
        # The transcript link rules accept a gap of up to 300 s, so the window is widened by that much.
        pad = datetime.timedelta(seconds=chain.TRANSCRIPT_WINDOW_S)
        inwin = act.first is not None and act.first - pad <= ct <= act.last + pad
        res["in_window" if inwin else "out_of_window"] += 1
        has_t = trl.get(c["sha"], False)
        res["trailer"] += has_t
        near = act.near(ct, worktrees)
        wts = set().union(*near.values()) if near else set()
        row = {"sha": short, "time": ct.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
               "label": s["label"], "session": (s["session"] or "-")[:8],
               "actor": "-" if s["label"] == "MISSING" else ("subagent" if s.get("agent") else "main"),
               "trailer": has_t, "actors": len(near), "worktrees": len(wts),
               "candidates": 1 + len(r["other_sessions"]) if s["session"] else 0, "flags": []}
        if s["label"] == "MISSING":
            res["missing"] += 1
        else:
            res["linked"][s["label"]] += 1
        if row["actor"] == "subagent":
            res["subagent"].append(short)
            row["flags"].append("subagent")
        if inwin and has_t and s["label"] == "MISSING":
            res["trailer_no_session"].append(short)
            row["flags"].append("trailer-no-session")
        if s["label"] != "MISSING" and not has_t:
            res["session_no_trailer"].append(short)
            row["flags"].append("session-no-trailer")
        if r["other_sessions"]:
            res["multi_candidate"].append(short)
            row["flags"].append("multi-candidate")
        if len(near) > 1:
            res["multi_actor"].append(short)
            row["flags"].append("multi-actor")
        if len(wts) > 1:
            res["multi_worktree"].append(short)
            row["flags"].append("multi-worktree")
        res["rows"].append(row)
    for k in ("subagent", "trailer_no_session", "session_no_trailer", "multi_candidate", "multi_actor",
              "multi_worktree"):
        res[k].sort()
    res["rows"].sort(key=lambda x: x["time"])
    return res


def _shas(xs, limit):
    more = f" (+{len(xs) - limit} more)" if len(xs) > limit else ""
    return (", ".join(xs[:limit]) + more) if xs else "-"


def render(r, limit=10):
    n = r["commits"]
    pct = lambda k: f"{100 * k / n:5.1f}%" if n else "    -"
    tw = r["transcript_window"]
    out = ["change-chain audit | read-only, local; short SHAs, times, labels and counts only "
           "(no subjects, commands or paths)",
           f"repo: {r['repo']}  worktrees: {r['worktrees']}  since: {r['since'] or '-'}  "
           f"transcript window: {tw[0] or '-'} to {tw[1] or '-'}",
           "",
           "Who made the commits                                   commits   share",
           f"  commits (branches and tags)                          {n:>7}",
           f"  inside the transcript window                         {r['in_window']:>7}  {pct(r['in_window'])}",
           f"  session link ESTABLISHED                             {r['linked']['ESTABLISHED']:>7}  "
           f"{pct(r['linked']['ESTABLISHED'])}",
           f"  session link INFERRED                                {r['linked']['INFERRED']:>7}  "
           f"{pct(r['linked']['INFERRED'])}",
           f"  no session link (MISSING)                            {r['missing']:>7}  {pct(r['missing'])}",
           f"  made in a subagent (link from a subagent transcript) {len(r['subagent']):>7}  "
           f"{pct(len(r['subagent']))}",
           f"  with an agent trailer                                {r['trailer']:>7}  {pct(r['trailer'])}",
           "",
           "Attribution checks                                     commits   short SHAs",
           ]
    checks = [("agent trailer, no session record (in window)", "trailer_no_session"),
              ("agent session, no agent trailer (reads as human)", "session_no_trailer"),
              ("more than one candidate session", "multi_candidate"),
              (f"more than one agent actor in the repo within {r['window_s']} s", "multi_actor"),
              ("  of those, actors in more than one worktree", "multi_worktree")]
    for name, k in checks:
        out.append(f"  {name:<52} {len(r[k]):>5}   {_shas(r[k], limit)}")
    flagged = [x for x in r["rows"] if x["flags"]]
    out += ["", f"Flagged commits ({len(flagged)}; newest {min(limit, len(flagged))} shown)",
            "  sha           time (UTC)            label        session   actor     trailer actors wt  flags"]
    for x in flagged[-limit:]:
        out.append(f"  {x['sha']:<13} {x['time']:<21} {x['label']:<12} {x['session']:<9} {x['actor']:<9} "
                   f"{'yes' if x['trailer'] else 'no':<7} {x['actors']:>6} {x['worktrees']:>3}  "
                   f"{','.join(x['flags'])}")
    out += ["",
            "What to do",
            "  trailer, no session record: the agent's log for that commit is gone or the commit came from "
            "another agent; check it by hand.",
            "  session, no trailer: an agent session made the commit but the message reads as human; "
            "line-attribution tools may count it as human.",
            "  more than one actor or worktree: a hook or tracker that links by time or by repo can link the "
            "commit to the wrong session; read the lookup output for these SHAs (chain.py lookup <sha>).",
            "",
            "Limits",
            "  1. Session links come from Claude Code transcripts (and OTel or policy rows if present); other "
            "agents' logs are not read.",
            f"  2. Agent trailer patterns: {TRAILER_TEXT}.",
            "  3. A commit outside the transcript window (first to last record, +-300 s) is no data, not a "
            "gap; it is not counted as 'trailer, no session record'.",
            f"  4. Actors = distinct (session, subagent) pairs with a transcript record whose cwd is in a "
            f"worktree of this repo, {r['window_s']} s before or after the commit time. A session that only "
            "reads the repo from another cwd is not seen.",
            "  5. Transcripts are unsigned local files; a deleted transcript makes a commit MISSING.",
            "Nothing was written."]
    return "\n".join(out)
