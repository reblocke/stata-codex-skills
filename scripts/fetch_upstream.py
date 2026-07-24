#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import os
import re
import tempfile

from libskillpack import (
    LOCK_ROOT,
    RAW_ROOT,
    UPSTREAM_REPO_DIR,
    UPSTREAM_REPO_URL,
    read_yaml,
    run_command,
    sha256_file,
    write_yaml,
)


UPSTREAM_ROOTS = {
    "core": Path("plugins/stata/skills/stata/references"),
    "packages": Path("plugins/stata/skills/stata/packages"),
    "plugins": Path("plugins/stata-c-plugins/skills/stata-c-plugins/references"),
}
CANDIDATE_REPORT = RAW_ROOT / "candidates" / "upstream-comparison.yaml"
UPSTREAM_LOCK_PATH = LOCK_ROOT / "upstream.yaml"
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
LOCAL_GIT_TIMEOUT_SECONDS = 30
NETWORK_GIT_TIMEOUT_SECONDS = 120


def exact_commit(value: str) -> str:
    """Accept only an explicit full Git object ID.

    Branches, tags, abbreviated hashes, and expressions such as ``HEAD~1`` are
    deliberately rejected so a refresh cannot silently move to a newer
    revision.
    """

    if not COMMIT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "--upstream-ref must be an exact 40-character hexadecimal commit"
        )
    return value.lower()


def checked_git(
    arguments: list[str],
    *,
    timeout_seconds: int = LOCAL_GIT_TIMEOUT_SECONDS,
    action: str,
) -> str:
    result = run_command(arguments, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            raise RuntimeError(f"{action}: {detail}")
        raise RuntimeError(action)
    return result.stdout.strip()


def initialize_upstream_repo() -> None:
    """Create an empty checkout with the reviewed upstream as its only remote."""

    if UPSTREAM_REPO_DIR.exists():
        checked_git(
            [
                "git",
                "-C",
                str(UPSTREAM_REPO_DIR),
                "rev-parse",
                "--git-dir",
            ],
            action="Raw upstream checkout is not a Git repository",
        )
    else:
        UPSTREAM_REPO_DIR.mkdir(parents=True)
        checked_git(
            ["git", "-C", str(UPSTREAM_REPO_DIR), "init"],
            action="Could not initialize raw upstream checkout",
        )

    remote = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "remote", "get-url", "origin"],
        timeout_seconds=LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if remote.returncode != 0:
        checked_git(
            [
                "git",
                "-C",
                str(UPSTREAM_REPO_DIR),
                "remote",
                "add",
                "origin",
                UPSTREAM_REPO_URL,
            ],
            action="Could not configure the upstream remote",
        )
    elif remote.stdout.strip() != UPSTREAM_REPO_URL:
        raise RuntimeError(
            "Raw upstream checkout origin does not match the configured repository"
        )


def refresh_upstream_repo(upstream_ref: str, *, offline: bool = False) -> None:
    """Check out exactly ``upstream_ref`` in detached-HEAD state.

    Online refreshes fetch only the requested object. Offline refreshes require
    the exact object to be present already. Neither mode resolves a branch or
    pulls a moving default branch.
    """

    initialize_upstream_repo()
    if not offline:
        checked_git(
            [
                "git",
                "-C",
                str(UPSTREAM_REPO_DIR),
                "fetch",
                "--no-tags",
                "--force",
                "origin",
                upstream_ref,
            ],
            timeout_seconds=NETWORK_GIT_TIMEOUT_SECONDS,
            action=f"Could not fetch requested upstream commit {upstream_ref}",
        )

    resolved = checked_git(
        [
            "git",
            "-C",
            str(UPSTREAM_REPO_DIR),
            "rev-parse",
            "--verify",
            f"{upstream_ref}^{{commit}}",
        ],
        action=f"Requested upstream commit is unavailable: {upstream_ref}",
    ).lower()
    if resolved != upstream_ref:
        raise RuntimeError(
            f"Requested upstream commit resolved unexpectedly: {resolved}"
        )

    checked_git(
        [
            "git",
            "-C",
            str(UPSTREAM_REPO_DIR),
            "checkout",
            "--detach",
            "--force",
            upstream_ref,
        ],
        action=f"Could not check out requested upstream commit {upstream_ref}",
    )
    if upstream_commit() != upstream_ref:
        raise RuntimeError("Raw upstream checkout did not land on the requested commit")

    symbolic_head = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "symbolic-ref", "-q", "HEAD"],
        timeout_seconds=LOCAL_GIT_TIMEOUT_SECONDS,
    )
    if symbolic_head.returncode == 0:
        raise RuntimeError("Raw upstream checkout is not detached")
    if symbolic_head.returncode != 1:
        raise RuntimeError(
            symbolic_head.stderr.strip()
            or "Could not verify detached upstream checkout"
        )


def upstream_commit() -> str:
    return checked_git(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "rev-parse", "HEAD"],
        action="Could not read upstream commit",
    ).lower()


