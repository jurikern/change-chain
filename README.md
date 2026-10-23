# change-chain

change-chain finds the agent session that made a git commit. It also finds the settings version (the policy) that was active for that session.
It reads local files only. It gives each link one label: ESTABLISHED, INFERRED or MISSING.

## What it does

Give a commit SHA. change-chain returns this chain:

| Link | Question |
|---|---|
| commit -> session | Which Claude Code session made the commit? |
| session -> policy | Which settings digest was active at the commit call? |
| commit -> deployment | Did a deployment start and end with this commit? (optional input) |
| commit -> PR, PR -> reviews, PR -> checks | Always MISSING. The tool gives the reason only. |

It also counts tool calls of the session that show no commit ("outside git").
The output holds IDs, hashes, times, labels and the commit subject from git. It never holds command text or transcript text.

## Requirements

| Item | Value |
|---|---|
| Python | 3.9 or later |
| git | on `PATH` |
| Packages | none (standard library only) |
| Tested on | macOS |

## Install

1. Get the source tree. No package install is necessary.
2. Run the tests (see Tests). Make sure all tests pass.
3. Optional: install the policy hook (see Policy hook). Without the hook, the policy link can be INFERRED only. The session link can then be ESTABLISHED only from OTel log lines.

## Quick start

`examples/make_demo.py` builds a synthetic dataset. All data is made up. The script reads nothing from your machine.

```sh
DEMO=$(mktemp -d)
python3 examples/make_demo.py --out "$DEMO"
export CHANGE_CHAIN_DIR="$DEMO/state"
python3 chain.py demo --repo "$DEMO/repo" --transcripts "$DEMO/projects" --gh-hosts "$DEMO/no-gh-hosts.yml"
```

The dataset has 2 commits (2026-10-20 10:00 and 11:00 UTC). Commit 1 has no session record. Commit 2 is an agent commit with a deployment.
Expected output of `demo`:

```text
demo commit: 2d534fe445c5
commit   2d534fe445c5       ESTABLISHED  git: 2026-10-20T11:00:00Z "demo commit 2"
session  demo-session-0001  ESTABLISHED  hook HEAD f2d705c31e23 -> 2d534fe445c5 across Bash call toolu_demo_2; call ended 2 s after the commit time
policy   2f91a02762a0de5b   ESTABLISHED  snapshot at the commit call toolu_demo_2 (2026-10-20T10:59:58+00:00)
outside  -                  INFERRED     session made 3 tool calls; 1 showed no commit (OTel; Bash 2, Read 1)
deploy   cm-demo-0001       ESTABLISHED  deploy log: start at 2026-10-20T11:05:00+00:00, end row with ConfigMap cm-demo-0001; no check row
pr       -                  MISSING      no PR record: no remote
reviews  -                  MISSING      no PR record: no remote
checks   -                  MISSING      no PR record: no remote
source windows holding the commit time: otel, policy
link gaps: commit -> PR: no remote, PR -> reviews: no remote, PR -> checks: no remote
gap count: 3
```

Then look at commit 1. Its session, policy, deployment and PR links are MISSING:

```sh
python3 chain.py lookup f2d705c --repo "$DEMO/repo" --transcripts "$DEMO/projects" --gh-hosts "$DEMO/no-gh-hosts.yml"
```

## Commands

```sh
python3 chain.py lookup <sha> [--repo PATH] [--json]       # one commit; text, or JSON with --json
python3 chain.py export [--repo PATH] [--since DATE]       # JSON lines, one line for each commit
python3 chain.py coverage [--repo PATH] [--since DATE]     # population, period, source windows, label counts
python3 chain.py demo [--repo PATH] [--sha SHA]            # full chain and gaps of the latest deployed commit
```

`--repo` is `.` by default. `export` and `coverage` read all commits on branches and tags. `--since` limits them by commit time. A bare date (`YYYY-MM-DD`) means midnight in the local time zone of the machine.
`demo` picks the latest deployment that has a `start` row and an `end` row, and whose commit is in the repo.

Each command accepts these flags. Each flag overrides one default path.

