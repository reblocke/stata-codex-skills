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

- macOS with a local Stata install in `/Applications/Stata`
- A working Python 3.11+ with `PyYAML` and `Jinja2`
- Network access for:
  - cloning or refreshing the upstream GitHub repo
  - downloading plugin SDK sample files from `stata.com`
  - installing community packages during validation

The checked-in `Makefile` currently defaults to:

```bash
/opt/anaconda3/bin/python3
```

That path is machine-specific. On any machine where it does not exist, or where you want to use a different interpreter, override `PYTHON` explicitly:

```bash
make PYTHON=$(which python3) check
```

If you change the checked-in `Makefile`, keep it consistent with your local Python setup.

## Quick start

Render the reviewed content and run the offline checks:

```bash
cd ~/src/stata-codex-skills
PYTHON=$(which python3)
make PYTHON="$PYTHON" render check
```

Run licensed/network integration validation before publishing:

```bash
make PYTHON="$PYTHON" validate
make PYTHON="$PYTHON" publish
```

Useful targeted validation commands:

```bash
make PYTHON="$PYTHON" check
make PYTHON="$PYTHON" validate-core
make PYTHON="$PYTHON" validate-packages
make PYTHON="$PYTHON" validate-packages PACKAGES="reghdfe rdrobust"
make PYTHON="$PYTHON" validate-plugin-compile
make PYTHON="$PYTHON" validate-plugin-runtime
```

The validator CLI accepts repeatable suites and package selections:

```bash
$PYTHON scripts/validate_skill_pack.py --suite static --suite core
$PYTHON scripts/validate_skill_pack.py --suite packages \
  --package reghdfe --package rdrobust
$PYTHON scripts/validate_skill_pack.py --suite plugin-runtime --keep-workdir
```

`--suite default` is used when no suite is supplied. It runs static checks, the
core smoke test, every package smoke test, and plugin compilation. It does not
execute the plugin. `--keep-workdir` preserves a failed run's temporary
workspace for debugging; successful workspaces are always removed.

If this is the first time you have installed the skills on a machine, restart Codex after `make ... publish`.

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
PYTHON=$(which python3)
make PYTHON="$PYTHON" render check
make PYTHON="$PYTHON" validate
make PYTHON="$PYTHON" publish
```

After `make publish`, the skill folders should exist under `~/.codex/skills/` unless you published to a custom destination. Restart Codex if the skills were not already installed on that machine.

### Publishing to a non-default Codex home

If the machine uses a custom `CODEX_HOME`, publish there explicitly:

```bash
PYTHON=$(which python3)
$PYTHON scripts/publish_local.py --dest "$CODEX_HOME/skills"
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
PYTHON=$(which python3)
make PYTHON="$PYTHON" render check
make PYTHON="$PYTHON" validate
make PYTHON="$PYTHON" publish
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
2. Run `make PYTHON=$(which python3) render check`, `make PYTHON=$(which python3) validate`, and `make PYTHON=$(which python3) publish`.
3. Confirm the skill folders exist in `~/.codex/skills/` or `$CODEX_HOME/skills/`.
4. In the analysis repository, add `AGENTS.md` guidance that names the Stata skills explicitly.

Without step 2, an `AGENTS.md` file can mention the skills but Codex will not be able to load them on that machine.

## Typical workflow

When you update the repo, the normal order is:

1. Edit the reviewed YAML files in `content/`.
2. Regenerate structured cases with `scripts/render_prompt_cases.py`.
3. `make render`
4. `make check`
5. `make validate`
6. `make publish`

Fetching upstream material, harvesting help, scaffolding candidates, and
refreshing locks are explicit maintenance operations. Review their ignored
candidate reports before promoting any source or lock change.

## What each script does

- `scripts/fetch_upstream.py`: refreshes the ignored upstream checkout and records its exact resulting commit in a review candidate without changing curated content
- `scripts/harvest_stata_help.py`: resolves only exact help names or declared globs and writes a reviewable candidate report
- `scripts/scaffold_content.py`: reports missing or empty curated fields and never rewrites content
- `scripts/render_skills.py`: renders the three generated skill folders from the YAML content and Jinja templates
- `scripts/render_prompt_cases.py`: deterministically generates structured routing fixtures from every canonical reference plus boundary cases
- `scripts/refresh_locks.py`: writes ignored lock candidates for explicit review; it never promotes them
- `scripts/verify_locks.py`: verifies checked provenance locks, with optional live local/network checks
- `scripts/lint_skill_pack.py`: validates schema quality, exact provenance, locks, prompt cases, generated routing, and metadata
- `scripts/publish_local.py`: copies generated skill folders into `~/.codex/skills`
- `scripts/validate_skill_pack.py`: runs static lint, Stata smoke tests, package install tests, and plugin compilation tests

## Validation model

`make check` runs repository lint and Python unit tests without invoking Stata.
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

## Current status as of July 24, 2026

### Static and core validation

- Static schema, provenance, lock, prompt-case, and generated-drift lint: pass
- Python unit tests: 38 passed
- Structured routing cases: 76 (63 canonical plus 13 boundary cases)
- Fresh-agent forward routing: 76/76 selected the expected route and no forbidden route
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
- The checked-in `Makefile` default for `PYTHON` is machine-specific and may need an override on fresh machines.
- Package install sources change over time, so validation metadata will still need occasional refreshes.
- Plugin execution under local batch-mode `StataBE` is not yet reliable on this machine.

## Next recommended fixes

The highest-value follow-up changes are:

1. Investigate the plugin runtime hang at the macOS loader boundary before relying on `stata-c-plugins` runtime validation.
2. Decide whether to make the default `PYTHON` in `Makefile` more portable across machines.
3. Decide whether to fold package install URLs and dependency policy into a small reference page so future maintenance is more obvious.

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
