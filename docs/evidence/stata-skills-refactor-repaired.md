# Stata skills refactor: correctness-repaired checkpoint

Code checkpoint: `234cfa65b1ab7b1ae08f60e1553a2243e227e205`.
Source digest at that commit:
`60d65bcc717f8377862eeddb5bd99add4a901a69acd76341771e5a323b631cac`.
Generated-tree digest:
`1b5faf2bd8559870c2e8653d3eeadd6b01c57502efaa59d532979a20ad06ae8a`.
This checkpoint precedes any routing compression and is the comparison baseline
for instruction-loading cost.

| Check | Result |
| --- | --- |
| `make check TEST_JOBS=2` | Pass; 18 test modules, deterministic rendering, repository scan. |
| Targeted licensed core suite | Pass for programming basics, advanced programming, variables/operators, data management, workflow, and linear regression. |
| Published loops | Exact two source bodies occur in the rendered reference and executed smoke. Both corrected bodies pass; restored one-line `foreach` and `forvalues` each produce Stata return code 198 and fail offline lint. |
| Merge and join | Three merge dispositions and 6+1 within-key Cartesian pairs asserted on synthetic keys. |
| Collapse | Two unique keys and exact means 15 and 30 asserted. |
| Clean rerun | Two independent Stata sessions pass and emit the same substantive count and mean. |
| Regression samples | Deliberately excluded and future rows distinguish estimation, prediction, and evaluation samples. |
| Reusable program | Analytic-weighted `if`/`in` mean checked against 16/3; unsupported frequency weight rejected without data changes. |
| Missingness and identifiers | Ordinary and extended numeric missing values and 18-character string IDs tested. |

The complete licensed gate and behavioral trials remain pending. These targeted
tests do not create a publication receipt.
