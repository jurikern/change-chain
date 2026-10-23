# H3 results: recall of published Claude Code commit signals on change-chain agent commits

Prereg: `prereg-h3.md`, SHA-256 `927c2796f2e62f34c8f2a788a550c66f2ff0883b39d1c83e9dfc3244a50a1d84`, commit `fa5bab3`.
Run: 2026-10-23 (UTC), `run.py`, the change-chain development tree at `7dd4518`, read-only. Counts only.
Paper: Khosravani and Mockus, arXiv 2606.24429v1.

## 1. Population

| Item | Value |
|---|---|
| Window (commit time) | 2026-10-21T07:00:00Z to 2026-10-23T03:00:00Z |
| Repos in frame | 45 (0 with a change-chain error) |
| Unique commits (dedup by SHA) | 169 |
| Label disagreements across worktrees | 0 |
| ESTABLISHED (ground truth) | 106 |
| INFERRED | 42 |
| MISSING | 21 |
| ESTABLISHED merge commits | 0 |

| Repo | ESTABLISHED | INFERRED | MISSING |
|---|---|---|---|
| datapce | 54 | 7 | 0 |
| other-9 | 52 | 16 | 11 |
| change-chain-release | 0 | 12 | 1 |
| datapce-scan-public | 0 | 0 | 3 |
| change-chain-public | 0 | 0 | 1 |
| other-1 | 0 | 0 | 5 |
| other-30, other-31, other-38, other-39 | 0 | 7 | 0 |

## 2. Recall on ESTABLISHED commits (n = 106), Wilson 95 %

| Signal | Rule | k | Recall | 95 % interval |
|---|---|---|---|---|
| S1 | author e-mail contains `noreply@anthropic.com` | 0 | 0.000 | 0.000 - 0.035 |
| S2 | literal `Co-authored-by: Claude <noreply@anthropic.com>` | 0 | 0.000 | 0.000 - 0.035 |
| S3 | literal `Generated with Claude Code` | 0 | 0.000 | 0.000 - 0.035 |
| U | S1 or S2 or S3 | 0 | 0.000 | 0.000 - 0.035 |
| S2L | lenient trailer (model name allowed) | 94 | 0.887 | 0.813 - 0.934 |
| S3L | lenient footer (brackets allowed) | 0 | 0.000 | 0.000 - 0.035 |
| UL | S1 or S2L or S3L | 94 | 0.887 | 0.813 - 0.934 |

## 3. Comparison rows (signal rate, not recall)

| Label | n | U k | UL k | UL rate | 95 % interval |
|---|---|---|---|---|---|
| INFERRED | 42 | 0 | 28 | 0.667 | 0.516 - 0.790 |
| MISSING | 21 | 0 | 5 | 0.238 | 0.106 - 0.451 |

The 5 MISSING commits with a trailer are agent commits that change-chain does not link. They are not in the ground truth.

## 4. ESTABLISHED commits that no signal finds

| Union | Missed | Share | M1 trailer has model name | M2 footer has brackets | M3 merge | M4 no signal text | M5 other |
|---|---|---|---|---|---|---|---|
| U (literal) | 106 | 1.000 | 94 | 0 | 0 | 12 | 0 |
| UL (lenient) | 12 | 0.113 | 0 | 0 | 0 | 12 | 0 |

Main miss reason for U: M1. Every agent trailer has the form `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. The literal string has no model name between "Claude" and the address.

## 5. Predictions

| ID | Prediction | Result |
|---|---|---|
| P1 | S1 Wilson upper bound below 0.10 | Met (0.035) |
| P2 | S2 recall below 0.20 | Met (0.000) |
| P3 | UL recall 0.80 or more | Met (0.887; lower bound 0.813) |
| P4 | Main miss reason for U is M1 | Met (94 of 106) |

## 6. Exploratory (not preregistered)

| Item | Count |
|---|---|
| Distinct trailer names in S2L hits | 1 (`Claude Opus 5.5`, 94) |
| Session link source of ESTABLISHED commits | hook-head 106, OTel 0 |
| Distinct sessions behind the 106 ESTABLISHED commits | 2 |
| Sessions with 1 or more M4 misses | 1 |
| M4 misses: repo | other-9: 12 |
| M4 misses: any co-author line | 0 |
| M4 misses: the word "claude" anywhere in the message | 0 |

The cause of the 12 M4 commits is not determined. They come from one session in one repo. They carry no attribution text of any kind.

## 7. Limits

1. One operator, one agent (Claude Code), one model name, a window of about 2 days, local repos only.
2. The 106 ESTABLISHED commits come from 2 sessions. Commits in a session are not independent. The Wilson intervals are too narrow for that reason.
3. The literal rows test the strings as printed in the paper. The paper does not print its regexes. Its implemented regex can match the model-name form. The lenient rows estimate that case.
4. The owner's setup adds the trailer by default. Other users can turn attribution off. Recall in the wild can be lower.
5. The ground truth depends on change-chain hook rows. These rows are unsigned local files.
6. No footer (S3, S3L) appears in commits. In this setup, the attribution rule puts the `Generated with [Claude Code]` footer in PR descriptions only (INFERRED from the session attribution rule).
