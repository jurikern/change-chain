#!/usr/bin/env python3
"""Add the policy_snapshot.py hook to a Claude Code settings file (SessionStart + PreToolUse/Bash + PostToolUse/Bash).

    python3 install_hook.py [--settings PATH] [--dry-run] [--uninstall]

The default file is ~/.claude/settings.json. The script creates it if it is absent.
It makes a backup (<file>.bak-<time>) before it changes an existing file.
Running it twice adds nothing. It keeps all other settings and hooks.
--dry-run prints the changes and writes nothing.
--uninstall removes only the entries that run this policy_snapshot.py.
"""
import argparse, json, os, shlex, shutil, sys, tempfile, time

CMD = "python3 " + shlex.quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy_snapshot.py"))
ENTRIES = {"SessionStart": "startup|resume|clear|compact", "PreToolUse": "Bash", "PostToolUse": "Bash"}


def default_settings():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _ours(h):
    return isinstance(h, dict) and h.get("command") == CMD


def install(d):
    """Add the missing entries. Returns {event: 'added' | 'already present'}."""
    hooks = d.setdefault("hooks", {})
    result = {}
    for event, matcher in ENTRIES.items():
        present = any(_ours(h) for e in hooks.get(event, []) for h in e.get("hooks", []))
        if not present:
            hooks.setdefault(event, []).append({"matcher": matcher, "hooks": [{"type": "command", "command": CMD}]})
        result[event] = "already present" if present else "added"
    return result


def uninstall(d):
    """Remove only hooks that run CMD. Drop entries and events that become empty. Returns {event: count removed}."""
    result = {event: 0 for event in ENTRIES}
    hooks = d.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in list(hooks):
        kept, removed = [], 0
        for e in hooks[event]:
            own = [h for h in e.get("hooks", []) if _ours(h)]
            if own:
                removed += len(own)
                rest = [h for h in e["hooks"] if not _ours(h)]
                if not rest:
                    continue
                e = dict(e, hooks=rest)
            kept.append(e)
        if removed:
            result[event] = result.get(event, 0) + removed
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if any(result.values()) and not hooks:   # drop the key only when this run emptied it
        del d["hooks"]
    return result


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup, n = f"{path}.bak-{stamp}", 1
    while os.path.exists(backup):
        n += 1
        backup = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, backup)
    return backup


def _write(path, d):
    path = os.path.realpath(path)   # a symlinked settings file stays a symlink; the target gets the new text
    text = json.dumps(d, indent=2) + "\n"
    json.loads(text)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--settings", default=None, help="settings file (default: ~/.claude/settings.json)")
    ap.add_argument("--dry-run", action="store_true", help="print the changes; write nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove only the entries of this hook")
    a = ap.parse_args(argv)
    path = a.settings or default_settings()
    exists = os.path.exists(path)
    d = {}
    if exists:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as e:
            print(f"cannot read {path}: {e.__class__.__name__}; nothing written", file=sys.stderr)
            return 1
        if not isinstance(d, dict):
            print(f"{path} is not a JSON object; nothing written", file=sys.stderr)
            return 1
    before = json.dumps(d, sort_keys=True)
    if a.uninstall:
        for event, n in uninstall(d).items():
            print(f"{event}: " + ((f"would remove {n}" if a.dry_run else f"removed {n}") if n else "not present"))
    else:
        for event, state in install(d).items():
            print(f"{event}: " + ("would add" if a.dry_run and state == "added" else state))
    if json.dumps(d, sort_keys=True) == before:
        print(f"no change: {path}")
        return 0
    if a.dry_run:
        print(f"dry run: {path} not written")
        return 0
    if exists:
        print(f"backup: {_backup(path)}")
    _write(path, d)
    print(f"{'updated' if exists else 'created'}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