def tracked_markdown_files(relative_root: Path) -> list[Path]:
    output = checked_git(
        [
            "git",
            "-C",
            str(UPSTREAM_REPO_DIR),
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            "--",
            relative_root.as_posix(),
        ],
        action=f"Could not inventory upstream path {relative_root}",
    )
    paths: list[Path] = []
    for line in output.splitlines():
        relative_path = Path(line)
        if (
            relative_path.is_relative_to(relative_root)
            and relative_path.suffix == ".md"
            and relative_path.name != "filing-issues.md"
        ):
            source = UPSTREAM_REPO_DIR / relative_path
            if not source.is_file():
                raise RuntimeError(f"Tracked upstream file is missing: {relative_path}")
            paths.append(source)
    return sorted(paths)


def build_inventory() -> dict:
    inventory: dict[str, list[dict]] = {}
    for skill_key, relative_root in UPSTREAM_ROOTS.items():
        inventory[skill_key] = [
            {
                "path": str(path.relative_to(UPSTREAM_REPO_DIR)),
                "sha256": sha256_file(path),
            }
            for path in tracked_markdown_files(relative_root)
        ]
    return {
        "repository": UPSTREAM_REPO_URL,
        "commit": upstream_commit(),
        "inventory": inventory,
    }


def inventory_by_path(inventory: dict) -> dict[str, str]:
    return {
        item["path"]: item["sha256"]
        for items in inventory["inventory"].values()
        for item in items
    }


def build_comparison_report(inventory: dict, upstream_ref: str) -> dict:
    reviewed_lock = read_yaml(UPSTREAM_LOCK_PATH)
    reviewed_files = {
        path: metadata["sha256"]
        for path, metadata in reviewed_lock.get("files", {}).items()
    }
    candidate_files = inventory_by_path(inventory)

    reviewed_paths = set(reviewed_files)
    candidate_paths = set(candidate_files)
    added_paths = sorted(candidate_paths - reviewed_paths)
    removed_paths = sorted(reviewed_paths - candidate_paths)
    changed_paths = sorted(
        path
        for path in reviewed_paths & candidate_paths
        if reviewed_files[path] != candidate_files[path]
    )
    unchanged_paths = sorted(
        path
        for path in reviewed_paths & candidate_paths
        if reviewed_files[path] == candidate_files[path]
    )

    reviewed_repository = reviewed_lock.get("repository", {})
    return {
        "schema_version": 1,
        "report_type": "upstream-comparison",
        "repository": {
            "url": UPSTREAM_REPO_URL,
            "requested_commit": upstream_ref,
            "resolved_commit": inventory["commit"],
            "reviewed_commit": reviewed_repository.get("commit"),
        },
        "summary": {
            "added": len(added_paths),
            "removed": len(removed_paths),
            "changed": len(changed_paths),
            "unchanged": len(unchanged_paths),
        },
        "changes": {
            "added": [
                {"path": path, "candidate_sha256": candidate_files[path]}
                for path in added_paths
            ],
            "removed": [
                {"path": path, "reviewed_sha256": reviewed_files[path]}
                for path in removed_paths
            ],
            "changed": [
                {
                    "path": path,
                    "reviewed_sha256": reviewed_files[path],
                    "candidate_sha256": candidate_files[path],
                }
                for path in changed_paths
            ],
        },
        "candidate_inventory": inventory["inventory"],
        "promotion": {
            "performed": False,
            "note": (
                "Review this ignored report, then promote content and lock changes "
                "in a separate intentional edit."
            ),
        },
    }


def validate_report_path(report: Path) -> Path:
    resolved_report = report.resolve()
    try:
        resolved_report.relative_to(RAW_ROOT.resolve())
    except ValueError as error:
        raise RuntimeError(
            "Comparison report must stay under the ignored raw/ directory"
        ) from error
    return resolved_report


def remove_stale_report(report_path: Path) -> None:
    """Ensure a failed refresh cannot leave a prior report at the target path."""

    try:
        report_path.unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"Could not remove stale comparison report {report_path}: {error}"
        ) from error


def write_report_atomically(report_path: Path, report: dict) -> None:
    """Expose the candidate report only after its complete YAML is on disk."""

    temporary_path: Path | None = None
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            dir=report_path.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        write_yaml(temporary_path, report)
        os.replace(temporary_path, report_path)
    except Exception as error:
        raise RuntimeError(
            f"Could not write comparison report {report_path}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--upstream-ref",
        required=True,
        type=exact_commit,
        help="Exact 40-character upstream commit to fetch and compare.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require the exact commit from the existing raw checkout without fetching.",
    )
    parser.add_argument("--report", type=Path, default=CANDIDATE_REPORT)
    args = parser.parse_args(argv)
    try:
        report_path = validate_report_path(args.report)
        remove_stale_report(report_path)
        if args.offline and not UPSTREAM_REPO_DIR.exists():
            raise RuntimeError(
                "Raw upstream checkout is missing; offline refresh cannot continue"
            )
        refresh_upstream_repo(args.upstream_ref, offline=args.offline)
        inventory = build_inventory()
        if inventory["commit"] != args.upstream_ref:
            raise RuntimeError("Candidate inventory does not match the requested commit")
        report = build_comparison_report(inventory, args.upstream_ref)
        write_report_atomically(report_path, report)
    except RuntimeError as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Wrote ignored upstream comparison {report_path}")
    print("No curated content, lock, or manifest files were changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
