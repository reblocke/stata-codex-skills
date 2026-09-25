# Stata skills refactor: routing and utility recipes checkpoint

Code checkpoint: `0fd993868942f43b5ff0f606010fc64a5afa20d6`.
Source Git tree: `cce3fbcfae402e38bfcda80723fc8002313ade24`.
Generated-tree SHA-256 from the deterministic double render:
`4debb5158b3f1635c23937acde2184d86a7f0aecd38dbd48c823dcfa502b1718`.

The three root skills render the same reviewed data and error contract.
Category tables show intent, distinctive cues, and one link. A separate alias
index retains the full reviewed command and alias vocabulary. Directly known
references can be read after the root contract, without a category hop.

The historical `packages/data-manipulation.md` route is a landing page linking
to six focused recipes. Each recipe renders only its own read-only preflight,
conditional isolated install, dependencies, examples, and validation limit.
The `gcollapse` fixture remains the only numerical utility smoke; the other
five reviewed distributions are lock-verified by the default licensed gate but
are not claimed to have numerical execution coverage.

| Check | Result |
| --- | --- |
| `make check TEST_JOBS=2` | Pass; 18 test modules, lint, deterministic rendering, and repository scan. |
| Route tree | 63 existing canonical references, six new utility recipes, 16 category indexes, three alias indexes, and one compatibility alias; 104 files. |
| Routing fixtures | Existing independently written cases and six new package-specific cases pass lint; consequential method boundaries remain. |
| Safety tests | Each recipe is checked for its own guidance and absence of every other recipe's install command; no install command appears on the landing page. |
| Generated example parity | Every migrated published code body matches its reviewed YAML, including examples moved into recipes. |
| `git diff --check` | Pass before the code commit. |

Task-level instruction-token comparison and fresh-agent behavior are deferred
to the fixed three-variant evaluation. No normal installed skill was changed.
