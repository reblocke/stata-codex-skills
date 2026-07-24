#!/usr/bin/env python3
"""Render deterministic structured routing cases from canonical content."""

from __future__ import annotations

from pathlib import Path
import argparse

from libskillpack import (
    CONTENT_ROOT,
    PROMPT_CASES_PATH,
    iter_content_entries,
    load_skill_config,
    write_yaml,
)


BOUNDARY_CASES = [
    {
        "id": "boundary-built-in-regression-diagnostics",
        "prompt": (
            "After regress, run VIF, heteroskedasticity, RESET, link, and "
            "residual diagnostics using built-in Stata commands."
        ),
        "expected_skill": "stata-core",
        "expected_refs": ["stata-core/references/regression-diagnostics.md"],
        "forbidden_routes": ["stata-packages/packages/diagnostics.md"],
        "boundary": True,
    },
    {
        "id": "boundary-reghdfe-package",
        "prompt": (
            "Estimate a model with reghdfe, two absorbed fixed effects, and "
            "clustered standard errors."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": ["stata-packages/packages/reghdfe.md"],
        "forbidden_routes": ["stata-core/references/linear-regression.md"],
        "boundary": True,
    },
    {
        "id": "boundary-built-in-didregress-versus-csdid",
        "prompt": (
            "Use Stata's built-in didregress for a standard group-and-time "
            "difference-in-differences design; do not use csdid."
        ),
        "expected_skill": "stata-core",
        "expected_refs": ["stata-core/references/difference-in-differences.md"],
        "forbidden_routes": ["stata-packages/packages/did.md"],
        "boundary": True,
    },
    {
        "id": "boundary-csdid-versus-built-in-didregress",
        "prompt": (
            "Use csdid for staggered treatment timing and group-time effects; "
            "do not substitute built-in didregress."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": ["stata-packages/packages/did.md"],
        "forbidden_routes": [
            "stata-core/references/difference-in-differences.md"
        ],
        "boundary": True,
    },
    {
        "id": "boundary-manual-rd-versus-rdrobust",
        "prompt": (
            "Implement a regression-discontinuity analysis manually with "
            "built-in regress, bandwidth restrictions, and cutoff interactions; "
            "do not use rdrobust."
        ),
        "expected_skill": "stata-core",
        "expected_refs": ["stata-core/references/regression-discontinuity.md"],
        "forbidden_routes": ["stata-packages/packages/rdrobust.md"],
        "boundary": True,
    },
    {
        "id": "boundary-rdrobust-versus-manual-rd",
        "prompt": (
            "Use rdrobust with robust bias-corrected inference and rdbwselect; "
            "do not replace it with a hand-coded built-in RD regression."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": ["stata-packages/packages/rdrobust.md"],
        "forbidden_routes": [
            "stata-core/references/regression-discontinuity.md"
        ],
        "boundary": True,
    },
    {
        "id": "boundary-built-in-table-versus-estout",
        "prompt": "Create a table with Stata's built-in collect and table commands, without estout.",
        "expected_skill": "stata-core",
        "expected_refs": ["stata-core/references/tables-reporting.md"],
        "forbidden_routes": ["stata-packages/packages/estout.md"],
        "boundary": True,
    },
    {
        "id": "boundary-esttab-versus-built-in-table",
        "prompt": (
            "Export stored regression estimates with esttab to LaTeX; do not "
            "rewrite this as a built-in table or collect workflow."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": ["stata-packages/packages/estout.md"],
        "forbidden_routes": ["stata-core/references/tables-reporting.md"],
        "boundary": True,
    },
    {
        "id": "boundary-ivreghdfe-versus-ivregress-or-reghdfe",
        "prompt": (
            "Estimate IV with high-dimensional absorbed fixed effects using "
            "ivreghdfe; do not route to built-in ivregress or plain reghdfe."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": ["stata-packages/packages/ivreg2.md"],
        "forbidden_routes": [
            "stata-core/references/linear-regression.md",
            "stata-packages/packages/reghdfe.md",
        ],
        "boundary": True,
    },
    {
        "id": "boundary-ado-programming-versus-native-plugin",
        "prompt": (
            "Define an ado-style rclass program with program define and syntax; "
            "no native C plugin or stata_call entry point is needed."
        ),
        "expected_skill": "stata-core",
        "expected_refs": ["stata-core/references/advanced-programming.md"],
        "forbidden_routes": [
            "stata-c-plugins/references/plugin-sdk-basics.md"
        ],
        "boundary": True,
    },
    {
        "id": "boundary-native-plugin-sdk",
        "prompt": (
            "Compile a native C plugin with stplugin.h and a stata_call entry "
            "point because an ado-style Stata program is not sufficient."
        ),
        "expected_skill": "stata-c-plugins",
        "expected_refs": ["stata-c-plugins/references/plugin-sdk-basics.md"],
        "forbidden_routes": [
            "stata-core/references/advanced-programming.md",
            "stata-core/references/external-tools-integration.md",
        ],
        "boundary": True,
    },
    {
        "id": "boundary-community-package-lifecycle",
        "prompt": (
            "Inspect and pin a community ado package with which, ado describe, "
            "ssc, and net install; this is package lifecycle work, not native "
            "plugin packaging."
        ),
        "expected_skill": "stata-packages",
        "expected_refs": [
            "stata-packages/packages/package-management.md"
        ],
        "forbidden_routes": [
            "stata-c-plugins/references/packaging_and_help.md"
        ],
        "boundary": True,
    },
    {
        "id": "boundary-native-plugin-package-manifest",
        "prompt": (
            "Package a compiled Stata plugin with stata.toc, a .pkg manifest, "
            "an ado wrapper, help, and platform-specific binaries; this is not "
            "ordinary community ado-package lifecycle management."
        ),
        "expected_skill": "stata-c-plugins",
        "expected_refs": [
            "stata-c-plugins/references/packaging_and_help.md"
        ],
        "forbidden_routes": [
            "stata-packages/packages/package-management.md"
        ],
        "boundary": True,
    },
]


def canonical_cases() -> list[dict]:
    config = load_skill_config()
    cases: list[dict] = []
    for skill_key, _, entry in iter_content_entries(CONTENT_ROOT, config):
        skill = config["skills"][skill_key]
        alias = entry["aliases"][0]
        cases.append(
            {
                "id": f"{skill_key}-{entry['slug']}",
                "prompt": f"I need task-specific guidance for {alias}. {entry['trigger']}",
                "expected_skill": skill["name"],
                "expected_refs": [
                    f"{skill['name']}/{skill['route_dir']}/{entry['slug']}.md"
                ],
                "forbidden_routes": [],
                "boundary": False,
            }
        )
    return sorted(cases, key=lambda case: case["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=PROMPT_CASES_PATH)
    args = parser.parse_args(argv)
    payload = {
        "schema_version": 1,
        "cases": [*canonical_cases(), *BOUNDARY_CASES],
    }
    write_yaml(args.output, payload)
    print(f"Wrote {args.output} with {len(payload['cases'])} structured cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
