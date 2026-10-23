"""The policy snapshot must hash settings, ignore paths and mtimes, and never fail the hook."""
import io, json, os, stat, subprocess, tempfile, unittest
from unittest import mock

import policy_snapshot as ps


def f(scope, sha, path="/x", mtime=None):
    return {"scope": scope, "path": path, "sha256": sha, "mtime": mtime}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        t = self._tmp.name
        self.home, self.cwd = os.path.join(t, "home"), os.path.join(t, "cwd")
        self.managed = os.path.join(t, "managed.json")
        self.out = os.path.join(t, "out")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.cwd, ".claude"))

    def write(self, path, text):
        with open(path, "w") as fh:
            fh.write(text)


class DigestTest(Base):
    def test_stable(self):
        a = [f("user", "a"), f("project", "b")]
        self.assertEqual(ps.digest(a), ps.digest(list(a)))

    def test_order_sensitive(self):
        a, b = f("user", "a"), f("project", "b")
        self.assertNotEqual(ps.digest([a, b]), ps.digest([b, a]))

    def test_ignores_path_and_mtime(self):
        a = [f("user", "a", "/p1", "t1")]
        b = [f("user", "a", "/p2", "t2")]
        self.assertEqual(ps.digest(a), ps.digest(b))

    def test_formula(self):
        import hashlib
        files = [f("user", None)]
        want = hashlib.sha256(b'[["user",null]]').hexdigest()
        self.assertEqual(ps.digest(files), want)


class SnapshotTest(Base):
    def test_missing_is_none_and_order_kept(self):
        files = ps.snapshot_files(self.cwd, self.home, self.managed)
        self.assertEqual([x["scope"] for x in files], [s for s, _ in ps.SCOPES])
        self.assertTrue(all(x["sha256"] is None and x["mtime"] is None for x in files))

    def test_content_change_changes_digest(self):
        p = os.path.join(self.home, ".claude", "settings.json")
        self.write(p, "{}")
        d1 = ps.digest(ps.snapshot_files(self.cwd, self.home, self.managed))
        self.write(p, '{"a":1}')
        d2 = ps.digest(ps.snapshot_files(self.cwd, self.home, self.managed))
        self.assertNotEqual(d1, d2)

    def test_present_file_has_hash_and_mtime(self):
        self.write(self.managed, "{}")
        m = [x for x in ps.snapshot_files(self.cwd, self.home, self.managed) if x["scope"] == "managed"][0]
        self.assertEqual(len(m["sha256"]), 64)
        self.assertTrue(m["mtime"])


class RecordTest(Base):
    def test_appends_rows_with_mode_and_no_content(self):
        self.write(os.path.join(self.home, ".claude", "settings.json"), "SECRET-CONTENT")
        ev = {"session_id": "s1", "source": "startup", "cwd": self.cwd}
        ps.record(ev, self.out, self.home, self.managed)
        row = ps.record(dict(ev, session_id="s2"), self.out, self.home, self.managed)
        self.assertEqual(row["session_id"], "s2")
        path = os.path.join(self.out, "policy.jsonl")
        with open(path) as f:
            raw = f.read()
        rows = [json.loads(x) for x in raw.splitlines()]
        self.assertEqual([r["session_id"] for r in rows], ["s1", "s2"])
        self.assertNotIn("SECRET-CONTENT", raw)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)


