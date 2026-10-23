"""Adversarial review probes for chain.py (2026-10-20).

Each probe asserts the CORRECT behaviour. A probe that fails or errors shows a defect.
Run: python3 -m unittest test_review_probes -v
"""
import contextlib
import io
import json
import os
import unittest

import chain
import policy_snapshot
import test_chain
from test_chain import SECRET, _epoch, _otel_line


def _otel_raw(records):
    """records: list of attribute dicts (str values)."""
    recs = [{"attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in a.items()]} for a in records]
    return json.dumps({"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}})


def _ev(session, ts, rev, tool="Bash", seq=0):
    a = {"event.name": "tool_result", "event.timestamp": ts, "event.sequence": seq,
         "tool_name": tool, "tool_use_id": f"toolu_{seq}", "vcs.ref.head.revision": rev}
    if session is not None:
        a["session.id"] = session
    return a


class Probe(test_chain.ChainTest):
    """Reuses the fixture of test_chain.ChainTest. Only probe_* methods run (see load_tests)."""

    def _out(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def _lookup_file(self, otel_text="", policy_path=None):
        p = os.path.join(self.tmp.name, "logs.jsonl")
        self._out(p, otel_text)
        pol = chain.policy_rows(policy_path) if policy_path else {}
        return chain.lookup(self.repo, self.b[:8], otel=chain.otel_tool_results(p),
                            transcripts=chain.TranscriptIndex(self.transcripts),
                            policy=pol, home=self.home, managed=self.managed)

    # ---- labels that overstate the evidence --------------------------------------------

    def probe_head_move_seen_by_read_call_after_idle_gap(self):
        # Session idle since yesterday; the human commits b in a terminal at 11:00; the
        # session's next call (a Read, which cannot move HEAD) ends 60 s later.
        r = self._lookup([_otel_raw([
            _ev("S9", "2026-10-19T09:00:00Z", self.a, "Read", 1),
            _ev("S9", "2026-10-20T11:01:00Z", self.b, "Read", 2)])])
        self.assertNotEqual(r["session"]["label"], "ESTABLISHED", r["session"]["evidence"])

    def probe_two_sessions_both_established_no_conflict(self):
        # Two sessions share the worktree. Both see HEAD a -> b inside the window. At most
        # one made the commit, but the result is a single ESTABLISHED link and no gap.
        r = self._lookup([_otel_raw([_ev("S1", "2026-10-20T10:59:00Z", self.a, seq=1),
                                     _ev("S1", "2026-10-20T11:00:05Z", self.b, seq=2)]),
                          _otel_raw([_ev("S2", "2026-10-20T10:58:00Z", self.a, seq=1),
                                     _ev("S2", "2026-10-20T11:00:04Z", self.b, seq=2)])])
        # Changed 2026-10-20: both moves are Bash calls. A Read call cannot commit, so S1 Bash + S2 Read
        # gives one ESTABLISHED link by design.
        conflict = r["session"]["label"] != "ESTABLISHED" or r["gaps"]
        self.assertTrue(conflict, (r["session"]["session"], r["session"]["label"], r["other_sessions"]))

    def probe_policy_established_after_file_created_then_deleted(self):
        pol = self._snapshot("S1", "2026-10-20T10:30:00+00:00")      # no settings.local.json at 10:30
        local = os.path.join(self.repo, ".claude", "settings.local.json")
        self._out(local, '{"permissions": {"allow": ["Bash(*)"]}}')  # created 10:45, live at 11:00 commit
        os.utime(local, (_epoch("2026-10-20T10:45:00+00:00"),) * 2)
        os.remove(local)                                             # deleted 12:00, after the commit
        r = self._lookup(self._s1(), pol)
        self.assertNotEqual(r["policy"]["label"], "ESTABLISHED", r["policy"]["evidence"])

    def probe_policy_established_after_mv_restore_keeps_old_mtime(self):
        backup = self.settings + ".bak"                               # content A, mtime 10:00
        with open(self.settings) as f:
            a_text = f.read()
        self._out(backup, a_text)
        os.utime(backup, (_epoch("2026-10-20T10:00:00+00:00"),) * 2)
        pol = self._snapshot("S1", "2026-10-20T10:30:00+00:00")      # snapshot sees A
        self._write_settings('{"permissions": {"allow": ["Bash(*)"]}}', "2026-10-20T10:45:00+00:00")  # B at commit
        os.replace(backup, self.settings)                             # 12:00 `mv settings.json.bak settings.json`
        r = self._lookup(self._s1(), pol)
        self.assertNotEqual(r["policy"]["label"], "ESTABLISHED", r["policy"]["evidence"])

    def probe_policy_checks_repo_toplevel_not_session_cwd(self):
        wt2 = os.path.realpath(os.path.join(self.tmp.name, "wt2"))
        self._git("worktree", "add", "-q", wt2, self.a)
        files = policy_snapshot.snapshot_files(wt2, self.home, self.managed)
        pol = {"S1": [{"ts": "2026-10-20T10:30:00+00:00", "session_id": "S1", "source": "startup",
                       "cwd": wt2, "files": files, "digest": policy_snapshot.digest(files)}]}
        local = os.path.join(wt2, ".claude", "settings.local.json")   # session's own cwd, written 10:45
        self._out(local, '{"permissions": {"allow": ["Bash(*)"]}}')
        os.utime(local, (_epoch("2026-10-20T10:45:00+00:00"),) * 2)
        r = self._lookup(self._s1(), pol)
        self.assertNotEqual(r["policy"]["label"], "ESTABLISHED", r["policy"]["evidence"])

    def probe_missing_session_id_gives_established_none(self):
        files = policy_snapshot.snapshot_files(self.repo, self.home, self.managed)
        pol = {None: [{"ts": "2026-10-20T10:30:00+00:00", "session_id": None, "source": "startup",
                       "cwd": self.repo, "files": files, "digest": policy_snapshot.digest(files)}]}
        r = self._lookup([_otel_raw([_ev(None, "2026-10-20T10:59:00Z", self.a, seq=1),
                                     _ev(None, "2026-10-20T11:00:05Z", self.b, seq=2)])], pol)
        self.assertFalse(r["session"]["session"] is None and r["session"]["label"] == "ESTABLISHED",
                         (r["session"]["session"], r["session"]["label"], r["policy"]["label"]))

    # ---- crashes -----------------------------------------------------------------------

    def probe_crash_sessions_on_top_none_and_str(self):
        self._lookup([_otel_raw([_ev("S2", "2026-10-20T11:30:00Z", self.b, seq=1)]),
                      _otel_raw([_ev(None, "2026-10-20T11:31:00Z", self.b, seq=1)])])

    def probe_crash_naive_otel_timestamp(self):
        self._lookup([_otel_raw([_ev("S1", "2026-10-20T10:59:00", self.a, seq=1),
                                 _ev("S1", "2026-10-20T11:00:05", self.b, seq=2)])])

    def probe_crash_naive_transcript_timestamp(self):
        self._transcript(self._call("2026-10-20T11:00:02", self.repo))
        self._lookup()

    def probe_crash_otel_line_is_json_null(self):
        self._lookup_file("null\n")

    def probe_crash_otel_attribute_without_value(self):
        self._lookup_file(json.dumps({"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"attributes": [{"key": "event.name"}]}]}]}]}}) + "\n")

    def probe_crash_otel_sequence_not_int(self):
        self._lookup([_otel_raw([dict(_ev("S1", "2026-10-20T11:00:05Z", self.b), **{"event.sequence": "1.5"})])])

    def probe_crash_policy_row_without_ts(self):
        p = os.path.join(self.tmp.name, "policy.jsonl")
        self._out(p, json.dumps({"session_id": "S1"}) + "\n" + json.dumps({"session_id": "S1", "ts": "x"}) + "\n")
        self._lookup_file("", p)

    def probe_crash_transcript_message_is_string(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "message": "git commit ] x"})
        self._lookup()

    def probe_crash_transcript_command_null(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": None, "description": "git commit"}}]}})
        self._lookup()

    # ---- false links -------------------------------------------------------------------

    def probe_false_link_path_prefix_in_command(self):
        cmd = f"git -C {self.repo}-other commit -q -m 'second change'"
        self._transcript({"sessionId": "S4", "timestamp": "2026-10-20T11:00:02Z", "cwd": "/elsewhere",
                          "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})
        self.assertEqual(self._lookup()["session"]["label"], "MISSING")

    def probe_false_link_subject_substring(self):
        cmd = "git commit -q -m 'second change, part 2'"
        self._transcript({"sessionId": "S4", "timestamp": "2026-10-20T11:00:02Z", "cwd": self.repo,
                          "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})
        self.assertEqual(self._lookup()["session"]["label"], "MISSING")

    def probe_false_link_output_read_days_later_elsewhere(self):
        # A later session `cat`s an old log that holds the commit output. No time or repo bound.
        self._transcript({"sessionId": "S7", "timestamp": "2026-10-23T09:00:00Z", "cwd": "/elsewhere",
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"log line: [main {self.b[:7]}] second change"}]}})
        self.assertEqual(self._lookup()["session"]["label"], "MISSING")

    def probe_false_link_lexicographic_ts_sort(self):
        # True order: HEAD=b at 11:00:05Z, then reset to a at 11:00:05.500Z. String sort reverses them.
        r = self._lookup([_otel_raw([_ev("S1", "2026-10-20T11:00:05Z", self.b, seq=0),
                                     _ev("S1", "2026-10-20T11:00:05.500Z", self.a, seq=0)])])
        self.assertEqual(r["session"]["label"], "MISSING", r["session"]["evidence"])

    # ---- text reaching the output ------------------------------------------------------

    def probe_transcript_text_in_branch_group_reaches_output(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "cwd": "/gone",
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"[{SECRET} {self.b[:7]}] second change"}]}})
        r = self._lookup()
        self.assertNotIn(SECRET, chain.render(r))
        self.assertNotIn(SECRET, json.dumps(r))

    def probe_commit_subject_reaches_output(self):
        # REJECTED finding (2026-10-20): the subject is git text from the user's repo, kept for the feed.
        # Guard the real rule instead: the transcript tool output is not copied.
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "cwd": self.repo,
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"[main {self.b[:7]}] second change\n{SECRET}"}]}})
        r = self._lookup()
        self.assertNotIn(SECRET, json.dumps(r))

    # ---- missed links (honest MISSING, lost coverage) ---------------------------------

    def probe_missed_amend(self):
        # KNOWN LIMIT (README): an amend moves HEAD from a non-parent, so the OTel link stays MISSING.
        pre_amend = "f" * 40
        r = self._lookup([_otel_raw([_ev("S1", "2026-10-20T10:59:00Z", pre_amend, seq=1),
                                     _ev("S1", "2026-10-20T11:00:05Z", self.b, seq=2)])])
        self.assertEqual(r["session"]["label"], "MISSING")

    def probe_missed_detached_head_output(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "cwd": self.repo,
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"[detached HEAD {self.b[:7]}] second change"}]}})
        self.assertEqual(self._lookup()["session"]["session"], "S3")

    def probe_missed_git_dash_c_config_commit(self):
        cmd = "git -c commit.gpgsign=false commit -q -m 'second change'"
        self._transcript({"sessionId": "S4", "timestamp": "2026-10-20T11:00:02Z", "cwd": self.repo,
                          "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})
        self.assertEqual(self._lookup()["session"]["session"], "S4")

    def probe_coverage_counts_stash_commits(self):
        with open(os.path.join(self.repo, "f.txt"), "a") as f:
            f.write("dirty\n")
        self._git("stash", "push", "-q", "-m", "probe")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chain.main(["coverage", "--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                        "--transcripts", self.transcripts])
        self.assertIn("commits: 2", buf.getvalue())


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in sorted(n for n in dir(Probe) if n.startswith("probe_")):
        suite.addTest(Probe(name))
    return suite


if __name__ == "__main__":
    unittest.main()
