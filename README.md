# stata-codex-skills

## Overview

`stata-codex-skills` builds and locally publishes three Codex skills for Stata:

- `stata-core` for built-in commands, do-files, data management, estimation,
  graphics, workflow, and Mata
- `stata-packages` for community-contributed packages
- `stata-c-plugins` for Stata C and C++ plugin development

Reviewed YAML under `content/` is the sole executable publication authority.
Local Stata help, upstream material, manifests, and locks provide reviewable
evidence and provenance; they cannot add, remove, or alter published routes.
Generated skill folders must not be edited by hand or committed.

The generated tree contains 73 files: three root `SKILL.md` routers, three
`agents/openai.yaml` files, three compact `PROVENANCE.md` indexes, 63 canonical
references, and one historical diagnostics compatibility alias. Canonical
content is split across 38 core, 19 package, and 6 plugin references.

## Requirements

- `uv` 0.11.11, pinned in `pyproject.toml` and CI
- Python 3.11.x with the pinned Unicode behavior
- POSIX process-group semantics (macOS or Linux) for isolated unit tests
- Git

The offline build does not require Stata or network access after the frozen
Python environment is installed. Licensed validation additionally expects
macOS, Stata under `/Applications/Stata`, `/usr/bin/sandbox-exec`, network
access for isolated package and SDK retrieval, and `clang` for plugin
compilation.

## Quick start

```bash
git clone https://github.com/reblocke/stata-codex-skills.git
cd stata-codex-skills
make bootstrap
make doctor
make check
```

Before publishing, run the licensed default validation and then publish the
freshly validated tree:

```bash
make validate
make publish
```

Restart Codex after the first installation on a machine.

## Build and validation

`make build` validates source paths and content, renders the complete
three-skill tree beside `build/generated/`, validates the staged tree, and
replaces the prior tree transactionally. `make check` adds generated-drift
lint, unit tests, deterministic double rendering, and secret/artifact scanning.
Both commands are deterministic and independent of Stata and package or
upstream networks. `make all` is a compatibility alias for `make check`.
Unit-test modules use four isolated workers by default; set `TEST_JOBS=1` for
serial diagnosis or adjust the per-module `TEST_TIMEOUT` when needed. The
module phase has a configurable six-minute global deadline
(`TEST_GLOBAL_TIMEOUT=360`). CI allows 15 minutes for dependency setup, build,
bounded cleanup, deterministic rerendering, and final scans around that phase.
The cooperative process guard covers trusted Python-visible
`Popen`/fork/`posix_spawn`/multiprocessing paths. It is not an operating-system
sandbox against hostile native code or deliberate descriptor shedding; a lost
lease or guard initialization error fails the test gate.

Validation evidence is deliberately separated:

- `make check` provides automated static schema, lock, routing-fixture,
  generated-tree, determinism, and repository-scan evidence.
- Fresh-agent forward tests are manual checks of actual Codex routing. Static
  prompt-fixture lint does not claim that a fresh agent followed a route.
- `make validate` runs licensed Stata integration: static checks, all core and
  package smoke tests, and plugin compilation.
- `make validate-plugin-runtime` explicitly attempts plugin loading and
  execution. It is excluded from the default gate because the official sample
  has hung at the local Stata/macOS loader boundary even when compilation
  succeeds.

GitHub Actions installs the frozen environment and runs `make check` on Ubuntu
and macOS. CI has no Stata license, so licensed integration and plugin runtime
remain local checks.

Useful targeted commands:

```bash
make validate-core
make validate-packages
make validate-packages PACKAGES="reghdfe rdrobust"
make validate-plugin-compile
make validate-plugin-runtime
```

The validator CLI also accepts repeatable `--suite` and `--package` arguments.
The default suite is `static`, `core`, `packages`, and `plugin-compile`; it
never includes plugin execution. `--keep-workdir` retains a failed
run-specific transaction outside the repository for inspection.

Every Stata check uses an isolated temporary `PLUS` and `PERSONAL` path, a
unique completion marker, bounded subprocess and network timeouts, and
content-defined assertions. Missing or stale logs, missing markers, Stata
errors, assertion failures, cleanup uncertainty, and partial package results
fail the aggregate command. Diagnostics redact repository, home, temporary,
and license metadata.

## Editing and maintenance

The normal change sequence is:

1. Edit reviewed YAML in `content/`.
2. If routing changes, independently edit the relevant structured cases in
   `tests/prompts/cases.yaml`; do not derive fixtures from the trigger being
   tested.
3. Run `make check`.
4. Run `make validate`.
5. Run `make publish`.

