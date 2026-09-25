# Stata skills refactor: local acceptance record

The user's request authorizes implementation and local validation on this
MacBook Air. It does not authorize merging, publishing, changing installed
skills, or updating the distribution repository. The linked specification
defines the checks below; this record reports observed results, not inferred
publication readiness.

## Checkpoints and environment

| Point | Commit | Generated-tree SHA-256 | Evidence |
| --- | --- | --- | --- |
| Original source, frozen tasks | `f2fade1` (`15c816b` code) | `1bd4eb3fb4357dd6064c07e864f0231a80c6adb1004871c7d988b4b9e105762d` | [Baseline](stata-skills-refactor-baseline.md) |
| Correctness repaired, before compression | `234cfa65` | `1b5faf2bd8559870c2e8653d3eeadd6b01c57502efaa59d532979a20ad06ae8a` | [Repair](stata-skills-refactor-repaired.md) |
| Test-backed example schema | `29e8535` | `4423c576f758a041ae6e9d3dc70d47ab1f5156ef22b2f08ac1ad38335760e85b` | [Examples](stata-skills-refactor-examples.md) |
| Routing and utility recipes | `0fd9938` | `4debb5158b3f1635c23937acde2184d86a7f0aecd38dbd48c823dcfa502b1718` | [Routing](stata-skills-refactor-routing.md) |
| Research workflows | `885d1b0` | `e674f62f7ea74021df3d8e3224047888e03299682999cc3608214d1cc36a1d18` | [Workflows](stata-skills-refactor-workflows.md) |
| First compressed candidate and recipe precision | `b239935` | `84fdc5ead21dc1b59c569310c454ae24b1d9d07c4ad15eaba910b59d2a23fe9e` | This record |
| Final direct-route clarification | `b51a506` | `eb8150f3d4ae664c7b668f9d62460eaef6b5451ec5deeecfde80310cb13bd382` | This record |

The branch is `codex/stata-skills-refactor-implementation`, based on the
documentation request commit `838e5e2`. Its final code Git tree is
`4268029211aa9d8b931ed46bb47dd71e5251409f`. The generated-tree digest
above was verified at the code checkpoint. The source-bound receipt for the
final evidence commit must be minted afterward and is reported in the handoff.
No generated skills are tracked.

The observed host is MacBook Air `Mac14,15`, arm64, macOS 26.6.2, with
licensed StataBE 18.0.179 at the inspected application path. The pinned build
uses Python 3.11.10, uv 0.11.11, Jinja2 3.1.6, and PyYAML 6.0.3. The
installed global Codex CLI is 0.46.0. Behavioral trials used an isolated
Codex CLI 0.156.1 and private skill homes; they did not change normal skill
discovery or installation.

## Prescribed gates on final source

| Gate | Result | Evidence and scope |
| --- | --- | --- |
| `make bootstrap && make doctor && make check TEST_JOBS=2 && git diff --check` | Pass, exit 0 | Pinned environment, prerequisites, 18 offline test modules, deterministic double render, repository scan, and whitespace check. |
| `make validate-core TEST_JOBS=2 CORES='programming-basics advanced-programming variables-operators data-management workflow-best-practices linear-regression bootstrap-simulation'` | Pass, exit 0 | Seven targeted core entries and their published examples. |
| `make validate TEST_JOBS=2 KEEP_WORKDIR=1` | Pass on `b51a506` before the final evidence commit | Static, 38 core, 19 packages and compatibility alias, plus plugin compilation. Retained runtime directory is private. The final source-bound repeat is recorded in the handoff. |
| Launcher success and deliberate assertion error | Pass | Both natural exits with fresh completion markers; observed Stata status 0 and 9 respectively. A separate isolated package preflight had OS/Stata status 0/0. |
| Plugin runtime | Not run | No native behavior changed. Compilation above is not runtime acceptance. |
| Locked-screen execution | Not run | No owner-coordinated locked-screen test was requested. |
| Merge, publish, installed-skill relocation, distribution update | Not run | These remain outside authorization. |

## Acceptance criteria by checkpoint

### 0. Baseline

1. **Baseline and environment:** Pass. The clean branch start, machine and
   Stata/client versions, 95-file original generated-tree inventory, source
   digest, private discovery observation, and baseline offline/licensed gates
   are recorded in the [baseline evidence](stata-skills-refactor-baseline.md).
