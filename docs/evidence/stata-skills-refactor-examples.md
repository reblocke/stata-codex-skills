# Stata skills refactor: test-backed examples checkpoint

Code checkpoint: `29e8535c69713f0019fe13949b0584a87a19a647`.
Source digest at that commit:
`59a126f247b582f165f807c7ff094f641feb7d05675d0e32a05a892943915ee3`.
Generated-tree digest:
`4423c576f758a041ae6e9d3dc70d47ab1f5156ef22b2f08ac1ad38335760e85b`.

Eight source entries were migrated. Sixteen runnable Stata code bodies have
stable IDs, language, minimum version, prerequisites, fixture links, and
declared return status. The renderer and licensed validator consume the same
reviewed YAML body. Offline tests compare every rendered code fence byte for
byte with its source body and verify that the Stata wrapper writes that body
unchanged apart from one explicit terminal newline.

The other 55 references have reviewed per-block classifications: 208 fragments
and 10 illustrative blocks, all without independent execution claims. Migrated
references also include 18 labeled fragments. Stata, Mata, C, C++, shell, and
plain-text fences are explicitly distinguished.

| Check | Result |
| --- | --- |
| `make check TEST_JOBS=2` | Pass; 18 test modules and deterministic tree rendering. |
| Targeted licensed core validation | Pass for all seven migrated core entries. |
| Targeted licensed package validation | Pass for the migrated `gcollapse` example with the existing reviewed lock. |
| Schema rejection tests | Duplicate IDs, missing fixtures, conflicting legacy/new authorities, absent classifications, and invalid kinds fail offline checks. |
| Expected Stata errors | Duplicate and missing keys return 459; empty regression returns 2000; unsupported frequency weight returns 101; postconditions are asserted. |
| Numerical mutation | Changing the published collapse mean to a sum in a private trial fails its fixture assertion. |
| Launcher and log behavior | Current-run markers and natural exit remain required; expected errors are captured and checked rather than treated as successful Stata processes. |

The first targeted run revealed a relative child do-file path error in the new
validator; it was fixed and the complete targeted set was rerun successfully.
The complete licensed default gate and fresh-agent behavioral comparison remain
pending. No receipt or publication claim is made by this checkpoint.
