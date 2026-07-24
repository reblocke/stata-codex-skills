# stata-codex-skills

`stata-codex-skills` is a local build repo for Codex skills that help with Stata work.

It does three things:

1. Maintains reviewed, structured skill content in `content/**/*.yaml`.
2. Produces review candidates from an exact upstream commit, exact local Stata help files, and isolated package metadata without rewriting curated fields.
3. Renders three Codex-native skills and publishes them into the local Codex skills directory.

The repo is designed to be a distillation pipeline, not a mirror. Upstream material and local `.sthlp` files are evidence for review, but `content/**/*.yaml` is the sole executable publication authority. Manifests and locks record provenance; they cannot add, remove, or alter published routes. Generated skill folders are never edited by hand.

This repository is released under the MIT License. See `LICENSE`.

## What gets built

This repo builds three skills:

- `stata-core`: built-in Stata commands, do-files, data management, estimation, graphics, workflow, and Mata
- `stata-packages`: community-contributed packages such as `reghdfe`, `rdrobust`, `estout`, `synth`, and `xtabond2`
- `stata-c-plugins`: Stata C and C++ plugin development, packaging, and validation

Current content inventory:

- 38 core references in `content/core/`
- 19 package references in `content/packages/`
- 6 plugin references in `content/plugins/`

That is 63 canonical references. The former package diagnostics route remains
available as a generated compatibility alias to the canonical built-in
regression-diagnostics reference and does not count as a 64th reference.

Generated skills are written to `build/generated/` and published into:

- `~/.codex/skills/stata-core`
- `~/.codex/skills/stata-packages`
- `~/.codex/skills/stata-c-plugins`

If `CODEX_HOME` is set, the default publication root is
`$CODEX_HOME/skills/`.

## Design principles

- Treat the YAML files under `content/` as the source of truth.
- Do not hand-edit generated `SKILL.md` files or generated reference pages.
- Keep provenance explicit. Each content file records exact local help selectors and upstream files; checked locks record source revisions and file hashes.
- Prefer local Stata help over upstream prose whenever equivalent coverage exists.
- Use isolated validation directories and temporary Stata `PLUS` paths so validation does not pollute your normal Stata environment.

## Repository layout

```text
stata-codex-skills/
├── content/
│   ├── core/          # editable YAML source for built-in Stata topics
│   ├── packages/      # editable YAML source for community packages
│   └── plugins/       # editable YAML source for plugin workflows
├── config/            # deterministic skill names, sections, and compatibility routes
├── locks/             # reviewed upstream, Stata-help, package, and plugin-SDK locks
├── manifests/         # generated provenance indexes; never publication authority
├── templates/         # Jinja templates for SKILL.md and reference pages
├── scripts/           # fetch, harvest, scaffold, render, lint, publish, validate
├── tests/             # unit tests and structured routing cases
├── raw/               # upstream/help/lock review candidates (gitignored)
└── build/             # generated skill folders before publish (gitignored)
```

Also note:

- `raw/`, `build/`, and `tests/tmp/` are gitignored.
- Runtime validation writes logs, generated documents, package files, and license-bearing output only inside a run-specific system temporary directory, never in the repository root.
- The repo currently assumes a local Stata install under `/Applications/Stata`.

## Requirements

- `uv` 0.11.11 (enforced by `pyproject.toml` and pinned in CI)
- Python 3.11 or newer, installed or managed by `uv`
- Git

`make build` and `make check` do not require Stata, the upstream checkout, or
package-network access after `make bootstrap` has installed the frozen Python
environment. The build gate first checks `pyproject.toml` against `uv.lock`
offline, so dependency edits without a reviewed lock update fail.

The default licensed validation additionally requires:

- macOS with Stata under `/Applications/Stata`
- network access for isolated community-package installation
- `clang` and network access to the checksum-pinned plugin SDK sources

Upstream comparison refreshes require GitHub access. Plugin execution is an
optional local integration test.

## Quick start

Render the reviewed content and run the offline checks:

```bash
cd ~/src/stata-codex-skills
make bootstrap
make doctor
make check
```

Run licensed/network integration validation before publishing:

```bash
make validate
make publish
```

`make validate` writes a digest-bound receipt only after the complete default
gate succeeds. `make publish` requires that receipt to be less than one hour
old and to match both the current source state and the exact staged tree.

Useful targeted validation commands:

