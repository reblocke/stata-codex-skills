# stata-codex-skills

`stata-codex-skills` is a local build repo for Codex skills that help with Stata work.

It does three things:

1. Pulls topic coverage and routing hints from the upstream [`dylantmoore/stata-skill`](https://github.com/dylantmoore/stata-skill) repository.
2. Harvests local Stata help files from `/Applications/Stata/ado/base` and normalizes them into a reusable local reference cache.
3. Renders three Codex-native skills and publishes them into `~/.codex/skills`.

The repo is designed to be a distillation pipeline, not a mirror. Upstream material is used for inventory and gap-finding. Local `.sthlp` files are the primary raw source. Final skill content is generated from structured YAML in `content/`, not edited by hand in the generated skill folders.

This repository is released under the MIT License. See `LICENSE`.

## What gets built

This repo builds three skills:

- `stata-core`: built-in Stata commands, do-files, data management, estimation, graphics, workflow, and Mata
- `stata-packages`: community-contributed packages such as `reghdfe`, `rdrobust`, `estout`, `synth`, and `xtabond2`
- `stata-c-plugins`: Stata C and C++ plugin development, packaging, and validation

Current content inventory:

- 37 core references in `content/core/`
- 20 package references in `content/packages/`
- 6 plugin references in `content/plugins/`

Generated skills are written to `build/generated/` and published into:

- `~/.codex/skills/stata-core`
- `~/.codex/skills/stata-packages`
- `~/.codex/skills/stata-c-plugins`

## Design principles

- Treat the YAML files under `content/` as the source of truth.
- Do not hand-edit generated `SKILL.md` files or generated reference pages.
- Keep provenance explicit. Each content file records which local help topics and upstream files it was distilled from.
- Prefer local Stata help over upstream prose whenever equivalent coverage exists.
- Use isolated validation directories and temporary Stata `PLUS` paths so validation does not pollute your normal Stata environment.

## Repository layout

```text
stata-codex-skills/
├── content/
│   ├── core/          # editable YAML source for built-in Stata topics
│   ├── packages/      # editable YAML source for community packages
│   └── plugins/       # editable YAML source for plugin workflows
├── manifests/         # generated topic/package/plugin maps
├── templates/         # Jinja templates for SKILL.md and reference pages
├── scripts/           # fetch, harvest, scaffold, render, lint, publish, validate
├── tests/             # Stata smoke tests and prompt-trigger examples
├── raw/               # harvested upstream repo + normalized Stata help (gitignored)
└── build/             # generated skill folders before publish (gitignored)
```

Also note:

- `raw/`, `build/`, `tests/tmp/`, and common validation artifacts such as `*.log` are gitignored.
- The repo currently assumes a local Stata install under `/Applications/Stata`.

## Requirements

- macOS with a local Stata install in `/Applications/Stata`
- A working Python 3.11+ with `PyYAML` and `Jinja2`
- Network access for:
  - cloning or refreshing the upstream GitHub repo
  - downloading plugin SDK sample files from `stata.com`
  - installing community packages during validation

On this machine, the default `python3` did not have the required packages, so the `Makefile` is pinned to:

```bash
/opt/anaconda3/bin/python3
```

If your preferred Python already has the dependencies, override it when running `make`, for example:

```bash
make PYTHON=$(which python3) all
```

## Quick start

Build the manifests, harvest local help, scaffold content, render the skills, and lint the result:

```bash
cd ~/src/stata-codex-skills
make all
```

Publish the generated skills into Codex:

```bash
make publish
```

Run the validation suite:

```bash
make validate
```

Useful targeted validation commands:

```bash
/opt/anaconda3/bin/python3 scripts/validate_skill_pack.py --skip-plugin
/opt/anaconda3/bin/python3 scripts/validate_skill_pack.py --skip-packages
/opt/anaconda3/bin/python3 scripts/validate_skill_pack.py --skip-packages --skip-plugin
/opt/anaconda3/bin/python3 scripts/validate_skill_pack.py --package-limit 5
```

## Deploying on other machines and repositories

Codex discovers skills from the local machine, not from the current git repository alone.

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

On a new machine, clone this repo and publish the skills locally:

```bash
git clone https://github.com/reblocke/stata-codex-skills.git ~/src/stata-codex-skills
cd ~/src/stata-codex-skills
make all
make publish
```

If you want validation as well:

```bash
make validate
```

After `make publish`, the skill folders should exist under `~/.codex/skills/` unless you published to a custom destination.

### Publishing to a non-default Codex home

If the machine uses a custom `CODEX_HOME`, publish there explicitly:

```bash
/opt/anaconda3/bin/python3 scripts/publish_local.py --dest "$CODEX_HOME/skills"
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
make all
make publish
```

Run `make validate` as well if you changed package metadata, rendering logic, or anything that affects Stata execution.

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
2. Run `make all` and `make publish`.
3. Confirm the skill folders exist in `~/.codex/skills/` or `$CODEX_HOME/skills/`.
4. In the analysis repository, add `AGENTS.md` guidance that names the Stata skills explicitly.

Without step 2, an `AGENTS.md` file can mention the skills but Codex will not be able to load them on that machine.

## Typical workflow

When you update the repo, the normal order is:

1. `make fetch`
2. `make harvest`
3. `make scaffold`
4. Edit the YAML files in `content/`
5. `make render`
6. `make lint`
7. `make publish`
8. `make validate`

Use `make all` as a shortcut for steps 1 through 6.

## What each script does

- `scripts/fetch_upstream.py`: clones or refreshes the upstream repo and rebuilds the manifest files
- `scripts/harvest_stata_help.py`: copies matching local `.sthlp` files and writes normalized Markdown under `raw/stata-help/normalized/`
- `scripts/scaffold_content.py`: creates or refreshes YAML stubs for topics defined in the manifests
- `scripts/render_skills.py`: renders the three generated skill folders from the YAML content and Jinja templates
- `scripts/lint_skill_pack.py`: validates YAML structure, provenance, generated routing, and frontmatter
- `scripts/publish_local.py`: copies generated skill folders into `~/.codex/skills`
- `scripts/validate_skill_pack.py`: runs static lint, Stata smoke tests, package install tests, and plugin compilation tests

## Validation model

Validation has three layers:

1. Static linting
   - checks required YAML fields
   - checks provenance
   - checks generated files and routing integrity

2. Runtime Stata smoke tests
   - `stata-core`: built-in commands and batch execution
   - `stata-packages`: install each package into a temporary `PLUS` directory and run a minimal example
   - `stata-c-plugins`: compile a plugin and invoke it from batch-mode Stata

3. Prompt-level trigger checks
   - example prompts are stored in `tests/prompts/`

The batch runner watches for a `VALIDATION COMPLETE` marker in the generated Stata log. This is necessary because local `StataBE` on this machine does not reliably exit on its own in `-b do` mode, even when the do-file ends with `exit, clear`.

## Current status as of March 22, 2026

### Static and core validation

- Static lint: pass
- `stata-core` smoke test: pass

### Full package sweep

After tightening package metadata, install sources, and smoke tests, the full 20-package sweep was rerun. Result:

- 20 packages passed
- 0 packages failed

Packages that passed:

- `asdoc`
- `binsreg`
- `coefplot`
- `data-manipulation`
- `diagnostics`
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
- `reghdfe`: installs `require`, `ftools`, and `reghdfe` from the current upstream sources and compiles `ftools`
- `event-study`: uses a valid `eventstudyinteract` example based on the documented `nlswork` workflow
- `synth`: uses a synthetic local panel dataset instead of relying on `webuse synth_smoking`

### Plugin validation

Plugin validation is implemented, but it does not currently pass on this machine.

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
- Validation artifacts such as `.log`, `.doc`, and `.txt` files are left in the repo root when tests run. They are ignored by git, but they are still local clutter.

## Next recommended fixes

The highest-value follow-up changes are:

1. Investigate the plugin runtime hang at the macOS loader boundary before relying on `stata-c-plugins` runtime validation.
2. Decide whether to fold package install URLs and dependency policy into a small reference page so future maintenance is more obvious.
3. Add a small cleanup target for local validation artifacts if you want the repo root to stay visually clean after test runs.

## Generated outputs to inspect

After rendering and publishing, the most important files to inspect are:

- `build/generated/stata-core/SKILL.md`
- `build/generated/stata-packages/SKILL.md`
- `build/generated/stata-c-plugins/SKILL.md`
- `~/.codex/skills/stata-core/SKILL.md`
- `~/.codex/skills/stata-packages/SKILL.md`
- `~/.codex/skills/stata-c-plugins/SKILL.md`

If you want to update the knowledge, edit the YAML in `content/`, not the generated files.