| Flag | Default | Input |
|---|---|---|
| `--otel` | `<state>/otel.jsonl` | OTel log lines |
| `--policy` | `<state>/policy.jsonl` | Policy rows (hook capture) |
| `--deploys` | `<state>/deploys.tsv` | Deploy log (optional) |
| `--transcripts` | `~/.claude/projects` | Claude Code transcripts |
| `--gh-hosts` | `~/.config/gh/hosts.yml` | gh login file. The tool reads only whether it exists. |

`<state>` is `$CHANGE_CHAIN_DIR`, else `$XDG_STATE_HOME/change-chain`, else `~/.local/state/change-chain`. An empty or relative `$XDG_STATE_HOME` is ignored.
An absent input file is not an error. Its links are then MISSING. Bad lines are skipped.
A SHA that is not a commit, or a `--repo` that is not a git repo, gives one error line on stderr and exit 2.

## Input formats

### 1. OTel log lines (`otel.jsonl`)

One JSON object on each line. Each object holds one OTLP/JSON log export under the key `payload`:

```json
{"payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [
  {"key": "session.id", "value": {"stringValue": "demo-session-0001"}},
  {"key": "event.name", "value": {"stringValue": "tool_result"}},
  {"key": "event.timestamp", "value": {"stringValue": "2026-10-20T11:00:02Z"}},
  {"key": "event.sequence", "value": {"stringValue": "2"}},
  {"key": "tool_name", "value": {"stringValue": "Bash"}},
  {"key": "tool_use_id", "value": {"stringValue": "toolu_demo_2"}},
  {"key": "vcs.ref.head.revision", "value": {"stringValue": "2d534fe445c5..."}}]}]}]}]}}
```

| Attribute | Required | Use |
|---|---|---|
| `event.name` | yes | Only `tool_result` events count. |
| `session.id` | yes | Session ID. |
| `event.timestamp` | yes | ISO 8601 time. A time with no zone is read as UTC. |
| `event.sequence` | no | Order of events with the same time. |
| `tool_name` | no | The ESTABLISHED rule needs `Bash`. |
| `tool_use_id` | no | Joins the event to the policy rows. |
| `vcs.ref.head.revision` | no | HEAD after the call. The commit rule needs the full 40-char SHA. Events without it count for "outside git" only. |

### 2. Policy rows (`policy.jsonl`)

`policy_snapshot.py` writes these rows (see Policy hook). One JSON object on each line:

| Field | Required | Value |
|---|---|---|
| `ts` | yes | UTC time of the row |
| `session_id` | yes | Session ID |
| `digest` | yes | Policy version: sha256 over (scope, file sha256) of the 4 settings files |
| `event` | no | `SessionStart`, `PreToolUse` or `PostToolUse` |
| `source` | no | SessionStart source, for example `startup` |
| `tool_use_id` | no | The `git commit` call (tool events only) |
| `cwd` | no | Hook working directory |
| `files` | no | For each scope (`user`, `project`, `local`, `managed`): `path`, `sha256`, `mtime` |
| `head`, `toplevel` | no | HEAD and the repo toplevel at the hook (tool events only) |

The rows hold hashes, paths and mtimes. They never hold settings content. Paths and mtimes do not change the digest.

### 3. Transcripts (`~/.claude/projects/**/*.jsonl`)

Claude Code session transcripts. The tool reads records that have `sessionId`, `timestamp`, `cwd` and `message.content`. It keeps 2 items only:

| Item | Source |
|---|---|
| Commit output | A `tool_result` text line `[branch abc1234] subject` (also `detached HEAD` and `(root-commit)`) |
| Commit call | A `Bash` `tool_use` whose `command` runs `git ... commit` (also `-q`, `-c k=v`, `-C dir`) |

It keeps no other text.

### 4. Deploy log (`deploys.tsv`, optional)

The deploy log is optional. Without it, every commit -> deployment link is MISSING. All other links do not change.
Tab-separated values, one row on each line. The first field gives the row type:

| Type | Field 2 | Field 3 | Field 4 | Field 5 |
|---|---|---|---|---|
| `start` | start time | commit SHA or prefix (7 to 40 lowercase hex chars) | deploy ID | instance name |
| `end` | start time of the deployment | end time | deploy ID (the same as in the `start` row) | instance name |
| `check` | time | result text | - | - |

Example (from the demo):