```bash
make check
make validate-core
make validate-packages
make validate-packages PACKAGES="reghdfe rdrobust"
make validate-plugin-compile
make validate-plugin-runtime
```

The validator CLI accepts repeatable suites and package selections:

```bash
uv run --frozen python scripts/validate_skill_pack.py \
  --suite static --suite core
uv run --frozen python scripts/validate_skill_pack.py --suite packages \
  --package reghdfe --package rdrobust
uv run --frozen python scripts/validate_skill_pack.py \
  --suite plugin-runtime --keep-workdir
```

`--suite default` is used when no suite is supplied. It runs static checks, the
core smoke test, every package smoke test, and plugin compilation. It does not
execute the plugin. `--keep-workdir` preserves a failed run's temporary
workspace for debugging; successful workspaces are always removed.

If this is the first installation on a machine, restart Codex after
`make publish`.

## Deploying on other machines and repositories

Codex discovers skills from the local machine, not from the current git repository alone.

The same OpenAI account does not sync `~/.codex/skills` across machines automatically.

That means:

- installing the skills into `~/.codex/skills/` or `$CODEX_HOME/skills/` is a machine-specific step
- adding `AGENTS.md` or `README.md` guidance in a project repo is a repository-specific step
- you usually need both if you want Codex to reliably use these skills in another project

### Same machine, different repository

If the skills are already published on the current machine, no reinstall is needed.

What to do in the other repository:

- add an `AGENTS.md` or project README note that routes Stata work to `stata-core`, `stata-packages`, and `stata-c-plugins` as appropriate
- prefer portable skill paths such as `~/.codex/skills/stata-core/SKILL.md` rather than user-specific absolute paths
- name the relevant package skill only when a community package is actually involved

In practice, once these folders exist locally, Codex can use them from any repository on the same machine:

- `~/.codex/skills/stata-core`
- `~/.codex/skills/stata-packages`
- `~/.codex/skills/stata-c-plugins`

### New machine or new user account

On a new machine, you have two reasonable options.

Option 1: copy the already-generated skill folders from another working machine:

```bash
mkdir -p ~/.codex/skills
cp -R /path/from/other/machine/stata-core ~/.codex/skills/
cp -R /path/from/other/machine/stata-packages ~/.codex/skills/
cp -R /path/from/other/machine/stata-c-plugins ~/.codex/skills/
```

Option 2: clone this repo and publish the skills locally:

```bash
git clone https://github.com/reblocke/stata-codex-skills.git ~/src/stata-codex-skills
cd ~/src/stata-codex-skills
make bootstrap
make check
make validate
make publish
```

After `make publish`, the skill folders should exist under `~/.codex/skills/` unless you published to a custom destination. Restart Codex if the skills were not already installed on that machine.

### Publishing to a non-default Codex home

If the machine uses a custom `CODEX_HOME`, `make publish` honors it
automatically:

```bash
export CODEX_HOME=/path/to/codex-home
make publish
```

If `CODEX_HOME` is unset, the default location is:

```bash
~/.codex/skills
```

### Refreshing an existing installation

If the skills are already installed on a machine and you want to update them after pulling new changes:

```bash
cd ~/src/stata-codex-skills
git pull
make bootstrap
make check
make validate
make publish
```

Licensed Stata and network access are required for `make validate`.

### What is and is not repository-specific

Repository-specific actions:

- adding an `AGENTS.md` or repo README note that tells Codex when to use `stata-core`, `stata-packages`, and `stata-c-plugins`
- documenting any local prerequisites the repository assumes, such as Stata availability or a preferred batch entrypoint

Not repository-specific:

- the actual skill installation into `~/.codex/skills/` or `$CODEX_HOME/skills/`
- the rendered `SKILL.md` files themselves
- Codex discovery of installed skills on the local machine

### Minimal collaborator instructions

If collaborators will use these skills, the minimum setup note to give them is:

1. Clone `stata-codex-skills`.
2. Run `make bootstrap`, `make check`, `make validate`, and `make publish`.
3. Confirm the skill folders exist in `~/.codex/skills/` or `$CODEX_HOME/skills/`.
4. In the analysis repository, add `AGENTS.md` guidance that names the Stata skills explicitly.

Without step 2, an `AGENTS.md` file can mention the skills but Codex will not be able to load them on that machine.

## Typical workflow

When you update the repo, the normal order is:

1. Edit the reviewed YAML files in `content/`.
2. Regenerate structured cases with
   `uv run --frozen python scripts/render_prompt_cases.py`.
