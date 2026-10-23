"""Tests for the H3 signal matchers. Written before signals.py. Synthetic data only."""
import unittest

import signals as s

AGENT_MSG_MODEL = "fix: thing\n\nbody\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
AGENT_MSG_PLAIN = "fix: thing\n\nCo-authored-by: Claude <noreply@anthropic.com>\n"
FOOTER_BRACKETS = "feat: x\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
FOOTER_PLAIN = "feat: x\n\nGenerated with Claude Code\n"
HUMAN_MSG = "docs: update readme\n"


class S1Author(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(s.s1("noreply@anthropic.com"))

    def test_case_insensitive_substring(self):
        self.assertTrue(s.s1("Claude <NoReply@Anthropic.com>"))

    def test_human(self):
        self.assertFalse(s.s1("someone@example.org"))

    def test_none(self):
        self.assertFalse(s.s1(None))


class S2Trailer(unittest.TestCase):
    def test_plain_trailer(self):
        self.assertTrue(s.s2(AGENT_MSG_PLAIN))

    def test_case_insensitive(self):
        self.assertTrue(s.s2("x\n\nCO-AUTHORED-BY: CLAUDE <NOREPLY@ANTHROPIC.COM>"))

    def test_model_name_is_not_literal_match(self):
        self.assertFalse(s.s2(AGENT_MSG_MODEL))

    def test_lenient_accepts_model_name(self):
        self.assertTrue(s.s2l(AGENT_MSG_MODEL))

    def test_lenient_accepts_plain(self):
        self.assertTrue(s.s2l(AGENT_MSG_PLAIN))

    def test_lenient_rejects_other_coauthor(self):
        self.assertFalse(s.s2l("x\n\nCo-authored-by: Bob <bob@example.org>"))

    def test_lenient_rejects_claudette(self):
        self.assertFalse(s.s2l("x\n\nCo-authored-by: Claudette <noreply@anthropic.com>"))

    def test_lenient_does_not_cross_lines(self):
        self.assertFalse(s.s2l("Co-authored-by: Claude\n<noreply@anthropic.com>"))

    def test_human(self):
        self.assertFalse(s.s2(HUMAN_MSG))
        self.assertFalse(s.s2l(HUMAN_MSG))


class S3Footer(unittest.TestCase):
    def test_plain(self):
        self.assertTrue(s.s3(FOOTER_PLAIN))

    def test_brackets_not_literal(self):
        self.assertFalse(s.s3(FOOTER_BRACKETS))

    def test_lenient_brackets(self):
        self.assertTrue(s.s3l(FOOTER_BRACKETS))

    def test_lenient_plain(self):
        self.assertTrue(s.s3l(FOOTER_PLAIN))

    def test_human(self):
        self.assertFalse(s.s3(HUMAN_MSG))
        self.assertFalse(s.s3l(HUMAN_MSG))


class Unions(unittest.TestCase):
    def test_union_literal(self):
        r = s.match_all("someone@example.org", AGENT_MSG_PLAIN)
        self.assertTrue(r["U"])
        self.assertFalse(r["S1"])

    def test_union_literal_misses_model_trailer(self):
        r = s.match_all("someone@example.org", AGENT_MSG_MODEL)
        self.assertFalse(r["U"])
        self.assertTrue(r["UL"])

    def test_union_by_author(self):
        r = s.match_all("noreply@anthropic.com", HUMAN_MSG)
        self.assertTrue(r["U"] and r["UL"] and r["S1"])

    def test_keys(self):
        self.assertEqual(set(s.match_all(None, "")), {"S1", "S2", "S3", "U", "S2L", "S3L", "UL"})


class MissReason(unittest.TestCase):
    def test_m1(self):
        self.assertEqual(s.miss_reason(AGENT_MSG_MODEL, ["a"]), "M1")

    def test_m2(self):
        self.assertEqual(s.miss_reason(FOOTER_BRACKETS, ["a"]), "M2")

    def test_m1_before_m2(self):
        self.assertEqual(s.miss_reason(FOOTER_BRACKETS + AGENT_MSG_MODEL, ["a"]), "M1")

    def test_m3_merge(self):
        self.assertEqual(s.miss_reason("Merge branch 'x'\n", ["a", "b"]), "M3")

    def test_m4_no_text(self):
        self.assertEqual(s.miss_reason(HUMAN_MSG, ["a"]), "M4")

    def test_m4_root_commit(self):
        self.assertEqual(s.miss_reason(HUMAN_MSG, []), "M4")

    def test_m5_other(self):
        self.assertEqual(s.miss_reason("x\n\nCo-authored-by: Claude\n<noreply@anthropic.com>", ["a"]), "M5")

    def test_m5_merge_with_text(self):
        self.assertEqual(s.miss_reason("Merge\n\nmade with claude code tools", ["a", "b"]), "M5")


class Wilson(unittest.TestCase):
    def test_zero_n(self):
        self.assertEqual(s.wilson(0, 0), (None, None))

    def test_known_value(self):
        lo, hi = s.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)

    def test_all(self):
        lo, hi = s.wilson(50, 50)
        self.assertAlmostEqual(hi, 1.0, places=9)
        self.assertAlmostEqual(lo, 0.9287, places=4)

    def test_none(self):
        lo, hi = s.wilson(0, 50)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.0713, places=4)


class BestLabel(unittest.TestCase):
    def test_order(self):
        self.assertEqual(s.best_label(["MISSING", "ESTABLISHED", "INFERRED"]), "ESTABLISHED")
        self.assertEqual(s.best_label(["MISSING", "INFERRED"]), "INFERRED")
        self.assertEqual(s.best_label(["MISSING"]), "MISSING")


class RepoName(unittest.TestCase):
    def test_public_names(self):
        names = s.public_repo_names(["/a/datapce-demo", "/b/zeta", "/c/change-chain-demo", "/d/alpha"])
        self.assertEqual(names["/a/datapce-demo"], "datapce-demo")
        self.assertEqual(names["/c/change-chain-demo"], "change-chain-demo")
        self.assertEqual(names["/d/alpha"], "other-1")
        self.assertEqual(names["/b/zeta"], "other-2")


if __name__ == "__main__":
    unittest.main()