2. **Gate status:** Pass as a reporting requirement. Every requested gate is
   classified above; unperformed gates are not called passed.
3. **Independent benchmark:** Pass. The 24 synthetic task prompts, oracles,
   tolerances, budgets, and variant plan were committed at `f2fade1` before
   code changes. Their SHA-256 is
   `08ad7cebd548b8dded34821ee5b1da9e68a71a222e16daf60d284dac726feba2`.

### 1. Syntax and substantive test repair

1. **Exact loops and mutation test:** Pass. `test_published_examples` checks
   source/render/executed-body equality; restoring either inline `foreach` or
   `forvalues` yields Stata code 198 and fails offline lint.
2. **Join and merge:** Pass. Synthetic fixtures assert 2 × 3 plus 1 × 1
   `joinby` pairs and all three merge dispositions, including unmatched rows.
3. **Collapse:** Pass. The licensed fixture asserts unique group keys and
   group means 15 and 30; the independent behavioral task uses 15 and 10.
4. **Clean rerun:** Pass in the deterministic fixture. Two fresh Stata
   processes reproduce substantive counts and values; timestamped logs are
   not compared as bytes.
5. **Sample distinctions:** Pass. Regression fixtures separately identify
   `e(sample)`, prediction availability, evaluation eligibility, deliberate
   exclusions, and a future row.

### 2. Example authority and semantics

1. **Schema rejection:** Pass. Offline checks reject duplicate IDs, missing
   fixture references, conflicting authorities, and unclassified changed
   blocks.
2. **Stable runnable coverage and fragments:** Pass. Twenty runnable Stata
   examples have stable fixture IDs and reviewed code bodies. Nineteen
   fragments in migrated references describe prerequisites. Fifty-five
   remaining entries classify 208 fragments and 10 illustrative blocks by
   language and kind, without claiming independent execution.
3. **Independent mutation:** Pass. A private mutation from a published
   collapse mean to a sum fails the fixture assertion; published loop
   mutations fail as above.
4. **Semantic edge cases:** Pass in the deterministic suite. Fixtures cover
   ordinary and extended missing values, duplicate/missing keys, unmatched
   records, empty sample, `if`/`in`, analytic weights, returned values, and
   data side effects where relevant.
5. **Expected errors and postconditions:** Pass. Unsupported frequency weight
   returns 101, duplicate/missing key returns 459, and empty regression
   returns 2000; wrappers require expected `_rc`, a current log, and unchanged
   data where declared.
6. **Unknown versus false:** Pass. Separate examples preserve unknown for `.`
   and `.a` or explicitly code missing-as-false. Cohort fixtures distinguish
   not measured from measured negative.
7. **Identifier fidelity:** Pass. Eighteen-character string IDs retain
   leading zeros and long digits. Guidance does not claim that widening an
   already rounded numeric ID recovers lost information.
8. **Numerical strictness:** Pass. Counts, keys, and categories are exact;
   finite numeric assertions use the predeclared `1e-10` tolerance. No
   tolerance was enlarged after failure.

### 3. Routing and loading

1. **Compatibility and method boundaries:** Pass in offline route/link tests.
   The complete original canonical routes and alias set remain resolvable;
   independently written prompt cases retain estimator boundaries.
2. **Direct known-reference path:** Pass in generated-tree checks. Each root
   has the applicable shared contract, and known commands may go directly to
   the technical reference without a category read.
3. **Focused utilities:** Pass structurally. The historical data-management
   package route is a landing page; six focused recipes give package-specific
   instructions. A `gcollapse` smoke is not described as numerical validation
   of the other five packages.
4. **Conditional installation:** Pass. Isolated preflight checks matched the
   specific commands and dependencies; drafting does not require availability
   or trigger installation.
5. **Lower comparable instruction loading:** See behavioral results below.

### 4. Research workflows

1. **Positive, negative, boundary cases:** Pass structurally and in licensed
   synthetic fixtures. Twelve independently authored routing cases and four
   executable examples cover data contracts, safe refactoring, result
   extraction, and simulation accounting.
2. **Reusable program contracts:** Pass. Reviewed references state inputs,
   outputs, qualifiers, dependencies, return values, failures, and permitted
   state changes; `rclass` fixtures check valid and invalid calls.