3. Run `make check`.
4. Run `make validate`.
5. Run `make publish`.

`make build` stages and validates the complete three-skill tree beside
`build/generated/`, then swaps it as one filesystem transaction. A render or
staged-tree failure leaves the prior generated tree untouched. `make all`
remains a compatibility alias for `make check`.

Fetching upstream material, harvesting help, scaffolding candidates, and
refreshing locks are explicit maintenance operations. Review their ignored
candidate reports before promoting any source or lock change.

To compare one exact upstream revision:

```bash
make refresh UPSTREAM_REF=33a7efc85e92cd30edc7b907f1deb9d7038397bc
```

The refresh checks out that full commit in detached-head state and atomically
writes an ignored comparison under `raw/`; it never changes curated content or
locks. A failed refresh removes the prior target report rather than leaving
stale evidence at the canonical path.

## What each script does

- `scripts/fetch_upstream.py`: fetches and detaches at one required full commit, then writes an ignored comparison without changing curated content or locks
- `scripts/harvest_stata_help.py`: resolves only exact help names or declared globs and writes a reviewable candidate report
- `scripts/scaffold_content.py`: reports missing or empty curated fields and never rewrites content
- `scripts/render_skills.py`: renders, validates, and atomically replaces the complete three-skill tree
- `scripts/render_prompt_cases.py`: deterministically generates structured routing fixtures from every canonical reference plus boundary cases
- `scripts/refresh_locks.py`: writes ignored lock candidates for explicit review; it never promotes them
- `scripts/verify_locks.py`: verifies checked provenance locks, with optional live local/network checks
- `scripts/lint_skill_pack.py`: validates schema quality, exact provenance, locks, prompt cases, generated routing, and metadata
- `scripts/check_determinism.py`: renders twice in clean temporary roots and compares byte-level tree digests
- `scripts/scan_repository.py`: scans tracked and unignored files for generated artifacts, third-party code, and high-confidence secret patterns
- `scripts/doctor.py`: verifies the pinned uv version and reports offline build and optional licensed-validation prerequisites
- `scripts/validate_skill_pack.py`: runs static and licensed integration suites and writes the publication receipt after complete default success
- `scripts/publish_local.py`: stages all three validated skills under the destination filesystem and swaps them with full rollback

## Validation model

Validation is intentionally divided into four evidence levels:

- Automated static routing and build checks: `make check` runs atomic rendering,
  schema/lock/generated-drift lint, structured prompt-fixture lint, unit tests,
  deterministic double rendering, and secret/artifact scanning. It does not
  invoke Stata or package/upstream networks.
- Manual fresh-agent forward tests: prompts are given to clean agents and
  compared with `tests/prompts/cases.yaml`. These tests assess actual routing
  behavior and are reported separately from static fixture lint.
- Licensed Stata integration: `make validate` runs all built-in and package
  smokes plus plugin compilation and records a publication receipt.
- Optional plugin execution: `make validate-plugin-runtime` explicitly attempts
  the local loader/runtime path and is excluded from the default gate.

Runtime validation is split into independently selectable suites:

- `static`: YAML, provenance, generated-file, and routing lint
- `core`: built-in Stata commands and assertions
- `packages`: isolated package installation and content-specific smoke tests
- `plugin-compile`: download the pinned official SDK inputs and compile the sample plugin
- `plugin-runtime`: compile the plugin, then explicitly attempt to load and execute it in Stata
- `default`: `static`, `core`, `packages`, and `plugin-compile`

Each Stata check receives a unique completion marker and writes one
run-specific log in its own temporary directory. A check passes only when the
expected log exists, contains the exact marker and expected assertions or pass
line, and contains no Stata error. Pre-existing or similarly named logs are
never considered. Every selected result is aggregated, and any failure makes
the validator and corresponding Make target exit nonzero.

The validator uses bounded network, compiler, subprocess, and Stata timeouts
and terminates lingering Stata processes. Failed runs print bounded diagnostics
with repository, home-directory, temporary-path, and Stata license metadata
sanitized. The workspace is then deleted unless `--keep-workdir` was supplied.
Package tests use isolated temporary `PLUS` and `PERSONAL` paths, and stochastic
smoke tests use a fixed seed.

Plugin compilation is part of the default gate. The official `stplugin.h`,
`stplugin.c`, and `hello.c` downloads are pinned by URL and SHA-256 and verified
before compilation. Plugin execution remains an explicit integration test
because the official sample currently hangs at the plugin call on this local
Stata/macOS loader boundary.

