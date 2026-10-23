# H3 preregistration: recall of published Claude Code commit signals on change-chain agent commits

Date: 2026-10-23 (UTC). Author: agent run for the owner. Status: written before any outcome was computed or looked at.
Mode: local only. Nobody is contacted. No dataset is downloaded.

## 1. Question

Khosravani and Mockus (arXiv 2606.24429v1) detect Claude Code commits with commit-level signals.
Their abstract says: "Absolute recall is unknown".
H3 measures the recall of their signals on commits that change-chain links to a Claude Code session by a machine record.

## 2. Signals under test (from the paper, fetched 2026-10-23)

| ID | Type (paper) | Exact text in paper | Section |
|---|---|---|---|
| S1 | Type A, bot account | "case-insensitive substring match on noreply@anthropic.com" (author) | 4.1 Finding 4; 5.8 item 3 |
| S2 | Type B, message signature | "Co-authored-by: Claude <noreply@anthropic.com>" | 4.1 Finding 4 |
| S3 | Type B, message signature | "a Generated with Claude Code trailer" | 4.1 Finding 4 |

Section 3.2 says Type B uses "case-insensitive regular expressions". The paper does not print the regexes.
Section 3.2 says Type A uses the author identity. The paper names no committer field.

### 2.1 Primary matchers (literal reading)

| ID | Field | Rule |
|---|---|---|
| S1 | author e-mail | case-insensitive substring `noreply@anthropic.com` |
| S2 | full commit message | case-insensitive substring `co-authored-by: claude <noreply@anthropic.com>` |
| S3 | full commit message | case-insensitive substring `generated with claude code` |
| U | - | S1 or S2 or S3 |

### 2.2 Secondary matchers (lenient reading; sensitivity rows)

The paper does not print its regexes. The literal strings can be stricter than the implemented regexes.
These rows estimate a plausible implemented regex. They are not the paper's text.

| ID | Field | Rule (Python `re`, IGNORECASE) |
|---|---|---|
| S2L | full commit message | `co-authored-by:\s*claude\b[^<\n]*<noreply@anthropic\.com>` |
| S3L | full commit message | `generated with \[?claude code\]?` |
| UL | - | S1 or S2L or S3L |

### 2.3 Signals not tested

| Signal | Reason |
|---|---|
| Type C `claude_disp` (5.5) | The paper gives the cell name only. It gives no pattern. |
| Type D `CLAUDE.md`, `.claude/` (3.2, 4.1, 5.3) | Project-level signal. It does not label a commit. |

## 3. Population

1. Repos: each git repo at depth 1 under `<SRC>/` and `<SRC>/<RESEARCH>/` (two local source folders). Add each `toplevel` in the change-chain policy rows.
2. Commits: all commits on branches and tags (change-chain `export`) with commit time in [2026-10-21T07:00:00Z, 2026-10-23T03:00:00Z).
3. The start is local midnight 2026-10-21 (PDT). The end is a fixed time before this file was written.
4. Worktrees share commits. Count each commit SHA one time. If labels differ across repos, keep the best label (ESTABLISHED, then INFERRED, then MISSING).

### 3.1 Exclusions

| Exclusion | Reason |
|---|---|
| This study's own repo | This study's own repo |
| Commits outside the time window | Fixed range |
| Paths that are not a git repo, or where change-chain exits with an error | No label. Count reported. |

Merge commits stay in the population. A merge without signal text is a miss reason (section 6).

## 4. Ground truth and labels

| change-chain session label | Role |
|---|---|
| ESTABLISHED | Ground truth: agent commit. Recall denominator. |
| INFERRED | Comparison row only. Shown apart. |
| MISSING | Comparison row only. Shown apart. Includes human commits and agent commits with no record. |

Tool: the change-chain development tree at commit `7dd4518`, run read-only with default inputs (`PYTHONDONTWRITEBYTECODE=1`).

## 5. Measures

1. For each signal and for U and UL: recall = k / n on ESTABLISHED commits.
2. Interval: Wilson score, 95 %, z = 1.959964.
3. For INFERRED and MISSING: the same k / n with the Wilson interval, labelled "signal rate", not recall.
4. Share of ESTABLISHED commits that U finds none of, and that UL finds none of.

## 6. Miss reasons (ESTABLISHED commits that a union misses)

Assign the first reason that applies, in this order:

| Reason | Rule |
|---|---|
| M1 trailer-has-model-name | S2L matches (S2 does not) |
| M2 footer-has-brackets | S3L matches (S3 does not) |
| M3 merge-no-signal-text | 2 or more parents; message has no `noreply@anthropic.com` and no `claude code` |
| M4 no-signal-text | 1 or 0 parents; message has no `noreply@anthropic.com` and no `claude code` |
| M5 other | None of the above |

## 7. Predictions (written before the run)

Disclosure: the author knows the current Claude Code default trailer. It has the form `Co-Authored-By: Claude <model name> <noreply@anthropic.com>`. The author also knows the owner's git author e-mail is not an Anthropic address.

| ID | Prediction |
|---|---|
| P1 | S1 recall is 0 or near 0 (Wilson upper bound below 0.10) |
| P2 | S2 recall (literal) is below 0.20 |
| P3 | UL recall is 0.80 or more |
| P4 | The main miss reason for U is M1 |

## 8. Rules

1. If ESTABLISHED n is below 10, report all numbers as underpowered. Draw no conclusion.
2. No change to matchers, window or exclusions after the run. Any extra analysis is labelled "exploratory".
3. Outputs hold counts only. No commit message, no e-mail address, no path outside repo basenames.
4. Repo basenames appear only for repos named `datapce*` or `change-chain*`. Other repos appear as `other-N`.
5. Limits to state: one operator, one agent, a window of about 2 days, local repos only.