```text
start	2026-10-20T11:05:00+00:00	2d534fe	cm-demo-0001	pod-demo-a
end	2026-10-20T11:05:00+00:00	2026-10-20T11:06:00+00:00	cm-demo-0001	pod-demo-b
```

Times are ISO 8601. Use UTC. The deploy ID can be any string that is the same in the `start` row and the `end` row, for example a ConfigMap hash.
A `check` row is ok if its result text starts with `ok`. It names a commit only with the text `datapce-deploy-check/<sha>` in the result text.

## Labels

| Label | Meaning |
|---|---|
| ESTABLISHED | A machine record shows the link directly. |
| INFERRED | The link rests on unsigned text, a time window or file mtimes. |
| MISSING | No record links the two. |

## Link rules

| Link | Source | Rule | Label |
|---|---|---|---|
| commit -> session | Policy rows (`head`, `toplevel`) | Same `tool_use_id`: PreToolUse HEAD = a parent (none for a root commit), PostToolUse HEAD = the commit, both in a repo worktree, call ended 0-120 s after the commit time | ESTABLISHED |
| commit -> session | Policy rows | PostToolUse HEAD = the commit, but no Pre row, HEAD before is not a parent, or outside the window | INFERRED |
| commit -> session | OTel log lines | HEAD moved parent -> commit by the end of a Bash call that ended 0-120 s after the commit time | ESTABLISHED |
| commit -> session | OTel log lines | Same HEAD move in a non-Bash call, or outside the window | INFERRED |
| commit -> session | Any | Two or more sessions meet an ESTABLISHED rule | INFERRED, gap `commit -> session: conflicting sessions` |
| commit -> session | Transcript commit output | SHA prefix and whole subject match, 300 s or less from the commit time | INFERRED |
| commit -> session | Transcript commit call | Whole subject in the command, cwd in a repo worktree (or `-C`/`cd` to it), 300 s or less from the commit time | INFERRED |
| commit -> session | None | No rule matches | MISSING, gap `commit -> session` |
| session -> policy | Policy rows | PreToolUse row with the same `tool_use_id` as the commit call | ESTABLISHED |
| session -> policy | Policy rows | Last other row of the session at or before the commit time | INFERRED |
| session -> policy | Settings file mtimes in the session cwd | No row; every present settings file is older than the commit | INFERRED |
| session -> policy | None | No row, and a settings file changed after the commit | MISSING, gap `session -> policy` |
| commit -> deployment | Deploy log | The latest `start` row of the commit has an `end` row with the same deploy ID that does not end before the start | ESTABLISHED |
| commit -> deployment | Deploy log | The latest `start` row has no such `end` row, or only a `check` row names the commit | INFERRED |
| commit -> deployment | Deploy log | No row names the commit | MISSING, link gap `commit -> deployment` |
| commit -> PR | None | No local record links a commit to a PR | MISSING, link gap `commit -> PR: <reason>` |
| PR -> reviews | None | Same | MISSING, link gap `PR -> reviews: <reason>` |
| PR -> checks | None | Same | MISSING, link gap `PR -> checks: <reason>` |

If more than one session matches, the tool takes the best label, then the smallest time gap. It lists the other sessions.
A `start` row names a commit if it holds 7 or more hex chars and is a prefix of the full SHA. The result shows the count of `start` rows. A `check` row with `ok` sets `checked`; it does not change the label.

Reasons for the PR links:

| Reason | Condition |
|---|---|
| `no remote` | The repo has no git remote. |
| `gh not authenticated` | A remote exists. The gh login file does not exist. |
| `gh not queried` | A remote and the gh login file exist. The tool never runs `gh`. |

The reason uses remote names and file existence only. It never reads the remote URL or the login file.

## Output

`lookup --json` and `export` give these keys: `commit`, `session`, `policy`, `deployment`, `pr`, `reviews`, `checks`, `gaps`, `link_gaps`, `forge`, `outside_git`, `completeness`, `other_sessions`, `sessions_on_top`.
`gaps` holds the session and policy gaps. `link_gaps` holds the deployment and PR gaps.

`coverage` prints these lines before the label counts:

