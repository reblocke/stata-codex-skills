# Implementation request: reliable, efficient Stata skills

Date: 2026-09-24

Status: approved direction, implementation requested. This PR adds only the
request and its [compact goal prompt](stata-skills-refactor-goal.md). It does not
implement the refactor, demonstrate token savings, validate Stata execution, or
authorize publication. Implementation and licensed testing will occur on the
owner's MacBook Air.

## Goal and authority

Make published examples test-backed, reduce unnecessary task-level context, and
strengthen the skills' ability to produce reliable, maintainable scientific code.
Prefer fewer failed attempts over shorter instructions at the expense of safety.

The audited source is `reblocke/stata-codex-skills` at
`15c816bef640ffe0bdf45d9bb0cbc7425c5621b4`. That remains `main` when this request
is prepared. The inspected distribution was `reblocke/codex-personal-skills` at
`e040d02311bff78978835c7760f857f98c95ad1f`; its manifest identifies the same
source commit. Neither fact identifies the version installed on the MacBook Air.
Reconcile subsequent source changes before editing rather than resetting to the
audited commit.

Read `AGENTS.md`, `README.md`, `config/skills.yaml`, `templates/reference.md.j2`,
`templates/skill.md.j2`, `templates/routing.md.j2`, and
`tests/prompts/cases.yaml`. The relevant implementation entry points are
`scripts/render_skills.py`, `scripts/lint_skill_pack.py`,
`scripts/validate_skill_pack.py`, `scripts/release_state.py`, and
`scripts/stata_runner.py`. Discover additional files as needed. These are starting
points, not a requirement to read every large module into context.

## Scope and invariants

Preserve all three public entry points: `stata-core`, `stata-packages`, and
`stata-c-plugins`. Existing canonical paths and compatibility aliases must remain
usable, directly or through explicit compatibility pages. Preserve consequential
built-in/community method boundaries and established package choices.

Reviewed `content/` YAML remains the executable publication authority. Extend the
schema, renderer, validator, tests, and documentation only as required. Generated
content remains ignored in this repository and must not be patched by hand.
New fields and generated members must participate in existing schema validation,
complete-tree checks, deterministic rendering, and receipt digest binding.

Preserve package isolation, verified dependency sources, timeouts, process cleanup,
natural-exit requirements, fresh run evidence, receipt freshness, ownership and
path protections, transactional replacement, and rollback. No broad rewrite of
the publisher, launcher, lock machinery, or process guard. No blanket package or
Python dependency refresh. Any additional dependency or lock update requires a
separate reviewed decision; do not invent successful package verification.

Use synthetic fixtures only. Do not modify other analysis repositories, clinical
definitions, estimands, missing-data policies, seeds, public command interfaces,
or output schemas as an incidental cleanup. Examples may illustrate alternative
policies, but must label them. Do not minify generated Stata code or abbreviate
meaningful names to save tokens.

No automatic merge, installation-path migration, publication, or distribution
update. The owner must separately authorize those operations. Keep patient data,
credentials, license-bearing output, full runtime logs, and machine-specific
private paths out of public commits and PR comments.

## Confirmed starting defects and coverage gaps

These are source-review findings at the audited commit, not fresh runtime results.

| Starting point | Finding to resolve |
|---|---|
| `content/core/programming-basics.yaml` | Published `foreach` and `forvalues` examples use inline brace bodies; the separate smoke uses multiline syntax. |
| `templates/reference.md.j2` | Publishes `syntax_patterns`; the validator executes a separately authored `smoke_test`. Alternative or incomplete patterns are combined into one code block. |
| `content/core/data-management.yaml` | Validation describes a `joinby` count; the smoke executes merge/collapse without `joinby`. |
| `content/core/workflow-best-practices.yaml` | Validation describes two clean runs; the smoke is a single seeded save/reload workflow. |
| `content/core/linear-regression.yaml` | The sample-check claim is not challenged with deliberately excluded observations. |
| `content/core/advanced-programming.yaml` | The simple mean smoke does not exercise the broader advertised qualifier/weight interface. |
| `content/core/variables-operators.yaml` | A guarded classification maps missing input to false; identifier advice needs representability and original-format qualifications. |
| `content/packages/data-manipulation.yaml` | Broad utility coverage exceeds the package-specific preflight and smoke coverage. |
| Generated category indexes | Paths, aliases, command cues, and full trigger descriptions are repetitive; a category read is required even when a reference is already known. |

