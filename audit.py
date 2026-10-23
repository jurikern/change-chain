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
import bisect, datetime, hashlib, json, os, re

import chain

WINDOW_S = 300
# An agent trailer names a coding agent by its e-mail domain (Co-Authored-By), or by a fixed
# "Generated with/by <agent>" line, or by a Generated-by / Assisted-by trailer. A person called
# Claude or Devin with a private address is not an agent.
AGENT_DOMAINS = ("anthropic.com", "openai.com", "cursor.com", "cursor.sh", "aider.chat", "cognition.ai",
                 "devin.ai", "ampcode.com", "sourcegraph.com", "windsurf.com", "codeium.com", "opencode.ai",
                 "cline.bot", "google.com")
AGENT_NAMES = (r"(?:Claude(?: Code)?|Codex|(?:GitHub )?Copilot|Cursor(?: Agent)?|Gemini(?: CLI| Code Assist)?"
               r"|Aider|OpenCode|Cline|Devin|Jules|Amp|Windsurf)")
_COAUTHOR = re.compile(r"^[ \t]*Co-authored-by:[ \t]*([^<\n]*)<([^>\n]*)>", re.M | re.I)
_GENERATED = re.compile(r"^[ \t]*(?:\W{1,3}[ \t]*)?Generated (?:with|by) \[?" + AGENT_NAMES + r"\b", re.M)
_TRAILER_KEY = re.compile(r"^[ \t]*(?:Generated|Assisted|AI-assisted)-by:[ \t]*" + AGENT_NAMES + r"\b",
                          re.M | re.I)
TRAILER_TEXT = ("Co-Authored-By with an e-mail at %s, or a Copilot or [bot] address; a line 'Generated "
                "with/by <agent>'; a Generated-by or Assisted-by trailer naming an agent (Claude, Codex, "
                "Copilot, Cursor, Gemini, Aider, OpenCode, Cline, Devin, Jules, Amp, Windsurf)"
                % ", ".join(AGENT_DOMAINS))
# A Bash command that runs git in, or cd's into, an absolute or ~ path.
_CMD_PATH = re.compile(r"""(?:-C|\bcd)\s+(['"]?)((?:/|~/)[^'"\s;&|)]*)\1""")


def is_agent_trailer(body):
    for _, mail in _COAUTHOR.findall(body):
        m = mail.strip().lower()
        dom = m.rsplit("@", 1)[-1]
        if any(dom == d or dom.endswith("." + d) for d in AGENT_DOMAINS) or "copilot" in m or "[bot]" in m:
            return True
    return bool(_GENERATED.search(body) or _TRAILER_KEY.search(body))


def _since_git(since):
    # A bare date means local midnight (as chain.commits_since).
    return since + "T00:00:00" if since and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else since


class ActivityIndex:
    """Agent actors in time: (ts, session, agent, worktree) for each tool call of an agent in a worktree of
    the repo. The worktree is the record cwd, or a path the Bash command names with `git -C` or `cd`."""

    def __init__(self, root, worktrees, since=None, home=None):
        self.recs = []
        self.first = self.last = None
        home = home or os.path.expanduser("~")
        cut = since.timestamp() - 86400 if since else None
        if not os.path.isdir(root):
            return
        times = []
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
                        if '"tool_use"' not in line:
                            continue
                        try:
                            r = json.loads(line)
                            ts = chain._ts_or_none(r.get("timestamp"))
                            sid, cwd = r.get("sessionId"), r.get("cwd")
                            content = r["message"]["content"]
                        except chain.BAD:
                            continue
                        if not sid or ts is None or r.get("type") != "assistant" or not isinstance(content, list):
                            continue
                        uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
                        if not uses:
                            continue
                        times.append(ts)
                        agent = (r.get("agentId") or "sidechain") if r.get("isSidechain") else None
                        wts = set()
                        if isinstance(cwd, str):
                            wts.add(_worktree(cwd, worktrees))
                        for u in uses:
                            cmd = (u.get("input") or {}).get("command") if u.get("name") == "Bash" else None
                            if isinstance(cmd, str):
                                for _, p in _CMD_PATH.findall(cmd):
                                    wts.add(_worktree(home + p[1:] if p.startswith("~") else p, worktrees))
                        for wt in wts - {None}:
                            self.recs.append((ts, sid, agent or "", wt))
        self.recs.sort()
        if times:
            self.first, self.last = min(times), max(times)

    def near(self, when):
        """Actors with a tool call in a repo worktree within WINDOW_S of `when`: {(session, agent): {worktree}}."""
        lo = bisect.bisect_left(self.recs, (when - datetime.timedelta(seconds=WINDOW_S),))
        hi = bisect.bisect_right(self.recs, (when + datetime.timedelta(seconds=WINDOW_S), "\uffff"))
        out = {}
        for ts, sid, agent, wt in self.recs[lo:hi]:
            out.setdefault((sid, agent), set()).add(wt)
        return out


