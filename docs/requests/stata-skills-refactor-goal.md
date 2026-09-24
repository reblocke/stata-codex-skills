# Goal: reliable, efficient Stata skills

Implement `docs/requests/stata-skills-refactor.md` in
`reblocke/stata-codex-skills` on the MacBook Air. Read that specification and
`AGENTS.md` first. This request PR contains documentation only; the implementation
and all execution evidence must be produced locally. Work on a new implementation
branch without disturbing other work. Do not merge or publish automatically.

## Outcome

Make the exact code taught by the skills test-backed, reduce unnecessary reference
loading, and add focused workflows for maintainable scientific Stata code.
Correctness takes precedence over token reduction. Keep readable Stata code.

## Boundaries

Keep `stata-core`, `stata-packages`, and `stata-c-plugins`; existing routes and
aliases must resolve. Reviewed YAML remains the publication authority. Preserve
locked dependencies, independent routing fixtures, deterministic rendering,
source/tree receipt binding, isolated tests, bounded execution, fresh Stata
return-code evidence, natural exit, rollback, and publication authorization.
Do not rewrite the launcher or transaction machinery, change scientific choices,
update packages silently, edit generated skills manually, or touch other analysis
repositories. Keep runtime logs, license details, and patient data out of Git.
No changes to installed skills or `codex-personal-skills` without separate approval.

## Checkpoints

0. Record the clean source baseline, actual Mac/Stata/client environment, route
   inventory, and baseline checks. Prepare the independent synthetic evaluation
   cases before compressing the skill text. Missing evidence is not a pass.
1. Fix published inline loops and misleading runnable blocks. Test the exact
   published examples. Reconcile validation descriptions with actual tests;
   strengthen joinby, clean-rerun, and estimation/prediction-sample checks.
2. Add stable example IDs, runnable/fragment and language labels, prerequisites,
   fixture/assertion links, and declared return status. Render and execute the
   same authoritative code body. Add independent semantic and failure tests.
   Explicitly distinguish unknown from false and preserve identifier fidelity.
3. Condense routing tables, allow direct known-reference access, retain method
   boundaries, and narrow the broad data-utility package reference. Render a
   small shared safety contract without forcing every task through core.
4. Add or strengthen focused data-contract, safe-refactor, result-extraction,
   and simulation/resampling workflows under existing roots. Explanation-only
   tasks must not require full execution or methods review.
5. Compare baseline, correctness-repaired, and compressed versions on the fixed
   tasks. Record routing, correctness, prohibited actions, repair iterations,
   loaded references, and actual tokens or a clearly labeled proxy. Run the
   MacBook Air gates in the specification. Retain checkpoint commits and report
   evidence against every acceptance criterion.

## Done when

Required deterministic tests pass, the original example defect is detectable,
critical behavioral cases do not regress, and the fixed comparable task paths
show lower instruction-loading cost than the correctness-repaired baseline.
Use the existing `make bootstrap`, `make doctor`, `make check`, and `make validate`
gates as specified. Do not call static checks licensed execution, a smoke test
numerical validation, or text size billed tokens. Report unavailable gates as
blocked/not run; do not fabricate receipts or weaken tests. Leave release pending
until the owner approves publication and any distribution update. The full
specification controls scope, exceptions, and evidence requirements.