3. **Warning scope:** Pass by source review. Guidance asks for locations and
   reasons, with no automatic scientific redesign or parser framework.
4. **Runner safety:** Pass. The existing bounded runner contract is reused;
   process status and Stata assertion status remain distinct. Raw logs stay
   private rather than being treated as automatically safe to publish.

### 5. Behavioral comparison

1. **Deterministic semantics and safety:** Pass for the prescribed tests and
   observed final candidate. The default offline and licensed gates passed.
   The final candidate made no prohibited install, network, or explanation-only
   execution command in 54 trials. All 27 execution-requested trials reached
   a final natural Stata exit with status 0 and asserted task outputs; each
   clean-rerun trial had two successful independent sessions. The three
   unsupported-weight behavioral calls returned 101. Four observed Stata
   repair cycles in the final variant were resolved before its final answers.
2. **Paired routing, code, and cost:** Pass with the exceptions below. The
   [per-trial table](stata-skills-refactor-trials.csv) includes all 216 trials,
   loaded paths, route and completion status, proxy costs, client telemetry,
   repairs, process outcomes, and elapsed time. The first candidate's routing
   decline and the repaired RD timeout remain in that table.
3. **Correct comparator:** Pass. Savings are measured against `234cfa65`,
   which had the syntax/test repairs but no routing compression. Original
   source metrics are reported separately; syntax fixes are not credited as
   loading savings.
4. **Lower loading without required-outcome regression:** Pass for the frozen
   comparable task outcomes observed here. The final variant loaded 44/44
   expected comparable routes versus 43/44 repaired, completed 44/44 versus
   43/44, and reduced the instruction-content proxy 16.4%. Its executable
   tasks reached the required final Stata checks; method boundaries and
   explanation-only tasks kept their stated limits. The no-file safe-refactor
   prompt was an input boundary, not a completed patch; the licensed synthetic
   example tests the executable refactor contract separately.
5. **Evidence limit:** Pass as a reporting requirement. Prompt-fixture lint
   and generated-tree tests are distinguished from the isolated fresh-client
   trials. These small synthetic trials do not establish universal correctness
   or clinical-data suitability.

## Behavioral trial design and results

The immutable suite contains 24 tasks, including 15 critical tasks repeated
three times and nine other tasks once: 54 trials per variant, 162 originally
planned. The route repair triggered a fourth, separately labeled 54-trial
variant. All 216 attempts are retained. All variants use the same isolated
CLI 0.156.1, `gpt-6-sol` at high reasoning, StataBE executable, trial prompt
prefix, 180-second time limit, and 8,000 instruction/12,000 tool-output token
proxy budgets. Each trial starts with a fresh private client home and immutable
variant skills. The four newly added workflow prompts account for 10 trials
per variant and are separate from the 44 fixed comparable trials. Raw client
events and synthetic work directories remain in a private local cache.
From that private evidence directory, the commands were
`eval-env/bin/python eval-harness.py --workers 2`, then
`eval-env/bin/python eval-harness-routed.py --variant candidate-routed --workers 2`;
`eval-env/bin/python eval-recount.py` recomputed all 216 records from the
retained event streams. The second harness differs only in the new variant
label and the same focused-package route allowances used for the first
candidate.

The instruction-loading proxy counts exact full skill-file bodies and partial
skill-content search output returned by completed CLI command calls, encoded
with `tiktoken` 0.12.0 and `o200k_base`. It removes path listings appended
to combined read commands when the full file body can be identified; residual
unmatched read output is counted conservatively. It is a tokenizer proxy for
covered skill text, not billed-token usage. Total tool-output proxy counts
all completed command output. Actual CLI `turn.completed` input, cached input,
and output telemetry are reported separately; cached input is a subset of
input and is never added twice. Repair iterations count failed observed Stata
invocations followed by a successful rerun. Missing telemetry remains blank.
The private trial harness SHA-256 values are `30c45d67e73a5953627e72cf0f7c29ef05c8194a72c44019d3699e11f7b850b5`
for the original three variants and `4d5f6057d7f9e6c349d3b623c3c9a60063f529b7cc5371d911f213920bfc6d0d`
for the isolated follow-up; the latter only adds the variant label and its
package-route allowances. The post-run parser SHA-256 is
`8b4bdf1fcf1befe34ce0d373e0e3425e6d7c62964acd8d1349c096f0305bd8a0`.

