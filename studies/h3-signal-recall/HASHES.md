# H3 artifact: hashes and scrub record

This folder is a scrubbed copy of the H3 study (recall of published Claude Code commit signals on change-chain labels).
The copy removes local paths and local repo names. It keeps every count. The original study folder stays private and unchanged.

## Preregistration hashes

| File | SHA-256 |
|---|---|
| Original `prereg-h3.md` (written before the run; the paper cites this hash) | `927c2796f2e62f34c8f2a788a550c66f2ff0883b39d1c83e9dfc3244a50a1d84` |
| Scrubbed `prereg-h3.md` (this folder) | `dd762c5d8c02febafe1cc9746cda1cca440e7509780bed705a95285ad21777f4` |

The two files differ only on the lines in the table below. No rule, matcher, window, measure or prediction changed.

## What the scrub removed (line numbers of the original files)

| File | Line | Removed or changed |
|---|---|---|
| `prereg-h3.md` | 52 | two absolute local folder paths (home directory) replaced by placeholders <SRC> and <SRC>/<RESEARCH> |
| `prereg-h3.md` | 61 | local repo name of the study replaced by "This study's own repo" |
| `prereg-h3.md` | 75 | local repo name of the change-chain development tree replaced by a description (commit kept) |
| `results.md` | 4 | local repo name of the change-chain development tree replaced by a description (commit kept) |
| `run.py` | 20 | absolute local path to the change-chain checkout replaced by env var H3_CHAIN |
| `run.py` | 21 | absolute local path to the private hook capture replaced by env var H3_POLICY |
| `run.py` | 31-32 | three absolute local folder globs replaced by env var H3_ROOTS (same depth-1 globs per root) |
| `run.py` | 45 | local repo name of the study replaced by the basename of the script folder (same exclusion) |
| `test_signals.py` | 149 | two local repo names in a synthetic fixture replaced by demo names |
| `test_signals.py` | 150 | same fixture name |
| `test_signals.py` | 151 | same fixture name |

Total: 11 line-level edits in 4 files. `signals.py`, `results.csv` and `miss-reasons.csv` are byte-equal to the originals.

Behaviour note for `run.py`: the original searched one dot-folder glob in the first source folder only. The copy applies both globs (`*` and `.[a-z]*`) to every folder in `H3_ROOTS`. Set `H3_ROOTS`, `H3_CHAIN` and `H3_POLICY` before a run.

## All files in this folder

| File | SHA-256 | Same as original |
|---|---|---|
| `prereg-h3.md` | `dd762c5d8c02febafe1cc9746cda1cca440e7509780bed705a95285ad21777f4` | no (scrubbed) |
| `signals.py` | `8f6b8c84b8f8a86196f5fd3241d9d353429eb516e5a9af274b75febc2eb8759b` | yes |
| `test_signals.py` | `af14d411389f7af504074b71a93ccfb7ad8db8467bc35d7a6df0264e892924f7` | no (scrubbed) |
| `run.py` | `f8e23a8eb8fe75ffd5018ea515699ec5bcc62447cebbf8869a464c931a47130c` | no (scrubbed) |
| `results.md` | `09d205e4eb2043d125bb771e89fe9f44887476d92a6103de2b863c013bcf3c1d` | no (scrubbed) |
| `results.csv` | `8d433b8c3d95377d963f2645d8bfba2efb06c661e915b6814f16bd566b252730` | yes |
| `miss-reasons.csv` | `5d69ecbd780ec9817bfb891735be97cc5ea6d3e75ed42ad7e00450362a6994cb` | yes |

## Checks (2026-10-23)

| Check | Result |
|---|---|
| `python3 -m unittest test_signals` in this folder (Python 3.14.7, empty HOME) | 36 tests OK |
| Search for home paths, the owner handle, the study repo name and local-only repo names | 0 hits |
| `run.py` | parses; not re-run (it needs the private session records) |
