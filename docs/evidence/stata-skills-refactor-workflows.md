# Stata skills refactor: research workflow checkpoint

Code checkpoint: `885d1b09ff0b40ffc0320583b1885191106ea9e2`.
Source Git tree: `56be5df58abe8c686167b46b71ba73b6bafef197`.
Generated-tree SHA-256: `e674f62f7ea74021df3d8e3224047888e03299682999cc3608214d1cc36a1d18`.

Four existing core references now state explicit contracts for cohort data,
minimal scientific code refactoring, named result extraction, and simulation
failure accounting. All three root skills render reviewed modes for explanation,
editing, execution, and scientific analysis. The explanation mode explicitly
precludes installation, data execution, and an unsolicited full methods review.

Twelve independently written routing cases cover positive, negative, and
consequential boundary prompts for the four workflows. Four published runnable
examples have separate synthetic fixtures. The examples assert cohort window
endpoints and tie disposition, permitted refactor outputs and row counts, named
coefficients with model IDs, and simulation attempted/successful/failed counts.

| Check | Result |
| --- | --- |
| Targeted licensed Stata validation | Pass for data management, workflow best practices, advanced programming, and bootstrap/simulation after a clean-rerun marker correction. |
| Workflow clean rerun | Both independent sessions now report the same substantive count and grouped mean. |
| `make check TEST_JOBS=2` | Pass; 18 modules, deterministic double render, repository scan. |
| `git diff --check` | Pass before the code commit. |

The initial targeted workflow run executed and asserted both sessions but failed
the existing result-line comparison because the new fixture omitted its
`CODEX_RESULT` line. That line was added and the targeted workflow gate passed.
The complete licensed default gate and behavioral comparison remain pending.
