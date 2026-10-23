#!/usr/bin/env python3
"""SessionStart, PreToolUse and PostToolUse hook: record a settings digest (the policy version) for each session.

Reads the hook JSON on stdin and appends one row to <dir>/policy.jsonl. <dir> is $DATAPCE_POLICY_DIR,
else $CHANGE_CHAIN_DIR, else $XDG_STATE_HOME/change-chain, else ~/.local/state/change-chain. Rows are written
at session start, and before and after each Bash `git commit` call (rows keyed by tool_use_id).
Commit-call rows also hold HEAD and the repo toplevel of the hook cwd, so chain.py can see
HEAD move parent -> commit inside one call. Other tool calls write nothing.
Stores only hashes, paths and mtimes. It never stores settings content.
The hook always exits 0 and prints nothing to stdout.
"""
from __future__ import annotations
import datetime, hashlib, json, os, re, subprocess, sys

# A Bash command that runs `git ... commit` (also `git -C dir commit`, `git -c k=v commit`).
COMMIT_CALL = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
MANAGED_BY_PLATFORM = {"darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
                       "linux": "/etc/claude-code/managed-settings.json"}
SCOPES = [
    ("user", "{home}/.claude/settings.json"),
    ("project", "{cwd}/.claude/settings.json"),
    ("local", "{cwd}/.claude/settings.local.json"),
    ("managed", "{managed}"),
]


def state_dir():
    """Directory for the hook capture and the chain.py inputs.

    $CHANGE_CHAIN_DIR, else $XDG_STATE_HOME/change-chain, else ~/.local/state/change-chain.
    An empty or relative $XDG_STATE_HOME is ignored (XDG Base Directory rule).
    """
    d = os.environ.get("CHANGE_CHAIN_DIR")
    if d:
        return d
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "change-chain")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "change-chain")


def out_dir():
    """Where the hook writes policy.jsonl: $DATAPCE_POLICY_DIR (older override), else the state dir."""
    return os.environ.get("DATAPCE_POLICY_DIR") or state_dir()


def managed_path(platform=None):
    """Managed-settings path for the platform (sys.platform by default). Other platforms use the Linux path."""
    platform = sys.platform if platform is None else platform
    key = "darwin" if platform == "darwin" else "linux"
    return MANAGED_BY_PLATFORM[key]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def settings_paths(cwd, home=None, managed=None):
    home = home if home is not None else os.path.expanduser("~")
    managed = managed if managed is not None else managed_path()
    return [(scope, tpl.format(home=home, cwd=cwd, managed=managed)) for scope, tpl in SCOPES]


def _stat_file(path):
    try:
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        mtime = datetime.datetime.fromtimestamp(
            os.stat(path).st_mtime, datetime.timezone.utc).isoformat()
        return sha, mtime
    except OSError:
        return None, None


def snapshot_files(cwd, home=None, managed=None):
    rows = []
    for scope, path in settings_paths(cwd, home, managed):
        sha, mtime = _stat_file(path)
        rows.append({"scope": scope, "path": path, "sha256": sha, "mtime": mtime})
    return rows


def digest(files):
    data = json.dumps([[f["scope"], f["sha256"]] for f in files], separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def git_head(cwd):
    """(HEAD sha, realpath of the repo toplevel) for cwd; None for each value git cannot give."""
    def run(*args):
        try:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    top = run("rev-parse", "--show-toplevel")
    head = run("rev-parse", "--verify", "-q", "HEAD^{commit}") if top else None
    return head, os.path.realpath(top) if top else None


def wanted(event):
    """Session events always; PreToolUse and PostToolUse only for a Bash git commit call."""
    if event.get("hook_event_name") not in TOOL_EVENTS:
        return True
    cmd = (event.get("tool_input") or {}).get("command")
    return event.get("tool_name") == "Bash" and isinstance(cmd, str) and bool(COMMIT_CALL.search(cmd))


def record(event, out_dir, home=None, managed=None):
    cwd = event.get("cwd") or os.getcwd()
    files = snapshot_files(cwd, home, managed)
    row = {"ts": _now(), "session_id": event.get("session_id"), "event": event.get("hook_event_name"),
           "source": event.get("source"), "tool_use_id": event.get("tool_use_id"),
           "cwd": cwd, "files": files, "digest": digest(files)}
    if event.get("hook_event_name") in TOOL_EVENTS:
        row["head"], row["toplevel"] = git_head(cwd)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(out_dir, "policy.jsonl"),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def main():
    try:
        event = json.load(sys.stdin)
        if wanted(event):
            record(event, out_dir())
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