class MainTest(Base):
    def run_main(self, text):
        with mock.patch("sys.stdin", io.StringIO(text)), \
             mock.patch.dict(os.environ, {"DATAPCE_POLICY_DIR": self.out}), \
             mock.patch("sys.stdout", io.StringIO()) as so:
            with self.assertRaises(SystemExit) as cm:
                ps.main()
        return cm.exception.code, so.getvalue()

    def test_bad_stdin_exits_zero(self):
        self.assertEqual(self.run_main("not json"), (0, ""))

    def test_good_stdin_writes_row(self):
        code, out = self.run_main(json.dumps({"session_id": "s", "source": "startup", "cwd": self.cwd}))
        self.assertEqual((code, out), (0, ""))
        self.assertTrue(os.path.exists(os.path.join(self.out, "policy.jsonl")))

    def test_pre_tool_use_writes_only_for_git_commit(self):
        base = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.cwd}
        for cmd in ("ls -la", "git status", "echo commit"):
            self.run_main(json.dumps(dict(base, tool_use_id="t0", tool_input={"command": cmd})))
        self.run_main(json.dumps(dict(base, tool_name="Edit", tool_input={"command": "git commit"})))
        self.assertFalse(os.path.exists(os.path.join(self.out, "policy.jsonl")))
        self.run_main(json.dumps(dict(base, tool_use_id="t1",
                                      tool_input={"command": "git -c a=b commit -q -m 'x SECRET-CMD'"})))
        with open(os.path.join(self.out, "policy.jsonl")) as f:
            raw = f.read()
        row = json.loads(raw)
        self.assertEqual((row["event"], row["tool_use_id"]), ("PreToolUse", "t1"))
        self.assertNotIn("SECRET-CMD", raw)

    def test_post_tool_use_writes_only_for_git_commit(self):
        base = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": self.cwd}
        for cmd in ("ls -la", "git status"):
            self.run_main(json.dumps(dict(base, tool_use_id="t0", tool_input={"command": cmd})))
        self.run_main(json.dumps(dict(base, tool_name="Edit", tool_input={"command": "git commit"})))
        self.assertFalse(os.path.exists(os.path.join(self.out, "policy.jsonl")))
        self.run_main(json.dumps(dict(base, tool_use_id="t1", tool_input={"command": "git commit -m x"})))
        with open(os.path.join(self.out, "policy.jsonl")) as f:
            row = json.loads(f.read())
        self.assertEqual((row["event"], row["tool_use_id"]), ("PostToolUse", "t1"))


