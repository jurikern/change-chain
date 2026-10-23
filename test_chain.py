"""Change-chain v0 reverse lookup: commit -> session -> policy, with ESTABLISHED / INFERRED / MISSING labels."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import chain
import policy_snapshot

T_A = "2026-10-20T10:00:00+00:00"
T_B = "2026-10-20T11:00:00+00:00"
SECRET = "hunter2-token-xyz"


def _epoch(iso):
    return chain._ts(iso).timestamp()


def _otel_line(session, events):
    recs = [{"attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in {
        "session.id": session, "event.name": "tool_result", "event.timestamp": ts, "event.sequence": i,
        "tool_name": "Bash", "tool_use_id": f"toolu_{i}", "vcs.ref.head.revision": rev}.items()]}
        for i, (ts, rev) in enumerate(events)]
    return json.dumps({"ts": "x", "payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}})


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.repo = os.path.realpath(os.path.join(d, "repo"))
        self.home = os.path.join(d, "home")
        self.managed = os.path.join(d, "managed.json")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self.a = self._commit("first", T_A)
        self.b = self._commit("second change", T_B)
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self._write_settings('{"permissions": {}}', "2026-10-19T00:00:00+00:00")
        self.transcripts = os.path.join(d, "projects")
        os.makedirs(self.transcripts)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args, env=None):
        e = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                 GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **(env or {}))
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True,
                              text=True, env=e).stdout.strip()

    def _commit(self, subject, when):
        with open(os.path.join(self.repo, "f.txt"), "a") as f:
            f.write(subject + "\n")
        self._git("add", "f.txt")
        self._git("commit", "-q", "-m", subject, env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        return self._git("rev-parse", "HEAD")

    # git reads a bare date in --since as that day at the CURRENT clock time; a day boundary must be midnight.
    def test_since_bare_date_means_the_start_of_that_local_day(self):
        late_prev = self._commit("day before", "2026-10-20T23:59:00")   # local time, after T_B
        early = self._commit("just after midnight", "2026-10-21T00:01:00")
        got = chain.commits_since(self.repo, "2026-10-21")
        self.assertIn(early, got)
        self.assertNotIn(late_prev, got)

    def _write_settings(self, text, mtime):
        with open(self.settings, "w") as f:
            f.write(text)
        os.utime(self.settings, (_epoch(mtime), _epoch(mtime)))

    def _transcript(self, *recs):
        with open(os.path.join(self.transcripts, "s.jsonl"), "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def _lookup(self, otel_lines=(), policy=None):
        p = os.path.join(self.tmp.name, "logs.jsonl")
        with open(p, "w") as f:
            f.write("\n".join(otel_lines) + "\n")
        return chain.lookup(self.repo, self.b[:8], otel=chain.otel_tool_results(p),
                            transcripts=chain.TranscriptIndex(self.transcripts),
                            policy=policy or {}, home=self.home, managed=self.managed)

    def _snapshot(self, session, ts):
        files = policy_snapshot.snapshot_files(self.repo, self.home, self.managed)
        return {session: [{"ts": ts, "session_id": session, "source": "startup", "cwd": self.repo,
                           "files": files, "digest": policy_snapshot.digest(files)}]}

    # commit -> session

    def test_otel_head_move_at_commit_time_is_established(self):
        r = self._lookup([_otel_line("S1", [("2026-10-20T10:59:00Z", self.a), ("2026-10-20T11:00:05Z", self.b)])])
        self.assertEqual(r["commit"]["sha"], self.b)
        self.assertEqual((r["session"]["session"], r["session"]["label"]), ("S1", "ESTABLISHED"))

    def test_otel_head_move_far_from_commit_time_is_inferred(self):
        r = self._lookup([_otel_line("S1", [("2026-10-20T12:00:00Z", self.a), ("2026-10-20T12:30:00Z", self.b)])])
        self.assertEqual(r["session"]["label"], "INFERRED")

    def test_session_only_on_top_of_commit_is_not_a_link(self):
        r = self._lookup([_otel_line("S2", [("2026-10-20T11:30:00Z", self.b)])])
        self.assertEqual(r["session"]["label"], "MISSING")
        self.assertEqual(r["sessions_on_top"], ["S2"])

    def test_transcript_commit_output_is_inferred(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "cwd": "/gone",
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"[main {self.b[:7]}] second change\n 1 file"}]}})
        r = self._lookup()
        self.assertEqual((r["session"]["session"], r["session"]["label"]), ("S3", "INFERRED"))
        self.assertEqual(r["session"]["source"], "transcript-output")

    def test_transcript_output_with_other_subject_does_not_match(self):
        self._transcript({"sessionId": "S3", "timestamp": "2026-10-20T11:00:01Z", "cwd": self.repo,
                          "message": {"content": [{"type": "tool_result",
                                                   "content": f"[main {self.b[:7]}] other subject"}]}})
        self.assertEqual(self._lookup()["session"]["label"], "MISSING")

    def _call(self, ts, cwd):
        return {"sessionId": "S4", "timestamp": ts, "cwd": cwd, "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": f"git commit -q -m 'second change' # {SECRET}"}}]}}

    def test_quiet_commit_call_in_repo_inside_window_is_inferred(self):
        self._transcript(self._call("2026-10-20T11:00:02Z", self.repo))
        r = self._lookup()
        self.assertEqual((r["session"]["session"], r["session"]["source"]), ("S4", "transcript-call"))
        self.assertEqual(r["session"]["label"], "INFERRED")

    def test_commit_call_outside_window_or_repo_is_ignored(self):
        self._transcript(self._call("2026-10-20T11:30:00Z", self.repo), self._call("2026-10-20T11:00:02Z", "/elsewhere"))
        self.assertEqual(self._lookup()["session"]["label"], "MISSING")

    def test_established_beats_inferred(self):
        self._transcript(self._call("2026-10-20T11:00:02Z", self.repo))
        r = self._lookup([_otel_line("S1", [("2026-10-20T10:59:00Z", self.a), ("2026-10-20T11:00:05Z", self.b)])])
        self.assertEqual(r["session"]["session"], "S1")
        self.assertEqual(r["other_sessions"], ["S4"])

    def test_output_has_no_command_text(self):
        self._transcript(self._call("2026-10-20T11:00:02Z", self.repo))
        r = self._lookup()
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertNotIn(SECRET, chain.render(r))

    def test_no_record_gives_missing_and_gaps(self):
        r = self._lookup()
        self.assertEqual((r["session"]["label"], r["policy"]["label"]), ("MISSING", "MISSING"))
        self.assertEqual(r["gaps"], ["commit -> session"])

    # session -> policy

    def _s1(self):
        return [_otel_line("S1", [("2026-10-20T10:59:00Z", self.a), ("2026-10-20T11:00:05Z", self.b)])]

    def _call_snapshot(self, session, ts, tool_use_id):
        pol = self._snapshot(session, ts)
        pol[session][0].update(event="PreToolUse", source=None, tool_use_id=tool_use_id)
        return pol

    def test_snapshot_at_the_commit_call_is_established(self):
        pol = self._call_snapshot("S1", "2026-10-20T11:00:01+00:00", "toolu_1")
        r = self._lookup(self._s1(), pol)
        self.assertEqual(r["policy"]["label"], "ESTABLISHED")
        self.assertEqual(r["policy"]["digest"], pol["S1"][0]["digest"])

    def test_snapshot_of_another_call_is_inferred(self):
        r = self._lookup(self._s1(), self._call_snapshot("S1", "2026-10-20T10:50:00+00:00", "toolu_9"))
        self.assertEqual(r["policy"]["label"], "INFERRED")

    def test_session_start_snapshot_is_inferred(self):
        pol = self._snapshot("S1", "2026-10-20T10:30:00+00:00")
        r = self._lookup(self._s1(), pol)
        self.assertEqual((r["policy"]["label"], r["policy"]["source"]), ("INFERRED", "policy-capture"))
        self.assertEqual(r["policy"]["digest"], pol["S1"][0]["digest"])

    def test_transcript_call_id_joins_the_policy_snapshot(self):
        call = self._call("2026-10-20T11:00:02Z", self.repo)
        call["message"]["content"][0]["id"] = "toolu_T"
        self._transcript(call)
        pol = self._call_snapshot("S4", "2026-10-20T11:00:01+00:00", "toolu_T")
        r = self._lookup(policy=pol)
        self.assertEqual((r["session"]["label"], r["policy"]["label"]), ("INFERRED", "ESTABLISHED"))

    def test_snapshot_after_commit_is_not_used(self):
        r = self._lookup(self._s1(), self._snapshot("S1", "2026-10-20T11:30:00+00:00"))
        self.assertEqual(r["policy"]["source"], "settings-mtime")

    def test_no_snapshot_old_settings_is_inferred_by_mtime(self):
        r = self._lookup(self._s1())
        self.assertEqual((r["policy"]["label"], r["policy"]["source"]), ("INFERRED", "settings-mtime"))

    def test_no_snapshot_settings_newer_than_commit_is_missing(self):
        self._write_settings("{}", "2026-10-20T12:00:00+00:00")
        r = self._lookup(self._s1())
        self.assertEqual(r["policy"]["label"], "MISSING")
        self.assertEqual(r["gaps"], ["session -> policy"])

    # commit -> session from the hook HEAD record (Pre and Post rows of one commit call)

    def _hook_call(self, session, before, after, post_ts="2026-10-20T11:00:03+00:00", tool_use_id="toolu_H",
                   toplevel=None, pre=True):
        files = policy_snapshot.snapshot_files(self.repo, self.home, self.managed)
        top = toplevel or self.repo
        rows = [{"ts": post_ts, "session_id": session, "event": "PostToolUse", "tool_use_id": tool_use_id,
                 "cwd": top, "files": files, "digest": policy_snapshot.digest(files), "head": after,
                 "toplevel": top}]
        if pre:
            rows.insert(0, dict(rows[0], ts="2026-10-20T10:59:58+00:00", event="PreToolUse", head=before))
        return {session: rows}

    def test_hook_head_move_parent_to_commit_is_established(self):
        r = self._lookup(policy=self._hook_call("S5", self.a, self.b))
        self.assertEqual((r["session"]["session"], r["session"]["label"]), ("S5", "ESTABLISHED"))
        self.assertEqual((r["session"]["source"], r["session"]["tool_use_id"]), ("hook-head", "toolu_H"))
        self.assertEqual(r["policy"]["label"], "ESTABLISHED")
        self.assertEqual(r["gaps"], [])

    def test_hook_head_root_commit_is_established(self):
        pol = self._hook_call("S5", None, self.a, post_ts="2026-10-20T10:00:02+00:00")
        r = chain.lookup(self.repo, self.a, otel={}, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy=pol, home=self.home, managed=self.managed)
        self.assertEqual(r["session"]["label"], "ESTABLISHED")

    def test_hook_head_before_not_parent_is_inferred(self):
        r = self._lookup(policy=self._hook_call("S5", "0" * 40, self.b))
        self.assertEqual((r["session"]["label"], r["session"]["source"]), ("INFERRED", "hook-head"))

    def test_hook_head_without_pre_row_is_inferred(self):
        r = self._lookup(policy=self._hook_call("S5", None, self.b, pre=False))
        self.assertEqual(r["session"]["label"], "INFERRED")

    def test_hook_head_outside_window_is_inferred(self):
        r = self._lookup(policy=self._hook_call("S5", self.a, self.b, post_ts="2026-10-20T11:30:00+00:00"))
        self.assertEqual(r["session"]["label"], "INFERRED")

    def test_hook_head_in_other_repo_is_ignored(self):
        r = self._lookup(policy=self._hook_call("S5", self.a, self.b, toplevel="/elsewhere"))
        self.assertEqual(r["session"]["label"], "MISSING")

    def test_hook_head_after_not_commit_is_ignored(self):
        r = self._lookup(policy=self._hook_call("S5", self.a, self.a))
        self.assertEqual(r["session"]["label"], "MISSING")

    def test_hook_head_two_sessions_conflict(self):
        pol = dict(self._hook_call("S5", self.a, self.b), **self._hook_call("S6", self.a, self.b, tool_use_id="t6"))
        r = self._lookup(policy=pol)
        self.assertEqual(r["session"]["label"], "INFERRED")
        self.assertIn("commit -> session: conflicting sessions", r["gaps"])

    def test_hook_head_beats_transcript(self):
        self._transcript(self._call("2026-10-20T11:00:02Z", self.repo))
        r = self._lookup(policy=self._hook_call("S5", self.a, self.b))
        self.assertEqual(r["session"]["session"], "S5")
        self.assertEqual(r["other_sessions"], ["S4"])

    # CLI

    def test_coverage_and_export_cli(self):
        args = ["--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                "--transcripts", self.transcripts]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chain.main(["coverage", *args])
        self.assertIn("commits: 2", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chain.main(["export", *args])
        rows = [json.loads(l) for l in buf.getvalue().splitlines()]
        self.assertEqual({r["commit"]["sha"] for r in rows}, {self.a, self.b})


class DeploymentLinkTest(unittest.TestCase):
    """Loop 1: commit -> deployment, from the deploy log rows start, end and check."""

    def _rows(self, *lines):
        p = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        self.addCleanup(os.unlink, p.name)
        p.write("\n".join(lines) + "\n")
        p.close()
        return chain.deploy_rows(p.name)

    def _commit(self, sha="5eed000aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
        return {"sha": sha, "parents": [], "commit_time": T_A, "author_time": T_A, "subject": "s"}

    START = "start\t2026-10-20T09:54:25Z\t5eed000\tc0ffee0000000001\tpod/a"
    END = "end\t2026-10-20T09:54:30Z\t2026-10-20T09:54:38Z\tc0ffee0000000001\tpod/b"
    CHECK = "check\t2026-10-20T09:54:40Z\tok csp GET-/=200 ua=datapce-deploy-check/5eed000"

    def test_start_end_same_hash_is_established(self):
        d = chain.deployment_link(self._commit(), self._rows(self.START, self.END, self.CHECK))
        self.assertEqual(d["label"], "ESTABLISHED")
        self.assertEqual(d["configmap"], "c0ffee0000000001")
        self.assertEqual(d["pod"], "pod/b")
        self.assertTrue(d["checked"])

    def test_start_only_is_inferred(self):
        d = chain.deployment_link(self._commit(), self._rows(self.START))
        self.assertEqual(d["label"], "INFERRED")
        self.assertFalse(d["checked"])

    def test_end_with_other_hash_is_inferred(self):
        end = self.END.replace("c0ffee0000000001", "ffffffffffffffff")
        d = chain.deployment_link(self._commit(), self._rows(self.START, end))
        self.assertEqual(d["label"], "INFERRED")

    def test_check_alone_is_inferred(self):
        d = chain.deployment_link(self._commit(), self._rows(self.CHECK))
        self.assertEqual(d["label"], "INFERRED")

    def test_other_commit_is_missing(self):
        d = chain.deployment_link(self._commit("1234567" + "0" * 33), self._rows(self.START, self.END))
        self.assertEqual(d["label"], "MISSING")
        self.assertIsNone(d["configmap"])

    def test_short_prefix_is_not_a_match(self):
        short = self.START.replace("5eed000", "5eed")
        d = chain.deployment_link(self._commit(), self._rows(short, self.END))
        self.assertEqual(d["label"], "MISSING")

    def test_end_before_start_does_not_count(self):
        end = self.END.replace("2026-10-20T09:54:30Z", "2026-10-20T09:00:00Z")
        d = chain.deployment_link(self._commit(), self._rows(self.START, end))
        self.assertEqual(d["label"], "INFERRED")

    def test_bad_rows_are_skipped(self):
        d = chain.deployment_link(self._commit(), self._rows("junk", "start\tnot-a-time\t5eed000\th\tp",
                                                            self.START, self.END))
        self.assertEqual(d["label"], "ESTABLISHED")

    def test_missing_file_gives_missing(self):
        self.assertEqual(chain.deploy_rows("/nonexistent/deploys.tsv"), [])

    def test_latest_deployment_wins(self):
        later = "start\t2026-10-20T12:00:00Z\t5eed000\tabc0000000000000\tpod/c"
        d = chain.deployment_link(self._commit(), self._rows(self.START, self.END, later))
        self.assertEqual(d["label"], "INFERRED")
        self.assertEqual(d["count"], 2)


class DeploymentInLookupTest(unittest.TestCase):
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def test_lookup_reports_deployment(self):
        p = os.path.join(self.tmp.name, "deploys.tsv")
        with open(p, "w") as f:
            f.write(f"start\t2026-10-20T12:00:00Z\t{self.b[:7]}\th1\tpod/a\n"
                    f"end\t2026-10-20T12:00:05Z\t2026-10-20T12:00:09Z\th1\tpod/b\n")
        r = chain.lookup(self.repo, self.b[:8], otel={}, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy={}, home=self.home, managed=self.managed, deploys=chain.deploy_rows(p))
        self.assertEqual(r["deployment"]["label"], "ESTABLISHED")
        r = chain.lookup(self.repo, self.a[:8], otel={}, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy={}, home=self.home, managed=self.managed, deploys=chain.deploy_rows(p))
        self.assertEqual(r["deployment"]["label"], "MISSING")
        self.assertIn("commit -> deployment", r["link_gaps"])
        self.assertNotIn("commit -> deployment", r["gaps"])

    def test_coverage_counts_deployment(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chain.main(["coverage", "--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                        "--transcripts", self.transcripts, "--deploys", "/nonexistent"])
        self.assertRegex(out.getvalue(), r"deployment MISSING\s+2")


class CompletenessTest(unittest.TestCase):
    """Loop 2: population, period and completeness (source windows) in coverage and lookup."""
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def _sources(self):
        otel = {"s1": [{"t": chain._ts("2026-10-20T10:00:05+00:00"), "rev": "x"},
                       {"t": chain._ts("2026-10-20T12:00:00+00:00"), "rev": "y"}]}
        pol = {"s1": [{"t": chain._ts("2026-10-20T09:00:00+00:00")}, {"t": chain._ts("2026-10-20T10:30:00+00:00")}]}
        deps = [{"sha": "abcdef0", "t": chain._ts("2026-10-20T11:30:00+00:00"), "ts": "x", "end": None, "check": None}]
        return otel, pol, deps

    def test_source_windows(self):
        otel, pol, deps = self._sources()
        w = chain.source_windows(otel, chain.TranscriptIndex(self.transcripts), pol, deps)
        self.assertEqual(w["otel"]["rows"], 2)
        self.assertEqual(w["otel"]["first"], "2026-10-20T10:00:05+00:00")
        self.assertEqual(w["policy"]["last"], "2026-10-20T10:30:00+00:00")
        self.assertEqual(w["deploys"]["rows"], 1)
        self.assertEqual(w["transcripts"]["rows"], 0)
        self.assertIsNone(w["transcripts"]["first"])

    def test_lookup_completeness_flags_commit_inside_or_outside(self):
        otel, pol, deps = self._sources()
        r = chain.lookup(self.repo, self.b[:8], otel=otel, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy={}, home=self.home, managed=self.managed, deploys=[])
        # T_B is 11:00; the otel window is 10:00:05 to 12:00 and holds it. No policy rows: no window.
        self.assertTrue(r["completeness"]["otel"]["commit_in_window"])
        self.assertFalse(r["completeness"]["policy"]["commit_in_window"])
        self.assertFalse(r["completeness"]["deploys"]["commit_in_window"])
        r = chain.lookup(self.repo, self.a[:8], otel=otel, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy={}, home=self.home, managed=self.managed, deploys=[])
        self.assertFalse(r["completeness"]["otel"]["commit_in_window"])   # T_A 10:00 is before 10:00:05

    def test_coverage_prints_population_period_completeness(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chain.main(["coverage", "--repo", self.repo, "--since", "2020-01-01", "--otel", "/nonexistent",
                        "--policy", "/nonexistent", "--transcripts", self.transcripts, "--deploys", "/nonexistent"])
        text = out.getvalue()
        self.assertRegex(text, r"population: refs=branches\+tags commits=2 merges=0 repo=repo")
        self.assertRegex(text, r"period: since=2020-01-01 first=2026-10-20T10:00:00\+00:00 last=2026-10-20T11:00:00\+00:00")
        self.assertRegex(text, r"source otel\s+rows=0 first=- last=-")
        self.assertRegex(text, r"in window: otel 0 of 2")

    def test_coverage_in_window_counts_commits(self):
        p = os.path.join(self.tmp.name, "deploys.tsv")
        with open(p, "w") as f:
            f.write("start\t2026-10-20T10:30:00Z\tabcdef0\th1\tpod/a\nstart\t2026-10-20T11:30:00Z\tabcdef1\th1\tpod/a\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chain.main(["coverage", "--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                        "--transcripts", self.transcripts, "--deploys", p])
        self.assertRegex(out.getvalue(), r"deploys 1 of 2")

    def test_no_commit_text_in_completeness(self):
        otel, pol, deps = self._sources()
        r = chain.lookup(self.repo, self.b[:8], otel=otel, transcripts=chain.TranscriptIndex(self.transcripts),
                         policy={}, home=self.home, managed=self.managed, deploys=[])
        self.assertNotIn("second change", json.dumps(r["completeness"]))


class ForgeLinkTest(unittest.TestCase):
    """Loop 3: commit -> PR and PR -> reviews and checks are MISSING, with a gap reason."""
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def _lookup(self, **kw):
        return chain.lookup(self.repo, self.b[:8], otel={}, transcripts=chain.TranscriptIndex(self.transcripts),
                            policy={}, home=self.home, managed=self.managed, deploys=[], **kw)

    def test_no_remote_reason(self):
        r = self._lookup()
        for k in ("pr", "reviews", "checks"):
            self.assertEqual(r[k]["label"], "MISSING")
            self.assertEqual(r[k]["reason"], "no remote")
        self.assertIn("commit -> PR: no remote", r["link_gaps"])
        self.assertIn("PR -> reviews: no remote", r["link_gaps"])
        self.assertIn("PR -> checks: no remote", r["link_gaps"])

    def test_remote_without_gh_login_reason(self):
        self._git("remote", "add", "origin", "https://example.invalid/x/y.git")
        r = self._lookup(gh_hosts="/nonexistent/hosts.yml")
        self.assertEqual(r["pr"]["reason"], "gh not authenticated")
        self.assertIn("commit -> PR: gh not authenticated", r["link_gaps"])

    def test_remote_with_gh_login_file_is_not_established(self):
        self._git("remote", "add", "origin", "https://example.invalid/x/y.git")
        hosts = os.path.join(self.tmp.name, "hosts.yml")
        with open(hosts, "w") as f:
            f.write("github.com:\n  oauth_token: " + SECRET + "\n")
        r = self._lookup(gh_hosts=hosts)
        self.assertEqual(r["pr"]["label"], "MISSING")
        self.assertEqual(r["pr"]["reason"], "gh not queried")
        self.assertNotIn(SECRET, json.dumps(r))

    def test_remote_url_is_not_printed(self):
        self._git("remote", "add", "origin", "https://user:" + SECRET + "@example.invalid/x/y.git")
        r = self._lookup(gh_hosts="/nonexistent/hosts.yml")
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertNotIn(SECRET, chain.render(r))

    def test_render_shows_pr_rows(self):
        text = chain.render(self._lookup())
        self.assertRegex(text, r"pr\s+-\s+MISSING\s+no PR record: no remote")

    def test_coverage_counts_forge_links_missing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            chain.main(["coverage", "--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                        "--transcripts", self.transcripts, "--deploys", "/nonexistent"])
        for k in ("pr", "reviews", "checks"):
            self.assertRegex(out.getvalue(), rf"{k}\s+MISSING\s+2")
        self.assertIn("forge: no remote", out.getvalue())


def _otel_calls(session, calls):
    """calls: list of (iso time, tool, rev or None). Adds one tool_result event for each."""
    recs = []
    for i, (ts, tool, rev) in enumerate(calls):
        a = {"session.id": session, "event.name": "tool_result", "event.timestamp": ts, "event.sequence": i,
             "tool_name": tool, "tool_use_id": f"toolu_{i}", "success": "true"}
        if rev:
            a["vcs.ref.head.revision"] = rev
        recs.append({"attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in a.items()]})
    return json.dumps({"ts": "x", "payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}})


class OutsideGitTest(unittest.TestCase):
    """Loop 4: count of agent tool calls with no commit seen in the call, per session, from OTel. INFERRED."""

    def _activity(self, *lines):
        p = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, p.name)
        p.write("\n".join(lines) + "\n")
        p.close()
        return chain.otel_activity(p.name)

    CALLS = [("2026-10-20T10:00:00Z", "Read", None), ("2026-10-20T10:00:01Z", "Bash", None),
             ("2026-10-20T10:00:02Z", "Bash", "aaa"), ("2026-10-20T10:00:03Z", "Edit", "aaa"),
             ("2026-10-20T10:00:04Z", "Bash", "bbb"), ("2026-10-20T10:00:05Z", "Write", None)]

    def test_counts_by_session(self):
        s = chain.outside_git(self._activity(_otel_calls("s1", self.CALLS)))["s1"]
        self.assertEqual(s["tool_calls"], 6)
        self.assertEqual(s["commit_seen"], 2)         # HEAD changed: aaa (first rev seen) and bbb
        self.assertEqual(s["outside_git"], 4)
        self.assertEqual(s["by_tool"], {"Read": 1, "Bash": 3, "Edit": 1, "Write": 1})
        self.assertEqual(s["label"], "INFERRED")

    def test_first_and_last_time(self):
        s = chain.outside_git(self._activity(_otel_calls("s1", self.CALLS)))["s1"]
        self.assertEqual((s["first"], s["last"]), ("2026-10-20T10:00:00+00:00", "2026-10-20T10:00:05+00:00"))

    def test_sessions_are_separate(self):
        o = chain.outside_git(self._activity(_otel_calls("s1", self.CALLS[:2]), _otel_calls("s2", self.CALLS[:1])))
        self.assertEqual((o["s1"]["tool_calls"], o["s2"]["tool_calls"]), (2, 1))

    def test_other_events_and_bad_lines_are_skipped(self):
        bad = json.dumps({"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [
            {"key": "event.name", "value": {"stringValue": "api_request"}}, {"key": "session.id",
             "value": {"stringValue": "s1"}}]}]}]}]}})
        o = chain.outside_git(self._activity("not json", bad, _otel_calls("s1", self.CALLS[:1])))
        self.assertEqual(o["s1"]["tool_calls"], 1)

    def test_missing_file(self):
        self.assertEqual(chain.otel_activity("/nonexistent/x.jsonl"), {})

    def test_output_has_no_ids_of_calls(self):
        s = chain.outside_git(self._activity(_otel_calls("s1", self.CALLS)))["s1"]
        self.assertNotIn("toolu_", json.dumps(s))


class OutsideGitInChainTest(unittest.TestCase):
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def _lookup(self, sha, otel_lines, outside):
        p = os.path.join(self.tmp.name, "logs.jsonl")
        with open(p, "w") as f:
            f.write("\n".join(otel_lines) + "\n")
        return chain.lookup(self.repo, sha[:8], otel=chain.otel_tool_results(p),
                            transcripts=chain.TranscriptIndex(self.transcripts), policy={}, home=self.home,
                            managed=self.managed, deploys=[], outside=outside)

    def test_lookup_shows_session_activity(self):
        calls = [("2026-10-20T10:59:00Z", "Bash", self.a), ("2026-10-20T10:59:30Z", "Read", None),
                 ("2026-10-20T11:00:20Z", "Bash", self.b)]
        p = os.path.join(self.tmp.name, "a.jsonl")
        with open(p, "w") as f:
            f.write(_otel_calls("s1", calls) + "\n")
        outside = chain.outside_git(chain.otel_activity(p))
        r = self._lookup(self.b, [_otel_calls("s1", calls)], outside)
        self.assertEqual(r["session"]["session"], "s1")
        self.assertEqual(r["outside_git"]["tool_calls"], 3)
        self.assertEqual(r["outside_git"]["outside_git"], 1)
        self.assertEqual(r["outside_git"]["label"], "INFERRED")

    def test_lookup_without_otel_session_has_none(self):
        r = self._lookup(self.b, [], {})
        self.assertIsNone(r["outside_git"])

    def test_coverage_prints_activity_line(self):
        out = io.StringIO()
        p = os.path.join(self.tmp.name, "logs.jsonl")
        with open(p, "w") as f:
            f.write(_otel_calls("s1", OutsideGitTest.CALLS) + "\n")
        with contextlib.redirect_stdout(out):
            chain.main(["coverage", "--repo", self.repo, "--otel", p, "--policy", "/nonexistent",
                        "--transcripts", self.transcripts, "--deploys", "/nonexistent"])
        self.assertIn("outside git (INFERRED): sessions=1 tool_calls=6 commit_seen=2 outside_git=4", out.getvalue())


class DemoTest(unittest.TestCase):
    """Loop 5: `demo` picks a deployed commit and prints its full chain and gaps."""
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def _deploys(self, *lines):
        p = os.path.join(self.tmp.name, "deploys.tsv")
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def _demo(self, deploys, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chain.main(["demo", "--repo", self.repo, "--otel", "/nonexistent", "--policy", "/nonexistent",
                             "--transcripts", self.transcripts, "--deploys", deploys, *extra])
        return rc, out.getvalue()

    def _pair(self, sha, t, h="h1"):
        return [f"start\t{t}\t{sha[:7]}\t{h}\tpod/a", f"end\t{t}\t{t}\t{h}\tpod/b"]

    def test_picks_latest_deployed_commit(self):
        d = self._deploys(*self._pair(self.a, "2026-10-20T12:00:00Z", "h1"),
                          *self._pair(self.b, "2026-10-20T13:00:00Z", "h2"))
        rc, text = self._demo(d)
        self.assertEqual(rc, 0)
        self.assertIn(f"demo commit: {self.b[:12]}", text)
        self.assertRegex(text, r"deploy\s+h2\s+ESTABLISHED")

    def test_skips_commit_not_in_repo(self):
        d = self._deploys(*self._pair(self.a, "2026-10-20T12:00:00Z"),
                          *self._pair("deadbee" + "0" * 33, "2026-10-20T13:00:00Z", "h2"))
        rc, text = self._demo(d)
        self.assertIn(f"demo commit: {self.a[:12]}", text)

    def test_prints_every_link_and_gaps(self):
        d = self._deploys(*self._pair(self.b, "2026-10-20T13:00:00Z"))
        rc, text = self._demo(d)
        for word in ("commit ", "session ", "policy ", "deploy ", "pr ", "reviews ", "checks "):
            self.assertIn(word, text)
        self.assertIn("gaps: commit -> session", text)
        self.assertIn("link gaps: commit -> PR: no remote", text)

    def test_no_deployed_commit(self):
        rc, text = self._demo(self._deploys("check\t2026-10-20T12:00:00Z\tok"))
        self.assertEqual(rc, 1)
        self.assertIn("no deployed commit", text)

    def test_sha_option_overrides_pick(self):
        d = self._deploys(*self._pair(self.b, "2026-10-20T13:00:00Z"))
        rc, text = self._demo(d, "--sha", self.a[:8])
        self.assertIn(f"demo commit: {self.a[:12]}", text)

    def test_start_without_end_is_not_picked(self):
        d = self._deploys(f"start\t2026-10-20T13:00:00Z\t{self.b[:7]}\th2\tpod/a",
                          *self._pair(self.a, "2026-10-20T12:00:00Z"))
        rc, text = self._demo(d)
        self.assertIn(f"demo commit: {self.a[:12]}", text)

    def test_demo_prints_no_commit_text_beyond_subject(self):
        d = self._deploys(*self._pair(self.b, "2026-10-20T13:00:00Z"))
        rc, text = self._demo(d)
        self.assertNotIn(SECRET, text)


class StateDirTest(unittest.TestCase):
    """G2: default inputs live in one state dir, never in sibling dirs of the script."""
    setUp, tearDown, _git, _commit, _write_settings = (
        ChainTest.setUp, ChainTest.tearDown, ChainTest._git, ChainTest._commit, ChainTest._write_settings)

    def _env(self, **kw):
        env = {k: v for k, v in os.environ.items() if k not in ("CHANGE_CHAIN_DIR", "XDG_STATE_HOME")}
        env.update(HOME=self.home, **kw)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_change_chain_dir_wins(self):
        with self._env(CHANGE_CHAIN_DIR="/x/cc", XDG_STATE_HOME="/x/state"):
            self.assertEqual(chain.state_dir(), "/x/cc")

    def test_xdg_state_home_next(self):
        with self._env(XDG_STATE_HOME="/x/state"):
            self.assertEqual(chain.state_dir(), os.path.join("/x/state", "change-chain"))

    def test_relative_or_empty_xdg_is_ignored(self):
        for v in ("", "rel/state"):
            with self._env(XDG_STATE_HOME=v):
                self.assertEqual(chain.state_dir(), os.path.join(self.home, ".local", "state", "change-chain"))

    def test_home_fallback(self):
        with self._env():
            self.assertEqual(chain.state_dir(), os.path.join(self.home, ".local", "state", "change-chain"))

    def test_default_paths_are_in_state_dir(self):
        with self._env(CHANGE_CHAIN_DIR="/x/cc"):
            p = chain.default_paths()
        self.assertEqual(p["otel"], os.path.join("/x/cc", "otel.jsonl"))
        self.assertEqual(p["policy"], os.path.join("/x/cc", "policy.jsonl"))
        self.assertEqual(p["deploys"], os.path.join("/x/cc", "deploys.tsv"))
        self.assertEqual(p["transcripts"], os.path.join(self.home, ".claude", "projects"))
        self.assertEqual(p["gh_hosts"], os.path.join(self.home, ".config", "gh", "hosts.yml"))
        # Every default path has a known root: the state dir for the data files, $HOME for the
        # Claude Code transcripts and the gh login file. No path is relative or next to the script.
        roots = {"otel": "/x/cc", "policy": "/x/cc", "deploys": "/x/cc",
                 "transcripts": self.home, "gh_hosts": self.home}
        self.assertEqual(set(p), set(roots))
        for k, v in p.items():
            self.assertTrue(os.path.isabs(v), k)
            self.assertEqual(os.path.commonpath([roots[k], v]), roots[k], k)
            self.assertNotEqual(v, roots[k], k)

    def _coverage(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = chain.main(["coverage", "--repo", self.repo, "--transcripts", self.transcripts, *extra])
        return rc, out.getvalue()

    def test_main_reads_defaults_from_state_dir(self):
        st = os.path.join(self.tmp.name, "state")
        os.makedirs(st)
        files = policy_snapshot.snapshot_files(self.repo, self.home, self.managed)
        with open(os.path.join(st, "policy.jsonl"), "w") as f:
            f.write(json.dumps({"ts": T_A, "session_id": "S1", "cwd": self.repo, "files": files,
                                "digest": policy_snapshot.digest(files)}) + "\n")
        with open(os.path.join(st, "deploys.tsv"), "w") as f:
            f.write(f"start\t{T_B}\t{self.b[:7]}\th1\tpod/a\nend\t{T_B}\t{T_B}\th1\tpod/b\n")
        with self._env(CHANGE_CHAIN_DIR=st):
            rc, text = self._coverage()
        self.assertEqual(rc, 0)
        self.assertRegex(text, r"source policy\s+rows=1 ")
        self.assertRegex(text, r"source deploys\s+rows=1 ")

    def test_flags_override_state_dir(self):
        with self._env(CHANGE_CHAIN_DIR=os.path.join(self.tmp.name, "state")):
            rc, text = self._coverage("--policy", "/nonexistent", "--deploys", "/nonexistent", "--otel", "/nonexistent")
        self.assertEqual(rc, 0)
        self.assertRegex(text, r"source policy\s+rows=0 ")

    def test_gh_hosts_flag(self):
        self._git("remote", "add", "origin", "https://example.invalid/x/y.git")
        hosts = os.path.join(self.tmp.name, "hosts.yml")
        with open(hosts, "w") as f:
            f.write("github.com: {}\n")
        with self._env(CHANGE_CHAIN_DIR=os.path.join(self.tmp.name, "state")):
            self.assertIn("forge: gh not authenticated", self._coverage("--gh-hosts", "/nonexistent")[1])
            self.assertIn("forge: gh not queried", self._coverage("--gh-hosts", hosts)[1])


class HygieneTest(unittest.TestCase):
    """G10: every file is opened in a `with` block, so no ResourceWarning."""

    def test_no_unclosed_open(self):
        here = os.path.dirname(os.path.abspath(__file__))
        bare = []
        for name in sorted(os.listdir(here)):
            if name.endswith(".py"):
                with open(os.path.join(here, name), encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(r"(?<![\w.])open\([^)]*\)\.(write|read)", line):
                            bare.append(f"{name}:{i}")
        self.assertEqual(bare, [])


class ExampleDemoTest(unittest.TestCase):
    """G6: examples/make_demo.py builds synthetic inputs; chain.py runs on them with exit 0."""
    HERE = os.path.dirname(os.path.abspath(__file__))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "emptyhome")
        os.makedirs(self.home)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CHANGE_CHAIN_DIR", "XDG_STATE_HOME", "DATAPCE_POLICY_DIR")}
        self.env.update(HOME=self.home, TMPDIR=self.tmp.name)

    def run_py(self, *args, **env):
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=self.tmp.name,
                              env=dict(self.env, **env), timeout=60)

    def build(self):
        out = os.path.join(self.tmp.name, "demo")
        r = self.run_py(os.path.join(self.HERE, "examples", "make_demo.py"), "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out, r.stdout

    def chain(self, out, *args):
        return self.run_py(os.path.join(self.HERE, "chain.py"), *args, "--repo", os.path.join(out, "repo"),
                           "--transcripts", os.path.join(out, "projects"),
                           "--gh-hosts", os.path.join(out, "no-gh-hosts.yml"),
                           CHANGE_CHAIN_DIR=os.path.join(out, "state"))

    def test_builds_inputs(self):
        out, text = self.build()
        for name in ("otel.jsonl", "policy.jsonl", "deploys.tsv"):
            self.assertTrue(os.path.getsize(os.path.join(out, "state", name)) > 0, name)
        self.assertTrue(os.path.isdir(os.path.join(out, "repo", ".git")))
        self.assertIn("chain.py demo", text)

    def test_demo_runs_on_output(self):
        out, _ = self.build()
        r = self.chain(out, "demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        for link in ("session", "policy", "deploy"):
            self.assertRegex(r.stdout, rf"(?m)^{link}\s+\S+\s+ESTABLISHED")
        self.assertRegex(r.stdout, r"(?m)^outside\s+-\s+INFERRED")
        self.assertIn("gap count: 3", r.stdout)

    def test_lookup_and_coverage_run_on_output(self):
        out, _ = self.build()
        r = self.chain(out, "lookup", "HEAD~1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"(?m)^session\s+-\s+MISSING")
        r = self.chain(out, "coverage")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("commits: 2", r.stdout)
        self.assertRegex(r.stdout, r"session\s+ESTABLISHED\s+1\s+50.0%")

    def test_default_out_is_a_new_temp_dir(self):
        r = self.run_py(os.path.join(self.HERE, "examples", "make_demo.py"))
        self.assertEqual(r.returncode, 0, r.stderr)
        made = [n for n in os.listdir(self.tmp.name) if n.startswith("change-chain-demo-")]
        self.assertEqual(len(made), 1)

    def test_out_dir_must_be_empty(self):
        out = os.path.join(self.tmp.name, "demo")
        os.makedirs(out)
        with open(os.path.join(out, "keep.txt"), "w") as f:
            f.write("x")
        r = self.run_py(os.path.join(self.HERE, "examples", "make_demo.py"), "--out", out)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(out), ["keep.txt"])


class MainBlockLastTest(unittest.TestCase):
    """G14 review: `python3 test_x.py` must run every class, so `unittest.main()` must come after them all."""

    def test_main_block_is_after_every_class_and_function(self):
        here = os.path.dirname(os.path.abspath(__file__))
        late = []
        for name in sorted(n for n in os.listdir(here) if n.startswith("test_") and n.endswith(".py")):
            with open(os.path.join(here, name), encoding="utf-8") as f:
                lines = f.read().splitlines()
            mains = [i for i, l in enumerate(lines) if l.startswith("if __name__ ==")]
            self.assertEqual(len(mains), 1, name)
            late += [f"{name}:{i + 1}" for i, l in enumerate(lines)
                     if i > mains[0] and re.match(r"(class|def) ", l)]
        self.assertEqual(late, [])


class NullTimeTest(unittest.TestCase):
    """G14 review: a row with a null time is a bad line. It is skipped; it does not crash the sort or a link."""

    def _file(self, *lines):
        p = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, p.name)
        p.write("\n".join(lines) + "\n")
        p.close()
        return p.name

    def test_policy_row_with_null_ts_is_skipped(self):
        path = self._file(json.dumps({"ts": None, "session_id": "S1", "digest": "d0"}),
                          json.dumps({"ts": T_A, "session_id": "S1", "digest": "d1"}),
                          json.dumps({"ts": None, "session_id": "S2", "digest": "d2"}))
        rows = chain.policy_rows(path)
        self.assertEqual([r["digest"] for r in rows["S1"]], ["d1"])
        self.assertNotIn("S2", rows)

    def _otel_null_ts(self, session):
        rec = {"attributes": [{"key": k, "value": {"stringValue": v}} for k, v in {
            "session.id": session, "event.name": "tool_result", "event.timestamp": None,
            "tool_name": "Bash", "vcs.ref.head.revision": "a" * 40}.items()]}
        return json.dumps({"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": [rec]}]}]}})

    def test_otel_event_with_null_timestamp_is_skipped(self):
        path = self._file(self._otel_null_ts("S1"), _otel_line("S1", [(T_A, "b" * 40)]),
                          self._otel_null_ts("S2"))
        evs = chain.otel_tool_results(path)
        self.assertEqual([e["rev"] for e in evs["S1"]], ["b" * 40])
        self.assertNotIn("S2", evs)
        act = chain.otel_activity(path)
        self.assertEqual(len(act["S1"]), 1)
        self.assertNotIn("S2", act)


class DeployEndOrderTest(unittest.TestCase):
    """G14 review: an end row joins the earliest start that it can follow, not the first row in the file."""

    def test_end_rows_out_of_file_order_join_their_own_start(self):
        p = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        self.addCleanup(os.unlink, p.name)
        p.write("\n".join([
            "start\t2026-10-20T09:00:00Z\taaaaaaa\tcm-1\tpod/a",
            "start\t2026-10-20T10:00:00Z\tbbbbbbb\tcm-1\tpod/b",
            "end\t2026-10-20T10:00:00Z\t2026-10-20T10:01:00Z\tcm-1\tpod/b",
            "end\t2026-10-20T09:00:00Z\t2026-10-20T09:01:00Z\tcm-1\tpod/a"]) + "\n")
        p.close()
        deps = {d["sha"]: d for d in chain.deploy_rows(p.name)}
        self.assertEqual(deps["aaaaaaa"]["end"]["pod"], "pod/a")
        self.assertEqual(deps["bbbbbbb"]["end"]["pod"], "pod/b")


class CliErrorTest(unittest.TestCase):
    """G14 review: a bad SHA or a path that is not a repo gives one error line and exit 2, not a traceback."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.join(tmp.name, "repo")
        self.transcripts = os.path.join(tmp.name, "projects")
        os.makedirs(self.transcripts)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True, env=env)
        subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", "first"], check=True, env=env)

    def _main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = chain.main([*args, "--otel", "/nonexistent", "--policy", "/nonexistent",
                               "--deploys", "/nonexistent", "--transcripts", self.transcripts,
                               "--gh-hosts", "/nonexistent"])
        return code, out.getvalue(), err.getvalue()

    def test_unknown_sha_is_an_error_line(self):
        code, out, err = self._main("lookup", "0000000", "--repo", self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("chain.py: error: "), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_path_that_is_not_a_repo_is_an_error_line(self):
        not_repo = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, not_repo)
        for cmd in (["lookup", "HEAD"], ["coverage"], ["export"], ["demo"]):
            code, out, err = self._main(*cmd, "--repo", not_repo)
            self.assertEqual(code, 2, cmd)
            self.assertTrue(err.startswith("chain.py: error: "), (cmd, err))


if __name__ == "__main__":
    unittest.main()