GitHub Actions pins the same uv version, checks lock freshness offline, performs
`uv sync --frozen`, and then runs the same deterministic offline `make check`
gate. Licensed Stata, package installation, and plugin runtime tests remain
documented local integrations because CI has no Stata license.

## Current status as of July 24, 2026

### Static and core validation

- Static schema, provenance, lock, prompt-case, and generated-drift lint: pass
- Python unit tests: 77 passed
- Structured routing cases: 76 (63 canonical plus 13 boundary cases)
- Fresh-agent forward routing: 76/76 selected the expected route and no forbidden route
- Deterministic clean double render and repository secret/artifact scan: pass
- `stata-core`: all 38 content-defined smoke tests passed
- `stata-core` validator path handling: pass when the repo lives in a path with spaces

### Full package sweep

The full package gate contains 19 canonical references plus the generated
historical diagnostics compatibility route. Result:

- 20 checks passed
- 0 checks failed

Packages that passed:

- `asdoc`
- `binsreg`
- `coefplot`
- `data-manipulation`
- `diagnostics` compatibility alias to built-in regression diagnostics
- `did`
- `estout`
- `event-study`
- `graph-schemes`
- `ivreg2`
- `nprobust`
- `outreg2`
- `package-management`
- `psmatch2`
- `rdrobust`
- `reghdfe`
- `synth`
- `tabout`
- `winsor`
- `xtabond2`

The package metadata changes that mattered most were:

- `binsreg` and `nprobust`: switched from stale `ssc install` paths to the current NP Packages `net install` URLs
- `nprobust`: smoke test now exercises the actual command family (`lprobust`, `kdrobust`) instead of the package name
- `ivreg2`: installs `ranktest` before the smoke test
- `rdrobust`: installs the suite from exact official GitHub commits after a newer SSC distribution failed its own Mata runtime
- `reghdfe`: installs pinned `require`, `ftools`, and `reghdfe` sources and compiles `ftools`
- `event-study`: uses a valid `eventstudyinteract` example based on the documented `nlswork` workflow
- `synth`: uses a synthetic local panel dataset instead of relying on `webuse synth_smoking`

### Plugin validation

Plugin runtime validation is implemented, but it does not currently pass on this machine.

What was verified:

- the validator can download `stplugin.h`, `stplugin.c`, and the official `hello.c` sample from `stata.com`
- the plugin compiles successfully with `clang`
- Stata can define the plugin-backed program with `program ..., plugin using("...")`

Where it fails:

- execution hangs at the first plugin call itself
- this happens with the official `hello.c` sample and with a no-op plugin
- the hang reproduces with `arm64` and universal plugin bundles

That strongly suggests the current blocker is the local Stata/macOS plugin runtime boundary, not the example plugin logic.

Manual plugin repro files used during debugging were written under `tests/tmp/plugin-manual/` and are gitignored.

## Known caveats

- The repo currently targets a macOS Stata install under `/Applications/Stata`.
- Package install sources change over time, so validation metadata will still need occasional refreshes.
- Plugin execution under local batch-mode `StataBE` is not yet reliable on this machine.
- Publication receipts expire after one hour even when the source and generated
  digests are unchanged; rerun `make validate` before a later publish.

## Remaining investigation

Investigate the plugin runtime hang at the macOS loader boundary before
relying on `stata-c-plugins` runtime execution. Compilation remains part of the
default gate, and execution remains explicit.

## Generated outputs to inspect

After rendering and publishing, the most important files to inspect are:

- `build/generated/stata-core/SKILL.md`
- `build/generated/stata-packages/SKILL.md`
- `build/generated/stata-c-plugins/SKILL.md`
- `~/.codex/skills/stata-core/SKILL.md`
- `~/.codex/skills/stata-packages/SKILL.md`
- `~/.codex/skills/stata-c-plugins/SKILL.md`

If you want to update the knowledge, edit the YAML in `content/`, not the generated files.

## Repository Notes

### Description

Builds local Codex skills for Stata from structured YAML and local help sources

### Data and Reuse

No clinical data expected

### Citation

No publication DOI is assigned to this repository. Cite the GitHub repository URL and the commit or release used.

### Contact

Maintainer: Brian W. Locke (`@reblocke`). Use GitHub issues or pull requests for repository-specific questions when the repository is public.