| Variant | Completed | Expected route | Timeouts | Instruction proxy, all 54 | Tool-output proxy, all 54 | Repairs | Client input / cached / output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original | 54/54 | 47/54 | 0 | 152,450 | 271,228 | 0 | 7,832,476 / 6,993,792 / 115,704 |
| Correctness repaired | 53/54 | 47/54 | 1 | 166,861 | 300,221 | 3 | 7,338,136 / 6,484,736 / 107,080 (53 available) |
| First compressed candidate | 54/54 | 44/54 | 0 | 130,040 | 255,094 | 2 | 7,354,227 / 6,623,104 / 109,298 |
| Final routed candidate | 54/54 | 54/54 | 0 | 142,656 | 256,279 | 4 | 7,041,872 / 6,331,008 / 104,360 |

All variants had zero instruction or tool-output budget breaches and zero
prohibited commands. The final variant's maxima were 7,191 instruction-proxy
tokens, 10,553 tool-output-proxy tokens, and 168.106 seconds. The repaired RD
trial reached the 180-second limit and has no client usage telemetry. Raw
client usage covers the entire turn and is not a measure of skill loading.

| Fixed comparable pairs | Repaired instruction proxy | Final instruction proxy | Reduction | Expected routes, repaired → final |
| --- | ---: | ---: | ---: | ---: |
| All 44 | 132,062 | 110,436 | 16.4% | 43/44 → 44/44 |
| Both loaded expected route (43) | 128,669 | 108,042 | 16.0% | 43/43 → 43/43 |
| Both turns completed (43) | 128,214 | 108,724 | 15.2% | 42/43 → 43/43 |
| Ten newly added workflow trials, reported separately | 34,799 | 32,220 | 7.4% | 4/10 → 10/10 |

The final variant loaded 191 distinct skill paths across all trials, compared
with 224 for the repaired variant; both medians were four paths per trial.
The paired reductions include every attempt, including the repaired timeout.
Only one task had a higher cost of more than 1,000 proxy tokens in the
comparable set: string-ID fidelity (10,254 → 12,141), where all three final
trials read both variable-type and join guidance and loaded the expected route.
Other task-level increases were `gcollapse` drafting (5,578 → 6,358), which
read the focused gtools recipe plus a compatibility route; `lprobust`
explanation (2,961 → 3,511), which cross-checked core nonparametric guidance;
`joinby` (8,745 → 8,925) and `collapse` (8,745 → 8,762), which varied in
bounded search output. These are included in the aggregate. The gtools trials
read no other utility recipe and did not install or execute an unavailable
package.

The first compressed candidate (`b239935`) completed all 54 turns without a
timeout, over-budget proxy, or prohibited command. It loaded the expected
route in 44/54 trials. On the 44 fixed comparable pairs, its instruction
proxy was 103,125 tokens versus 132,062 for the correctness-repaired version
(21.9% lower), but exact routing fell from 43/44 to 39/44. All three clean
reruns and two string-ID joins chose different references, as did two result
extractions; the three safe-refactor prompts did not open the workflow
reference. These misses led to the reviewed direct-route clarification at
`b51a506`; the first run remains in the per-trial evidence.

The final candidate used the direct-route clarification and loaded all 54
expected routes. Its 27 execution-requested trials ended with successful
natural Stata status and relevant assertions after any local repairs.
Fresh-agent plans distinguished unknown from false, preserved exact string
identifiers, retained `csdid`, `rdrobust`, `psmatch2`, `esttab`, and `lprobust`
boundaries, and kept explanation-only prompts free of execution. All three
result-extraction trials produced asserted two-model tables with named
coefficients and sample sizes. The safe-refactor prompts supplied no do-file;
all three agents asked for the source and made no invented patch. Their actual
patch behavior is therefore not observed in these trials. The separately
licensed `core-safe-refactor` fixture tests row counts, values, group totals,
and output fields on synthetic data.

## Scope and remaining gates

The work stops at local implementation and evaluation. The default licensed
gate validates plugin compilation but not plugin runtime; no native behavior
was changed. Locked-screen behavior was not tested. Publication, installation,
cross-repository distribution, and merge remain pending separate owner
approval. These unperformed release gates do not weaken the reported local
checks.