class HeadTest(Base):
    """Commit-call rows carry HEAD and the repo toplevel of the hook cwd."""

    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", self.cwd, *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def commit_event(self, event):
        return {"session_id": "s", "hook_event_name": event, "tool_name": "Bash", "tool_use_id": "t1",
                "cwd": self.cwd, "tool_input": {"command": "git commit -m x"}}

    def test_row_has_head_and_toplevel(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "a")
        row = ps.record(self.commit_event("PostToolUse"), self.out, self.home, self.managed)
        self.assertEqual(row["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(row["toplevel"], os.path.realpath(self.cwd))

    def test_unborn_repo_head_is_none(self):
        self.git("init", "-q")
        row = ps.record(self.commit_event("PreToolUse"), self.out, self.home, self.managed)
        self.assertIsNone(row["head"])
        self.assertEqual(row["toplevel"], os.path.realpath(self.cwd))

    def test_not_a_repo_gives_none(self):
        row = ps.record(self.commit_event("PreToolUse"), self.out, self.home, self.managed)
        self.assertEqual((row["head"], row["toplevel"]), (None, None))

    def test_session_row_has_no_head(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "a")
        row = ps.record({"session_id": "s", "hook_event_name": "SessionStart", "cwd": self.cwd},
                        self.out, self.home, self.managed)
        self.assertNotIn("head", row)


class OutDirTest(Base):
    """G3: the hook writes to the same state dir that chain.py reads."""

    def run_hook(self, env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("DATAPCE_POLICY_DIR", "CHANGE_CHAIN_DIR", "XDG_STATE_HOME")}
        base.update(HOME=self.home, **env)
        text = json.dumps({"session_id": "s", "source": "startup", "cwd": self.cwd})
        with mock.patch("sys.stdin", io.StringIO(text)), mock.patch.dict(os.environ, base, clear=True), \
             mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                ps.main()

    def test_change_chain_dir(self):
        self.run_hook({"CHANGE_CHAIN_DIR": self.out})
        self.assertTrue(os.path.exists(os.path.join(self.out, "policy.jsonl")))

    def test_datapce_policy_dir_overrides(self):
        other = os.path.join(self._tmp.name, "other")
        self.run_hook({"CHANGE_CHAIN_DIR": other, "DATAPCE_POLICY_DIR": self.out})
        self.assertTrue(os.path.exists(os.path.join(self.out, "policy.jsonl")))
        self.assertFalse(os.path.exists(other))

    def test_default_is_home_state_dir(self):
        self.run_hook({})
        self.assertTrue(os.path.exists(
            os.path.join(self.home, ".local", "state", "change-chain", "policy.jsonl")))

    def test_hook_and_chain_agree(self):
        import chain
        for env in ({"CHANGE_CHAIN_DIR": self.out}, {"XDG_STATE_HOME": self._tmp.name}, {}):
            base = {k: v for k, v in os.environ.items()
                    if k not in ("DATAPCE_POLICY_DIR", "CHANGE_CHAIN_DIR", "XDG_STATE_HOME")}
            base.update(HOME=self.home, **env)
            with mock.patch.dict(os.environ, base, clear=True):
                self.assertEqual(os.path.join(ps.out_dir(), "policy.jsonl"), chain.default_paths()["policy"])


class ManagedPathTest(Base):
    """G4: the managed-settings path follows the platform."""

    def test_macos(self):
        self.assertEqual(ps.managed_path("darwin"), "/Library/Application Support/ClaudeCode/managed-settings.json")

    def test_linux(self):
        self.assertEqual(ps.managed_path("linux"), "/etc/claude-code/managed-settings.json")

    def test_settings_paths_use_current_platform(self):
        for plat, want in (("linux", "/etc/claude-code/managed-settings.json"),
                           ("darwin", "/Library/Application Support/ClaudeCode/managed-settings.json")):
            with mock.patch.object(ps.sys, "platform", plat):
                got = dict(ps.settings_paths(self.cwd, self.home))
            self.assertEqual(got["managed"], want)

    def test_explicit_managed_wins(self):
        with mock.patch.object(ps.sys, "platform", "linux"):
            self.assertEqual(dict(ps.settings_paths(self.cwd, self.home, self.managed))["managed"], self.managed)


class InstallHookTest(Base):
    """G5: install_hook.py works on a temp settings file; it never touches the real one in tests."""

    def setUp(self):
        super().setUp()
        import install_hook
        self.ih = install_hook
        self.settings = os.path.join(self._tmp.name, "cfg", "settings.json")

    def run_ih(self, *args):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            rc = self.ih.main(["--settings", self.settings, *args])
        return rc, out.getvalue()

    def load(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def ours(self, d):
        return {e: sum(h.get("command") == self.ih.CMD for x in d.get("hooks", {}).get(e, []) for h in x["hooks"])
                for e in self.ih.ENTRIES}

    def backups(self):
        d = os.path.dirname(self.settings)
        return sorted(n for n in os.listdir(d) if ".bak-" in n) if os.path.isdir(d) else []

    def test_symlinked_settings_stays_a_symlink(self):
        real = os.path.join(self._tmp.name, "dotfiles", "settings.json")
        os.makedirs(os.path.dirname(real))
        os.makedirs(os.path.dirname(self.settings))
        with open(real, "w") as fh:
            json.dump({"model": "x"}, fh)
        os.symlink(real, self.settings)
        rc, _ = self.run_ih()
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.islink(self.settings))
        with open(real) as fh:
            d = json.load(fh)
        self.assertEqual(d["model"], "x")
        self.assertEqual(self.ours(d), {e: 1 for e in self.ih.ENTRIES})

    def test_uninstall_with_nothing_to_remove_changes_nothing(self):
        os.makedirs(os.path.dirname(self.settings))
        text = '{"hooks": {}, "other": true}\n'
        with open(self.settings, "w") as fh:
            fh.write(text)
        rc, _ = self.run_ih("--uninstall")
        self.assertEqual(rc, 0)
        with open(self.settings) as fh:
            self.assertEqual(json.load(fh), {"hooks": {}, "other": True})

    def test_import_has_no_side_effect(self):
        self.assertTrue(callable(self.ih.main))
        self.assertFalse(os.path.exists(self.settings))

    def test_default_settings_path_follows_home(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertEqual(self.ih.default_settings(), os.path.join(self.home, ".claude", "settings.json"))

    def test_command_runs_policy_snapshot(self):
        self.assertTrue(self.ih.CMD.startswith("python3 "))
        self.assertTrue(self.ih.CMD.endswith("policy_snapshot.py") or self.ih.CMD.endswith("policy_snapshot.py'"))

    def test_creates_absent_settings(self):
        rc, out = self.run_ih()
        self.assertEqual(rc, 0)
        self.assertEqual(self.ours(self.load()), {"SessionStart": 1, "PreToolUse": 1, "PostToolUse": 1})
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.load()["hooks"]["PreToolUse"][0]["matcher"], "Bash")

    def test_keeps_other_settings_and_backs_up(self):
        os.makedirs(os.path.dirname(self.settings))
        orig = {"permissions": {"allow": ["Read"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}]}}
        self.write(self.settings, json.dumps(orig))
        self.assertEqual(self.run_ih()[0], 0)
        d = self.load()
        self.assertEqual(d["permissions"], orig["permissions"])
        self.assertEqual(d["hooks"]["PreToolUse"][0], orig["hooks"]["PreToolUse"][0])
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(os.path.dirname(self.settings), self.backups()[0])) as fh:
            self.assertEqual(json.load(fh), orig)

    def test_twice_adds_nothing(self):
        self.run_ih()
        rc, out = self.run_ih()
        self.assertEqual(self.ours(self.load()), {"SessionStart": 1, "PreToolUse": 1, "PostToolUse": 1})
        self.assertIn("already present", out)

    def test_dry_run_absent_writes_nothing(self):
        rc, out = self.run_ih("--dry-run")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(os.path.dirname(self.settings)))
        self.assertIn("would add", out)

    def test_dry_run_existing_writes_nothing(self):
        os.makedirs(os.path.dirname(self.settings))
        self.write(self.settings, '{"a": 1}')
        before = os.stat(self.settings).st_mtime_ns
        self.run_ih("--dry-run")
        self.assertEqual(self.load(), {"a": 1})
        self.assertEqual(os.stat(self.settings).st_mtime_ns, before)
        self.assertEqual(self.backups(), [])

    def test_uninstall_removes_only_own_entries(self):
        os.makedirs(os.path.dirname(self.settings))
        other = {"type": "command", "command": "other"}
        self.write(self.settings, json.dumps({"x": 1, "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [other]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [other, {"type": "command", "command": self.ih.CMD}]}]}}))
        self.run_ih()
        before = self.load()
        rc, out = self.run_ih("--uninstall")
        self.assertEqual(rc, 0)
        d = self.load()
        self.assertEqual(self.ours(d), {"SessionStart": 0, "PreToolUse": 0, "PostToolUse": 0})
        self.assertEqual(d["x"], 1)
        self.assertEqual(d["hooks"]["PreToolUse"], [{"matcher": "Bash", "hooks": [other]}])
        self.assertEqual(d["hooks"]["PostToolUse"], [{"matcher": "Bash", "hooks": [other]}])
        self.assertNotIn("SessionStart", d["hooks"])
        newest = self.backups()[-1]
        with open(os.path.join(os.path.dirname(self.settings), newest)) as fh:
            self.assertEqual(json.load(fh), before)

    def test_uninstall_dry_run_writes_nothing(self):
        self.run_ih()
        before, n = self.load(), len(self.backups())
        rc, out = self.run_ih("--uninstall", "--dry-run")
        self.assertEqual(self.load(), before)
        self.assertEqual(len(self.backups()), n)
        self.assertIn("would remove", out)

    def test_uninstall_absent_file(self):
        rc, out = self.run_ih("--uninstall")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.settings))

    def test_bad_json_writes_nothing(self):
        os.makedirs(os.path.dirname(self.settings))
        self.write(self.settings, "{not json")
        rc, out = self.run_ih()
        self.assertEqual(rc, 1)
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "{not json")
        self.assertEqual(self.backups(), [])


class CommitMatchTest(unittest.TestCase):
    """G14 review: the commit match is broad by design (README, Policy hook). An extra row makes no link."""

    def test_match_is_broad_within_one_command_segment(self):
        for cmd in ("git commit -m x", "git -C /r commit -m x", "git -c a=b commit", "git log --grep=commit"):
            self.assertTrue(ps.COMMIT_CALL.search(cmd), cmd)
        for cmd in ("git status && echo commit", "git status; echo commit", "echo commit", "git status"):
            self.assertFalse(ps.COMMIT_CALL.search(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