| Line | Meaning |
|---|---|
| `population` | Refs counted (branches and tags), commit count, merge count, repo name |
| `period` | `--since` value, first and last commit time |
| `source <name>` | Rows, first time and last time of each source (`otel`, `transcripts`, `policy`, `deploys`) |
| `in window` | Commits whose commit time is inside each source window |
| `forge` | The PR link reason |
| `outside git (INFERRED)` | Sessions, tool calls, calls that showed a commit, calls outside git |

A commit outside the window of a source cannot get a link from that source. Read its MISSING as "no data", not as a true gap.
A window is first row to last row. It does not show holes inside the window.

"Outside git" counts the OTel tool calls of a session that show no new HEAD revision. The label is always INFERRED. The count is an upper bound for work outside git.

## Policy hook

`policy_snapshot.py` is a Claude Code hook. It writes one row to `policy.jsonl` at session start, and before and after each Bash `git commit` call. Other tool calls write nothing.
The match is broad: `git` and then `commit` in one command segment (for example `git -C repo commit` or `git log --grep=commit`). An extra row makes no link, because a link needs a HEAD move.
It writes to `$DATAPCE_POLICY_DIR` if set (older override), else to `<state>`. It always exits 0 and prints nothing to stdout. It never blocks or allows a call.

CAUTION: `install_hook.py` changes your Claude Code settings file. Do a dry run first.

1. Run `python3 install_hook.py --dry-run`. Read the changes.
2. Run `python3 install_hook.py`.
3. Restart each Claude Code session.

| Flag | Effect |
|---|---|
| `--settings PATH` | Use this settings file. The default is `~/.claude/settings.json`. |
| `--dry-run` | Print the changes. Write nothing. |
| `--uninstall` | Remove only the entries that run this `policy_snapshot.py`. |

The script creates the settings file if it does not exist. It makes a backup (`<file>.bak-YYYYMMDD-HHMMSS`) before it changes an existing file. A second run adds nothing. It keeps all other settings and hooks. If the settings file is a symlink, the script writes to its target. Close Claude Code before you run it: the script does not lock the file.
It adds these entries under `"hooks"` (the path is the absolute path of your copy):

```json
"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [{"type": "command",
  "command": "python3 /path/to/change-chain/policy_snapshot.py"}]}],
"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "python3 /path/to/change-chain/policy_snapshot.py"}]}],
"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "python3 /path/to/change-chain/policy_snapshot.py"}]}]
```

Cost: one or two Python starts for each Bash call.
The managed settings file is `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS and `/etc/claude-code/managed-settings.json` on other platforms.

## Limits

1. A HEAD move from parent to commit also occurs on `git pull` (fast-forward) or `git reset` in a Bash call. The Bash rule and the 120 s window reduce this risk; they do not remove it.
2. The policy digest covers the four settings files only. It does not cover CLI flags, the permission mode or session grants.
3. Policy rows, transcripts and the deploy log are unsigned local files.
4. Project settings at the commit are also in git (`git show <sha>:.claude/settings.json`). change-chain does not use this yet.
5. `git commit --amend`: HEAD moves from a non-parent, so the OTel link stays MISSING and the hook link is INFERRED.
6. A non-Bash call (Read, Edit) that sees the HEAD move gives INFERRED only. It does not make a conflict with a Bash call.
7. The mtime fallback cannot see a deleted or restored settings file. Its label is INFERRED for that reason.
8. The hook reads HEAD in the session cwd. A commit after `cd other-repo &&` or `git -C other-repo` gets no hook link.
9. Several commits in one call: the hook sees only HEAD before and after the call. The last commit is INFERRED (HEAD before is not its parent). The earlier ones get no hook link.
10. OTel log lines have no command text. A quiet `git commit -q` can show no revision. It then counts as outside git.
11. A commit that is only an ancestor of a deployed commit is MISSING for the deployment link.
12. The tool builds no PR, review or check link. These links are always MISSING.

## Tests

```sh
python3 -m unittest test_chain test_policy_snapshot test_review_probes   # 151 tests
```

The tests use temp repos and synthetic data only. `test_review_probes.py` holds the probes of an independent code review. Two probes record a design decision (Limits 5 and 6).

## Licence

MIT. See `LICENSE`.

## Citation

See `CITATION.cff`.