Exact local help names or explicitly reviewed `.sthlp` globs are required;
fuzzy help matching is not publication authority. Community packages are
tested in isolated paths against per-package lock metadata. Installation
instructions remain optional, require user authorization, avoid default
`replace`, and use pinned or lock-verified sources.

Upstream and lock refreshes are explicit candidate-generation operations.
They write ignored review reports and never promote content or locks
automatically. To compare an exact upstream revision:

```bash
make refresh UPSTREAM_REF=33a7efc85e92cd30edc7b907f1deb9d7038397bc
```

The refresh requires a full commit, validates the dedicated checkout and its
Git metadata, detaches at that commit, and atomically writes only
`raw/candidates/upstream-comparison.yaml`. Review candidate reports before
manually promoting any source or lock change. Lock-candidate replacement keeps
one fixed `.previous` recovery entry per target; review and remove that exact
entry before refreshing the same target again.

Key implementation entry points:

- `scripts/render_skills.py` renders and transactionally replaces all skills.
- `scripts/lint_skill_pack.py` validates content, provenance, locks, routing
  cases, generated output, and metadata.
- `scripts/validate_skill_pack.py` runs static and licensed suites and writes
  the publication receipt.
- `scripts/publish_local.py` stages all three validated skills and publishes
  them with rollback protection.
- `scripts/fetch_upstream.py`, `scripts/harvest_stata_help.py`, and
  `scripts/refresh_locks.py` produce ignored review candidates.

## Publication and recovery

By default, publication targets `~/.codex/skills/`. If `CODEX_HOME` is set,
`make publish` targets `$CODEX_HOME/skills/`. The resolved destination must be
outside this repository, its Git metadata, and `build/generated/`; it must be
owned by the effective user and must not be group- or other-writable.

`make validate` invalidates any prior receipt before running the offline gate
and licensed default suite. A new schema-3 receipt is written only after the
complete gate succeeds. It binds the tracked source bytes, generated-tree
digest, selected suites, validation time, and canonical modes (`0755` for
directories and `0644` for files).

`make publish` requires a receipt less than one hour old and rejects source,
index, ignored-input, generated-byte, membership, or permission drift. To
invalidate a receipt without validating:

```bash
uv run --frozen python scripts/validate_skill_pack.py --invalidate-receipt
```

Rendering and publishing use staged complete trees, no-replace operations,
destination locks, verified backups, and rollback. When cleanup or rollback
cannot prove ownership and identity before removal, the command preserves the
uncertain stage, backup, receipt, or recovery directory and prints its verified
path. A render cleanup or parent-verification failure after verified removal
may have no surviving prior-tree or workspace copy; the command fails and
reports that no survivor was verified instead of claiming retained recovery.
Unresolved `.stata-codex-skills-publish-*` recovery directories block later
publication.

Treat every reported recovery path as an explicit human review task:

1. Confirm no render, validation, or publication process is still running.
2. Inspect the exact reported path and recover anything needed.
3. Remove only that exact path; never use a wildcard or prefix-wide cleanup.

Forced termination, power loss, storage failure, or filesystem changes after a
command returns remain outside the transaction guarantee.

## Local discovery and repository use

Skills are installed per machine; an OpenAI account does not synchronize
`~/.codex/skills/` between machines. Once installed, the same skills can be
used from any repository on that machine. A project may additionally use
`AGENTS.md` to route built-in work to `stata-core`, community-package work to
`stata-packages`, and native plugin work to `stata-c-plugins`.

For a custom Codex home:

```bash
export CODEX_HOME=/path/to/codex-home
make publish
```

## Repository layout

```text
content/    reviewed executable YAML
config/     skill names, sections, boundaries, and compatibility routes
locks/      reviewed upstream, Stata-help, plugin-SDK, and package locks
manifests/  provenance records; never publication authority
templates/  deterministic skill templates
scripts/    render, lint, validation, refresh, and publication tools
tests/      unit tests, Stata smokes, and structured routing fixtures
raw/        ignored upstream/help/lock review candidates
build/      ignored generated skill tree and validation receipt
```

Runtime logs, package installations, raw proprietary help, license-bearing
output, and generated documents stay outside published skills and the
repository root.

## License and citation

Repository code is released under the MIT License; see `LICENSE`. Third-party
Stata help, package code, SDK material, and publisher content remain under
their original terms and are not vendored here.

No clinical data or manuscript version is expected in this repository. Cite
the GitHub repository URL and the commit or release used. Maintainer:
Brian W. Locke (`@reblocke`).
