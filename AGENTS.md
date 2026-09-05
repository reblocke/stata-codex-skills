# AGENTS

## Project Purpose
Builds local Codex skills for Stata from structured YAML and local help sources

## Public and Data-Safety Rules
- Treat this repository as public. Do not add PHI, restricted datasets, credentials, private drafts, or publisher-formatted article text.
- No clinical data expected
- Manuscript status: No manuscript version expected

## Workflow and validation
- Consult `README.md` for the build, source-authority, licensed-validation, and publication contracts relevant to the change.
- Edit reviewed `content/` YAML; generated skills in `build/generated/` are ignored build products. Change templates/rendering when altering the generated structure.
- For routing changes, update independently written cases in `tests/prompts/cases.yaml` and affected generated-tree checks.
- Use `make bootstrap` to provision the pinned environment and `make doctor` to inspect prerequisites. `make check` runs offline build, lint, tests, determinism, and repository scanning.
- The local unit tests use isolated temporary fixtures and no clinical data. Run affected checks and fix regressions from the requested change without repeated approval.
- `make validate` is the licensed Stata/package/plugin-compilation gate and produces a fresh receipt. `make publish` requires that receipt and separate authorization for the destination unless already granted. Do not treat offline checks as licensed or plugin-runtime acceptance.
- Preserve the source, lock, receipt, and transactional replacement safeguards. Dependency or upstream refreshes remain explicit operations.

## Verification Before Publishing Changes
- Run `git diff --check`.
- Validate `CITATION.cff` as YAML after citation edits.
- Do not commit generated outputs, logs, caches, virtual environments, `.DS_Store`, or checkpoint files unless intentionally released.
- For clinical or collaborator data, confirm that no row-level restricted data or identifiers are included.