def _worktree(path, worktrees):
    c = os.path.realpath(path)
    best = [p for p in worktrees if c == p or c.startswith(p + os.sep)]
    return max(best, key=len) if best else None


def trailers(repo, since):
    """sha -> True if the commit message has an agent trailer. One git call over the same commits as
    chain.commits_since (no SHA list on the command line)."""
    args = ["log", "--branches", "--tags", "--format=%H%x00%B%x1e"] + ([f"--since={_since_git(since)}"] if since else [])
    out = {}
    for chunk in chain._git(repo, *args).split("\x1e"):
        chunk = chunk.strip("\n")
        if "\x00" in chunk:
            h, body = chunk.split("\x00", 1)
            out[h] = is_agent_trailer(body)
    return out


def run(repo, since, transcripts_root, otel_path=None, policy_path=None, deploys_path=None, gh_hosts=None,
        hash_repo=False):
    dp = chain.default_paths()
    src = dict(otel=chain.otel_tool_results(otel_path or dp["otel"]),
               transcripts=chain.TranscriptIndex(transcripts_root),
               policy=chain.policy_rows(policy_path or dp["policy"]),
               deploys=chain.deploy_rows(deploys_path or dp["deploys"]))
    src["forge"] = chain.forge_state(repo, gh_hosts or dp["gh_hosts"])
    shas = chain.commits_since(repo, since)
    try:
        since_t = chain._ts(_since_git(since)) if since else None
    except ValueError:
        since_t = None   # a git date form ("2.weeks"): read every transcript file
    worktrees = chain.repo_paths(repo)
    act = ActivityIndex(transcripts_root, worktrees, since_t)
    windows = chain.source_windows(src["otel"], src["transcripts"], src["policy"], src["deploys"])
    trl = trailers(repo, since)
    top = os.path.realpath(chain._git(repo, "rev-parse", "--show-toplevel").strip())
    label = ("r-" + hashlib.sha256(top.encode()).hexdigest()[:8]) if hash_repo else os.path.basename(top)
    res = {"repo": label, "since": since, "window_s": WINDOW_S,
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
        near = act.near(ct)
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


def _shas(xs, limit, indent):
    """Short SHAs, 6 per line. limit=None: all."""
    shown = xs if limit is None else xs[:limit]
    more = f" (+{len(xs) - len(shown)} more; use --json)" if len(shown) < len(xs) else ""
    if not xs:
        return "-"
    lines = [", ".join(shown[i:i + 6]) for i in range(0, len(shown), 6)]
    return (",\n" + " " * indent).join(lines) + more


def render(r, limit=None):
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
        out.append(f"  {name:<52} {len(r[k]):>5}   {_shas(r[k], limit, 63)}")
    flagged = [x for x in r["rows"] if x["flags"]]
    shown = flagged if limit is None else flagged[-limit:]
    out += ["", f"Flagged commits ({len(flagged)}; {len(shown)} shown, oldest first)",
            "  sha           time (UTC)            label        session   actor     trailer actors wt  flags"]
    for x in shown:
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
            f"  4. Actors = distinct (session, subagent) pairs with a tool call {r['window_s']} s before or "
            "after the commit time whose cwd is in a worktree of this repo, or whose Bash command names one "
            "with `git -C` or `cd` (absolute or ~ paths). A relative path or a variable is not seen.",
            "  5. Transcripts are unsigned local files; a deleted transcript makes a commit MISSING.",
            "Nothing was written."]
    return "\n".join(out)
