"""chain.py audit: who made each commit, agent trailers vs session links, and concurrent agents in the repo."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest

import audit
import chain

TRAILER = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
T = {i: f"2026-10-20T1{i}:00:00+00:00" for i in range(1, 8)}
SUBJ = {1: "add parser", 2: "agent marked no record", 3: "subagent fix", 4: "human edit",
        5: "busy repo change", 6: "two sessions claim", 7: "late human"}
PRIVATE_CMD = "git commit -m 'PRIVATE-CMD-TEXT'"


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.repo = os.path.realpath(os.path.join(d, "repo"))
        self.wt = os.path.realpath(os.path.join(d, "repo-wt"))
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self.sha = {}
        for i, trailer in ((1, True), (2, True), (3, False), (4, False), (5, True), (6, True), (7, False)):
            self.sha[i] = self._commit(i, trailer)
        self._git("worktree", "add", "-q", "-b", "side", self.wt)
        self.projects = os.path.join(d, "projects")
        os.makedirs(os.path.join(self.projects, "p", "s1", "subagents"))
        main = [self._out("s1", None, 1), self._call("s1", None, 1, self.repo),
                self._out("s1", None, 5), self._call("s1", None, 5, self.repo),
                self._out("s1", None, 6)]
        sub = [self._out("s1", "a1", 3), self._call("s1", "a1", 3, self.repo)]
        # The last record (600 s after commit 7) only extends the transcript window past commit 7.
        other = [self._call("s2", None, 5, self.wt, dt=60), self._out("s2", None, 6),
                 self._call("s2", None, 6, self.repo), self._call("s2", None, 7, self.wt, dt=600)]
        self._write(os.path.join(self.projects, "p", "s1.jsonl"), main)
        self._write(os.path.join(self.projects, "p", "s1", "subagents", "agent-a1.jsonl"), sub)
        self._write(os.path.join(self.projects, "p", "s2.jsonl"), other)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                 GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **(env or {}))
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True,
                              text=True, env=e).stdout.strip()

    def _commit(self, i, trailer):
        with open(os.path.join(self.repo, "f.txt"), "a") as f:
            f.write(SUBJ[i] + "\n")
        self._git("add", "f.txt")
        self._git("commit", "-q", "-m", SUBJ[i] + (TRAILER if trailer else ""),
                  env={"GIT_AUTHOR_DATE": T[i], "GIT_COMMITTER_DATE": T[i]})
        return self._git("rev-parse", "HEAD")

    def _rec(self, sid, agent, i, cwd, content, kind, dt=2):
        ts = chain._ts(T[i]).timestamp() + dt
        r = {"type": kind, "sessionId": sid, "cwd": cwd, "message": {"content": content},
             "timestamp": chain.datetime.datetime.fromtimestamp(ts, chain.datetime.timezone.utc)
             .isoformat().replace("+00:00", "Z")}
        if agent:
            r.update(isSidechain=True, agentId=agent)
        return r

    def _out(self, sid, agent, i):
        text = f"[main {self.sha[i][:7]}] {SUBJ[i]}\n 1 file changed"
        return self._rec(sid, agent, i, self.repo,
                         [{"type": "tool_result", "tool_use_id": f"t{i}", "content": text}], "user")

    def _call(self, sid, agent, i, cwd, dt=1):
        return self._rec(sid, agent, i, cwd, [{"type": "tool_use", "id": f"c{sid}{i}", "name": "Bash",
                                               "input": {"command": PRIVATE_CMD}}], "assistant", dt)

    def _write(self, path, recs):
        with open(path, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def _run(self):
        return audit.run(self.repo, None, self.projects, otel_path=os.devnull, policy_path=os.devnull,
                         deploys_path=os.devnull)

    def short(self, *ids):
        return sorted(self.sha[i][:12] for i in ids)

    def test_counts(self):
        r = self._run()
        self.assertEqual(r["commits"], 7)
        self.assertEqual(r["linked"], {"ESTABLISHED": 0, "INFERRED": 4})
        self.assertEqual(r["missing"], 3)

    def test_subagent_commit_is_named(self):
        r = self._run()
        self.assertEqual(r["subagent"], self.short(3))
        row = next(x for x in r["rows"] if x["sha"] == self.sha[3][:12])
        self.assertEqual(row["actor"], "subagent")

    def test_trailer_without_session_record(self):
        self.assertEqual(self._run()["trailer_no_session"], self.short(2))

    def test_agent_session_without_trailer_reads_as_human(self):
        self.assertEqual(self._run()["session_no_trailer"], self.short(3))

    def test_two_candidate_sessions(self):
        self.assertEqual(self._run()["multi_candidate"], self.short(6))

    def test_concurrent_actors_and_worktrees(self):
        r = self._run()
        self.assertEqual(r["multi_actor"], self.short(5, 6))
        self.assertEqual(r["multi_worktree"], self.short(5))
        row = next(x for x in r["rows"] if x["sha"] == self.sha[5][:12])
        self.assertEqual((row["actors"], row["worktrees"]), (2, 2))

    def test_subagent_and_parent_count_as_two_actors(self):
        p = os.path.join(self.projects, "p", "s1.jsonl")
        with open(p, "a") as f:
            f.write(json.dumps(self._call("s1", None, 3, self.repo, dt=30)) + "\n")
        r = self._run()
        self.assertIn(self.sha[3][:12], r["multi_actor"])

    def test_commit_outside_transcript_window_is_no_data(self):
        self._git("commit", "-q", "--allow-empty", "-m", "old" + TRAILER,
                  env={"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        old = self._git("rev-parse", "HEAD")[:12]
        r = self._run()
        self.assertEqual(r["in_window"], 7)
        self.assertNotIn(old, r["trailer_no_session"])
        self.assertEqual(r["out_of_window"], 1)

    def test_only_tool_calls_in_this_repo_count(self):
        elsewhere = os.path.join(self.tmp.name, "repo2")
        os.makedirs(elsewhere)
        with open(os.path.join(self.projects, "p", "s3.jsonl"), "w") as f:
            f.write(json.dumps(self._call("s3", None, 1, elsewhere)) + "\n")
        self.assertNotIn(self.sha[1][:12], self._run()["multi_actor"])

    def test_text_output_has_items_and_no_private_text(self):
        r = self._run()
        out = audit.render(r)
        for s in ("commits", "subagent", "agent trailer, no session record",
                  "agent session, no agent trailer", "more than one candidate session",
                  "more than one agent actor", "more than one worktree", "Limits"):
            self.assertIn(s, out)
        for s in list(SUBJ.values()) + ["PRIVATE-CMD-TEXT", self.repo, self.wt, self.tmp.name, "t@t"]:
            self.assertNotIn(s, out)
        self.assertIn(self.sha[2][:12], out)

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = chain.main(["audit", "--repo", self.repo, "--transcripts", self.projects,
                             "--otel", os.devnull, "--policy", os.devnull, "--deploys", os.devnull, "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["trailer_no_session"], self.short(2))


if __name__ == "__main__":
    unittest.main()
