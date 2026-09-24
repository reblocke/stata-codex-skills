# Stata skills refactor: checkpoint 0

The implementation branch starts from documentation request `838e5e2`. The
original behavior checkpoint is `f2fade1`, which adds only the frozen synthetic
evaluation tasks. The underlying skill code remains `15c816b`. The working tree
was clean before branching, and no installed skill or distribution repository was
changed.

The source digest at `f2fade1` is
`355b444a1f830d9e24c83aa087f06e54022d10ff1fb45dbc3b6ac4a607e5c19d`.
The generated-tree digest is
`1bd4eb3fb4357dd6064c07e864f0231a80c6adb1004871c7d988b4b9e105762d`.
The complete baseline inventory has 63 canonical references, 16 category
indexes, one compatibility alias, and 95 generated files. The route set is
recorded in `stata-skills-refactor-routes.json`.

The observed machine is a MacBook Air (`Mac14,15`, arm64) running macOS 26.6.2.
The pinned environment has Python 3.11.10 and uv 0.11.11. The installed StataBE
app bundle reports version 18.0.179. The Codex CLI reports version 0.46.0.
Skill discovery was observed privately; no installed skills were changed.

| Baseline gate | Result |
| --- | --- |
| `make bootstrap` | Pass; frozen environment and lock check. |
| `make doctor` | Pass. |
| `make check TEST_JOBS=2` | Pass; 17 test modules, deterministic rendering, and repository scan. |
| Licensed `validate_skill_pack.py --suite default --keep-workdir` | Pass; core, package, compatibility alias, and plugin compilation suites. Private runtime logs were retained outside Git. |
| Launcher success fixture | Natural exit, current-run Stata status 0. |
| Launcher deliberate assertion error | Natural exit, current-run Stata status 9; correctly reported as failure. |
| Plugin runtime | Not run; no native behavior is changed in this request. |
| Locked-screen execution | Not run; requires separate observed coordination. |
| Fresh-agent behavioral comparison | Pending implementation checkpoints. |

One preliminary `make check` failed solely because the new task file was
untracked while receipt transaction tests ran. After committing that file, the
unchanged baseline passed. This is not counted as a pre-existing source failure.
The baseline licensed pass executes separately authored smoke tests and does not
establish that every published syntax block is runnable.

The 24 predeclared synthetic tasks, independent oracles, tolerances, and trial
budgets are in `tests/evaluation/tasks.yaml`. The original inputs remain fixed
for subsequent checkpoints.