For source attribution, inspect these paths at the
[audited commit](https://github.com/reblocke/stata-codex-skills/tree/15c816bef640ffe0bdf45d9bb0cbc7425c5621b4).
The
[distributed programming example](https://github.com/reblocke/codex-personal-skills/blob/e040d02311bff78978835c7760f857f98c95ad1f/skills/stata-core/references/programming-basics.md)
contains the same inline loops. Verify language behavior using the installed
Stata help and the official
[foreach](https://www.stata.com/manuals/pforeach.pdf) and
[forvalues](https://www.stata.com/manuals/pforvalues.pdf) manuals. Review the
[Stata identifier discussion](https://blog.stata.com/2011/04/18/merging-data-part-1-merges-gone-bad/)
when writing identifier fixtures. Do not vendor proprietary help or manuals.

## Checkpoint 0: establish the baseline

Create a new implementation branch or worktree; do not stash, reset, overwrite,
or commit unrelated work. Record source SHA, dirty state, generated-tree digest,
actual hardware architecture, macOS, Stata edition/version, Python/tool versions,
and client version. Record the actual skill discovery path privately. Do not
assume an Apple Silicon architecture, a Stata edition, or a client search path.

Inventory the complete generated route/alias set rather than trusting a README
file count. Run the baseline offline gate and the available licensed baseline.
Distinguish a pre-existing failure from a regression. Freeze a small independent
synthetic task suite before modifying routing; specify fixtures, expected
outcomes, budgets, and tolerances in advance.

Acceptance:

- [ ] Baseline and environment records exist without altering installed skills.
- [ ] Every requested gate has an explicit pass/fail/not-run/blocked status.
- [ ] Critical task definitions and correctness oracles are independent of the
      text being compressed. Preserve the original benchmark inputs.

## Checkpoint 1: repair examples and evidence claims

Correct the published inline loops under the default newline delimiter. Present
unrelated examples separately. Label fragments and their required context,
particularly incomplete `rclass`/`eclass` program bodies. Keep scientific defaults
unchanged except where an explicitly described example policy is being repaired.

Reconcile every changed validation description with the implemented assertions.
For the high-priority cases below, add substantive coverage instead of merely
removing the claim. Retain a correctness-repaired checkpoint before compression.

Acceptance:

- [ ] Tests exercise the exact published loop bodies with documented fixtures.
      A deliberately restored inline-body defect fails the appropriate test.
- [ ] `joinby` tests establish known within-key Cartesian counts; merge tests
      establish disposition of matched, master-only, and using-only rows.
- [ ] Collapse tests establish numeric results and group identifiers, not just
      the number of resulting rows.
- [ ] Two independent clean Stata sessions reproduce declared substantive
      results. Do not compare timestamp-bearing logs for byte identity.
- [ ] Regression fixtures distinguish estimation, prediction, and evaluation
      samples. Test intentional exclusions and out-of-sample predictions;
      sample identity is required only where the contract actually requires it.

## Checkpoint 2: make examples test-backed

Extend reviewed YAML with stable example IDs, language, runnable/fragment kind,
minimum version, dependency/prerequisite information, a canonical code body,
fixture/assertion references, and expected return status. Choose a small schema
that fits the existing implementation; do not introduce a new language framework.

Render and execute the same authoritative code body. Make any newline or
indentation normalization explicit and deterministic. Independent setup and
expected-result fixtures must not contain a second implementation of the example.
Derive tests from example IDs and code, not code from independently edited prose.
Tests must also check the actual rendered code bytes against the authoritative
body so that a template mutation cannot escape the contract.

Migrate the core/package entries named in the findings table and
`content/core/bootstrap-simulation.yaml`. Classify remaining published blocks
honestly as runnable, fragment, or illustrative;
record their actual coverage. Do not relabel all examples as fragments to avoid
tests. Non-migrated material must not acquire an unsupported execution claim.
Keep Stata, Mata, C/C++, and shell examples in their correct language contexts.

Acceptance:

- [ ] Duplicate IDs, unresolved example/test references, conflicting legacy/new
      example authorities, and unclassified changed blocks fail offline checks.
- [ ] Every migrated runnable example has a stable test ID. Every fragment
      names its necessary variables, prior model, enclosing program, or setup.
- [ ] A fixture changes the documented example to an incorrect value or syntax
      and demonstrates rejection, not only successful rendering.
- [ ] Semantic tests cover ordinary and extended numeric missing values,
      duplicate/missing keys, unmatched records, empty inputs/samples, `if`/`in`,
      supported weights, returned results, and data side effects as applicable.
- [ ] An unsupported weight or invalid argument fails with the declared Stata
      return code. Expected-error cases verify postconditions as well as `_rc`.
      Missing logs, unexpected failures, and timeouts cannot count as expected errors.
- [ ] Unknown-preserving and explicitly missing-as-false classifications are
      separately named and tested. Not-measured versus measured-negative states
      are distinguished when the fixture provides that information.
- [ ] Identifier fixtures retain leading zeros and long original identifiers;
      unsafe conversion is rejected or avoided. Widening already damaged numeric
      data is not described as recovery.
- [ ] Counts, keys, and categories use exact checks. Numerical checks reject
      unexpected missing/nonfinite results and use predeclared, justified
      tolerances. Do not enlarge tolerances to hide a discrepancy.

## Checkpoint 3: reduce unnecessary loading

Keep roots compact. Condense category tables to task intent, distinctive command
cues, and one canonical link. Retain complete aliases in authoritative metadata
and provide a usable lookup path for them. A known command/reference may go
directly to its technical reference after the applicable root safety contract.
Do not force recursive loading of every related reference.

A lookup helper is optional. Prefer a compact index unless a helper demonstrates
a better task-level loading path. Any helper returns candidate paths and relevant
boundaries; it never installs software, executes retrieved text, or selects an
unresolved scientific estimator. Do not silently weaken independently authored
routing expectations to make compression pass.

Split the broad data-utility reference into focused recipes or references, while
retaining its existing path as a compatibility landing page. Preflight and
installation guidance must correspond to the requested package and its actual
dependencies. Reuse reviewed locks; unavailable verification remains explicit.

Define a small common data/error-handling contract once in reviewed configuration
and render the applicable rules into each root. A package task should not need to
load the entire core catalog to receive those safeguards.

Acceptance:

- [ ] Existing exact-command/alias cases and consequential method boundaries
      remain covered; old links resolve without ambiguity or broken redirects.
- [ ] Known-reference tasks avoid the category hop without skipping safety rules.
- [ ] A single utility task does not read installation instructions for all six
      utilities or falsely claim that a `gcollapse` smoke validates them all.
- [ ] Installation details remain conditional on execution needs and authorization;
      missing runtime dependencies do not prevent code drafting.
- [ ] The fixed comparable task paths use fewer instruction tokens than the
      correctness-repaired checkpoint, measured as described below. Exceptions
      are explained individually. Do not delete safeguards to meet a target.

## Checkpoint 4: strengthen workflow guidance

Add focused references or strengthen existing ones under the three roots. Reuse
related content instead of adding top-level skills or parallel frameworks.

| Workflow | Required contract |
|---|---|
| Data contracts and cohort transformations | Observation grain, keys, units, identifier representation, missingness policy, join cardinality, time-window endpoints, deterministic tie handling, row-disposition accounting, immutable inputs, and output schema. |
| Safe code review/refactoring | Declared behavior and interface before editing; minimal patches; explicit allowed data mutations; targeted regression/negative tests; no silent change to scientific choices. |
| Estimation and result extraction | Correct use of estimation/prediction samples, immediate preservation of volatile returned results, named rather than positional result access where supported, model identity, and stable output schemas. |
| Simulation and resampling | Scenario/replication IDs, fixed RNG policy, correct resampling unit, convergence and failure ledger, explicit denominators, and predeclared summaries including Monte Carlo uncertainty where relevant. |

Provide context-sensitive modes for explanation, editing, execution/debugging,
and scientific analysis. An explanation-only request must not trigger package
installation, data execution, or a full methods review. A refactoring request must
not become permission to redesign the estimand or cohort.

Acceptance:

- [ ] Each workflow has positive, negative, and boundary routing cases plus
      executable synthetic tasks where applicable.
- [ ] Reusable programs declare inputs, outputs, qualifiers, dependencies, return
      values, failure behavior, and permitted state changes.
- [ ] Optional code warnings include location and rationale. They do not
      automatically repair scientific choices or introduce a full Stata parser.
- [ ] Reuse the existing runner. Any optional summary is bounded, backward
      compatible, and separates process success from assertion/numerical success.
      Never assume regex redaction makes arbitrary clinical logs safe to publish.

## Checkpoint 5: measure actual behavior

Use at least 24 small synthetic tasks, including the defects and workflows above,
explanation-only cases, package-unavailable cases, deliberate failures, direct
command/alias routing, and consequential method boundaries. Separate routing
correctness, executable correctness, and scientific appropriateness.

Compare the original, correctness-repaired, and compressed checkpoints with the
same client/model configuration, permissions, fixtures, and Stata/package
versions. At minimum repeat the predeclared critical subset three times per
variant. Set explicit trial/token/time budgets; do not run indefinitely to find
a favorable result. Resume incomplete evaluations using recorded trial IDs.

Record paths actually loaded, instruction tokens, tool-output tokens where
available, repair iterations, total usage, prohibited behavior, and completion
time. Keep actual client telemetry separate from tokenizer estimates. A proxy
must name its tokenizer/version, counting rule, and covered text. Missing usage
is null/unavailable, not zero; file size is not billed usage. Do not count cached
usage twice. Avoid a new dependency merely to report an unlabeled approximation.

Acceptance:

- [ ] All required deterministic semantic tests pass. Critical safety cases show
      no prohibited behavior in the candidate trials; every discrepancy is reviewed.
- [ ] Paired results show routing/code success alongside loading cost. Report
      failures and missing trials, not only successful-task averages.
- [ ] Compression savings are compared with the correctness-repaired version;
      do not attribute the benefit of syntax fixes to shorter routing.
- [ ] Lower aggregate instruction-loading cost is demonstrated for the fixed
      comparable tasks without regressions on their required outcomes. Newly
      added workflows are reported separately to avoid changing the denominator.
- [ ] Static prompt-fixture lint is not described as a fresh-agent evaluation.
      A small passing benchmark is not proof of universal correctness.

## MacBook Air validation and handoff

Use the existing environment and licensed executable after inspecting them. No
edition, application path, unlocked/locked-screen behavior, or client discovery
path should be guessed. Use a modest worker count initially; `TEST_JOBS=2` is a
starting configuration, not a claim about the machine's capacity.

From the implementation checkout, establish the environment and offline status:

```bash
make bootstrap && make doctor && make check TEST_JOBS=2 && git diff --check
```

For targeted iteration on migrated core examples, the existing target is:

```bash
make validate-core TEST_JOBS=2 \
  CORES="programming-basics advanced-programming variables-operators data-management workflow-best-practices linear-regression bootstrap-simulation"
```

Before reporting licensed acceptance, run the complete default gate:

```bash
make validate TEST_JOBS=2 KEEP_WORKDIR=1
```

The Makefile and validator at the checkout are authoritative. Integrate new
required deterministic example tests into the existing default gate without
removing previous coverage; document any additional explicit evaluation command.
Targeted tests alone do not replace the complete gate or mint a publication receipt.
`KEEP_WORKDIR=1` is for failure diagnosis; treat any retained directory as private.

Qualify the actual launcher with both a successful fixture and a deliberate
Stata error. Require natural exit, current-run completion evidence, and the
expected Stata status. A timeout remains a failure even when a marker exists.
Keep the existing runner contract. Never kill unrelated Stata sessions. Any
locked-screen claim requires a separately observed owner-coordinated test;
do not lock the owner's machine automatically or conflate screen lock with sleep.

Plugin runtime remains separate from the default gate. When applicable to actual
native changes and expressly authorized, use:

```bash
make validate-plugin-runtime TEST_JOBS=2 KEEP_WORKDIR=1
```

Compilation is not runtime validation. Record an unperformed runtime check as
not run; it need not block a change that does not alter native behavior.

Fresh-agent evaluation may need a temporary, isolated client environment. Inspect
supported client controls before using it; do not modify the normal skill
installation to create the comparison. Verify the generated skill names, loaded
paths, and source/tree digest rather than assuming discovery from file presence.
Compare documented legacy and current discovery locations only against the
actual client. No automatic relocation or conflicting same-name copies.

Use three completion states:

1. **Implementation prepared:** code, fixtures, checks, and documentation exist;
   pending execution remains explicit.
2. **Locally accepted:** required offline, licensed, and behavioral gates passed
   on the recorded MacBook Air environment, with matching source/tree evidence.
3. **Release pending or approved:** installation and cross-repository distribution
   have their own owner approval. Local acceptance is not publication permission.

Do not weaken a required gate when Stata, a package source, client telemetry, or
another prerequisite is unavailable. Continue independent engineering work and
report the specific blocked criterion. A labeled token proxy can support only
the corresponding proxy claim; it cannot replace actual execution evidence.

## Release boundary and completion evidence

This implementation request stops before production publication. After separate
owner approval, use the existing receipt-checked publisher and the distribution
repository's reviewed updater. Use a clean source commit and a fresh matching
receipt. Revalidate after source/tree changes or receipt expiry. Update
`codex-personal-skills` in a separately reviewable change; never copy generated
files there manually. Verify actual installed discovery and a fresh-agent smoke,
and retain a verified rollback path. Do not invent an updater CLI.

At every checkpoint retain a small commit and a sanitized evidence summary.
Final handoff must map each acceptance item to a test/result or a precise blocker,
identify changed paths and any justified scope deviation, and record baseline,
repaired, and candidate SHAs, tree digest, environment, commands and exit status,
coverage/test IDs, numerical comparisons, behavioral outcomes, and the token
measurement method. Keep machine-specific logs and licensed evidence private;
provide only safe summaries and identifiers in the PR. Do not mark unchecked
acceptance criteria complete merely because the intended files exist.
