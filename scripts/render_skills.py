#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import argparse
import ctypes
import os
import re
import stat
import sys
import tempfile
import uuid

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    REPO_ROOT,
    TEMPLATES_ROOT,
    content_entries_by_skill,
    iter_content_entries,
    load_skill_config,
    read_yaml,
    sha256_file,
    write_text,
)
from release_state import (
    SKILL_FOLDERS,
    tree_digest,
    validate_complete_skill_tree,
)


class RenderTransactionError(RuntimeError):
    """A render failed and requires explicit recovery from a preserved backup."""


@dataclass(frozen=True)
class RenderOutputState:
    exists: bool
    device: int | None = None
    inode: int | None = None
    tree_sha256: str | None = None


REQUIRED_SKILL_KEYS = ("core", "packages", "plugins")
REQUIRED_SKILLS = dict(zip(REQUIRED_SKILL_KEYS, SKILL_FOLDERS, strict=True))


def validate_required_skills(config: dict) -> None:
    """Keep an accidental config edit from publishing a partial skill pack."""

    skills = config.get("skills")
    if not isinstance(skills, dict):
        raise ValueError(
            "skill configuration must define exactly core, packages, and plugins"
        )
    observed_keys = set(skills)
    expected_keys = set(REQUIRED_SKILL_KEYS)
    if observed_keys != expected_keys:
        observed = ", ".join(sorted(str(key) for key in observed_keys)) or "<none>"
        raise ValueError(
            "skill configuration must define exactly core, packages, and "
            f"plugins; observed {observed}"
        )
    for key, expected_name in REQUIRED_SKILLS.items():
        skill = skills[key]
        if not isinstance(skill, dict):
            raise ValueError(f"skill configuration for {key} must be a mapping")
        if (
            skill.get("name") != expected_name
            or skill.get("folder") != expected_name
        ):
            raise ValueError(
                f"skill {key} must retain name and folder {expected_name}"
            )


def validate_render_inputs(config: dict, content_root: Path) -> None:
    """Apply the linter's path/schema checks before constructing output paths."""

    from lint_skill_pack import lint_config, lint_entry, lint_route_aliases

    errors = lint_config(config)
    if errors:
        raise ValueError("render input validation failed: " + "; ".join(errors))

    slug_to_skill: dict[str, str] = {}
    route_paths: set[str] = set()
    for skill_key, path, entry in iter_content_entries(content_root, config):
        skill = config["skills"][skill_key]
        errors.extend(lint_entry(skill_key, path, entry, skill))
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str):
            slug_to_skill[slug] = skill_key
            route_paths.add(
                f"{skill['name']}/{skill['route_dir']}/{slug}.md"
            )
    errors.extend(lint_route_aliases(config, slug_to_skill, route_paths))
    if errors:
        raise ValueError("render input validation failed: " + "; ".join(errors))


def _safe_render_path(root: Path, *parts: str | Path) -> Path:
    """Return a child path only when it remains canonically beneath root."""

    resolved_root = root.resolve()
    candidate = root.joinpath(*parts)
    resolved_candidate = candidate.resolve(strict=False)
    if (
        resolved_candidate == resolved_root
        or not resolved_candidate.is_relative_to(resolved_root)
    ):
        raise ValueError(f"render path escapes staging root: {candidate}")
    return resolved_candidate


def _resolved_output_root(output_root: Path) -> Path:
    """Canonicalize a render target while rejecting ambiguous destinations."""

    requested = Path(output_root).expanduser()
    if requested.is_symlink():
        raise ValueError(f"refusing symlinked render output root: {requested}")
    resolved = requested.resolve(strict=False)
    if resolved.parent == resolved:
        raise ValueError("refusing to render over a filesystem root")
    repository_root = REPO_ROOT.resolve()
    if (
        resolved.is_relative_to(repository_root)
        and resolved != BUILD_ROOT.resolve()
    ):
        raise ValueError(
            "in-repository render output must be build/generated; "
            f"refusing {resolved}"
        )
    return resolved


def preflight_existing_output_root(output_root: Path) -> None:
    """Require an existing target to be empty or recognizably generated."""

    if not output_root.exists():
        return
    if not output_root.is_dir():
        raise ValueError(f"render output root is not a directory: {output_root}")
    if not any(output_root.iterdir()):
        return

    errors = validate_complete_skill_tree(output_root)
    for folder in SKILL_FOLDERS:
        skill_root = output_root / folder
        if not skill_root.is_dir() or skill_root.is_symlink():
            continue
        direct_files = {
            path.name
            for path in skill_root.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        direct_directories = {
            path.name
            for path in skill_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        }
        if direct_files != {"SKILL.md", "PROVENANCE.md"}:
            errors.append(
                f"{folder}: expected only SKILL.md and PROVENANCE.md at skill root"
            )
        route_directories = direct_directories - {"agents"}
        if "agents" not in direct_directories or len(route_directories) != 1:
            errors.append(
                f"{folder}: expected agents/ and exactly one reference directory"
            )
        agents_root = skill_root / "agents"
        if (
            agents_root.is_dir()
            and not agents_root.is_symlink()
            and {path.name for path in agents_root.iterdir()} != {"openai.yaml"}
        ):
            errors.append(f"{folder}: agents/ must contain only openai.yaml")
        for route_name in route_directories:
            route_root = skill_root / route_name
            for path in route_root.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.suffix != ".md"
                ):
                    errors.append(
                        f"{folder}: unexpected reference entry "
                        f"{path.relative_to(skill_root)}"
                    )
        unsafe_links = [
            path.relative_to(skill_root)
            for path in skill_root.rglob("*")
            if path.is_symlink()
        ]
        if unsafe_links:
            errors.append(
                f"{folder}: symbolic links are not generated content: "
                + ", ".join(str(path) for path in unsafe_links)
            )
    if errors:
        raise ValueError(
            "refusing to replace a non-dedicated render output root: "
            + "; ".join(errors)
        )


def capture_output_root_state(output_root: Path) -> RenderOutputState:
    """Fingerprint an accepted target so staging cannot hide a later change."""

    if output_root.is_symlink():
        raise ValueError(f"refusing symlinked render output root: {output_root}")
    if not output_root.exists():
        return RenderOutputState(exists=False)

    preflight_existing_output_root(output_root)
    before = output_root.stat()
    digest = tree_digest(output_root)
    preflight_existing_output_root(output_root)
    after = output_root.stat()
    before_identity = (before.st_dev, before.st_ino)
    after_identity = (after.st_dev, after.st_ino)
    if before_identity != after_identity:
        raise RenderTransactionError(
            "render output root changed while its ownership was being checked"
        )
    return RenderOutputState(
        exists=True,
        device=after.st_dev,
        inode=after.st_ino,
        tree_sha256=digest,
    )


def verify_output_root_state(
    output_root: Path,
    expected: RenderOutputState,
) -> None:
    """Refuse replacement when the accepted root changed during staging."""

    try:
        observed = capture_output_root_state(output_root)
    except (OSError, ValueError) as error:
        raise RenderTransactionError(
            "render output root changed after preflight; refusing replacement"
        ) from error
    if observed != expected:
        raise RenderTransactionError(
            "render output root changed after preflight; refusing replacement"
        )


def _verify_moved_output_state(
    backup_root: Path,
    expected: RenderOutputState,
) -> None:
    """Require the object moved aside to be the exact accepted render root."""

    if not expected.exists:
        raise RenderTransactionError(
            "an unexpected output root appeared during replacement; it remains "
            f"preserved at {backup_root}"
        )
    try:
        observed = capture_output_root_state(backup_root)
    except (OSError, ValueError) as error:
        raise RenderTransactionError(
            "the object moved from the render output path is not the accepted "
            f"generated tree; it remains preserved at {backup_root}"
        ) from error
    if observed != expected:
        raise RenderTransactionError(
            "the object moved from the render output path changed after "
            f"verification; it remains preserved at {backup_root}"
        )


def build_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def normalized_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    compact = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return compact + "\n"


def canonical_route(skill: dict, slug: str) -> str:
    return f"{skill['route_dir']}/{slug}.md"


def prepare_catalog(
    config: dict,
    content_root: Path = CONTENT_ROOT,
) -> tuple[dict[str, list[dict]], dict[str, tuple[str, dict]]]:
    grouped = content_entries_by_skill(content_root, config)
    by_slug: dict[str, tuple[str, dict]] = {}

    for skill_key, entries in grouped.items():
        skill = config["skills"][skill_key]
        section_rank = {
            section: index for index, section in enumerate(skill["section_order"])
        }
        entries.sort(
            key=lambda entry: (
                section_rank.get(entry.get("section"), len(section_rank)),
                entry.get("order", 10**9),
                entry.get("slug", ""),
            )
        )
        for entry in entries:
            slug = entry["slug"]
            if slug in by_slug:
                raise ValueError(f"duplicate canonical slug: {slug}")
            routing_aliases: list[str] = []
            seen_aliases: set[str] = set()
            for alias in [*entry["aliases"], *entry["commands"]]:
                normalized_alias = alias.casefold()
                if normalized_alias not in seen_aliases:
                    seen_aliases.add(normalized_alias)
                    routing_aliases.append(alias)
            entry["routing_aliases"] = routing_aliases
            entry.setdefault("preflight_commands", [])
            entry.setdefault("install_commands", [])
            entry.setdefault("smoke_test", None)
            entry["route_path"] = canonical_route(skill, slug)
            entry["skill_name"] = skill["name"]
            entry["provenance_index_path"] = "../PROVENANCE.md"
            by_slug[slug] = (skill_key, entry)

    legacy_targets = {
        Path(alias["from_route"]).stem: alias["to_slug"]
        for alias in config.get("route_aliases", [])
    }
    for _, entries in grouped.items():
        for entry in entries:
            related_routes: list[dict] = []
            for related_slug in entry.get("related_refs", []):
                canonical_slug = legacy_targets.get(related_slug, related_slug)
                target = by_slug.get(canonical_slug)
                if target is None:
                    raise ValueError(
                        f"{entry['slug']}: unresolved related reference {related_slug}"
                    )
                target_skill_key, target_entry = target
                target_skill = config["skills"][target_skill_key]
                cross_skill = target_skill_key != entry["skill"]
                related_routes.append(
                    {
                        "slug": canonical_slug,
                        "skill_name": target_skill["name"],
                        "route_path": target_entry["route_path"],
                        "qualified_path": (
                            f"{target_skill['name']}/{target_entry['route_path']}"
                            if cross_skill
                            else target_entry["route_path"]
                        ),
                        "cross_skill": cross_skill,
                        "instruction": (
                            f" — cross-skill: load the `{target_skill['name']}` "
                            "skill first."
                            if cross_skill
                            else ""
                        ),
                    }
                )
            entry["related_routes"] = related_routes

    return grouped, by_slug


def prepared_route_aliases(
    config: dict,
    by_slug: dict[str, tuple[str, dict]],
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {
        skill_key: [] for skill_key in config.get("skills", {})
    }
    for raw_alias in config.get("route_aliases", []):
        alias = dict(raw_alias)
        target_skill_key, target_entry = by_slug[alias["to_slug"]]
        if target_skill_key != alias["to_skill"]:
            raise ValueError(
                f"alias {alias['from_route']}: target skill does not match {alias['to_slug']}"
            )
        target_skill = config["skills"][target_skill_key]
        alias["target_skill_name"] = target_skill["name"]
        alias["target_route"] = target_entry["route_path"]
        alias["target_slug"] = target_entry["slug"]
        grouped[alias["from_skill"]].append(alias)
    for aliases in grouped.values():
        aliases.sort(key=lambda item: item["from_route"])
    return grouped


def embedded_lock_index() -> list[dict]:
    index: list[dict] = []
    specifications = (
        ("upstream.yaml", "Upstream repository"),
        ("stata-help.yaml", "Local Stata help"),
        ("packages.yaml", "Community package distributions"),
        ("plugin-sdk.yaml", "Plugin SDK"),
    )
    for filename, label in specifications:
        path = LOCK_ROOT / filename
        if not path.is_file():
            continue
        data = read_yaml(path)
        if filename == "upstream.yaml":
            identity = f"commit `{data.get('repository', {}).get('commit', 'unknown')}`"
        elif filename == "stata-help.yaml":
            release = data.get("stata_release", {})
            identity = (
                f"{release.get('edition', 'unknown')} "
                f"{release.get('bundle_version', 'unknown')}"
            )
        elif filename == "packages.yaml":
            identity = f"{len(data.get('packages', {}))} locked package workflows"
        else:
            identity = f"{len(data.get('sources', []))} locked SDK sources"
        index.append(
            {
                "label": label,
                "identity": identity,
                "sha256": sha256_file(path),
            }
        )
    return index


def _render_tree(
    output_root: Path,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> None:
    env = build_environment()
    lock_index = embedded_lock_index()

    reference_template = env.get_template("reference.md.j2")
    skill_template = env.get_template("skill.md.j2")
    alias_template = env.get_template("alias.md.j2")
    provenance_template = env.get_template("provenance.md.j2")
    openai_template = env.get_template("openai.yaml.j2")

    for skill_key, skill in config["skills"].items():
        folder = _safe_render_path(output_root, skill["folder"])
        route_dir = _safe_render_path(folder, skill["route_dir"])
        route_dir.mkdir(parents=True, exist_ok=True)

        entries = grouped[skill_key]
        sections: OrderedDict[str, list[dict]] = OrderedDict(
            (section, []) for section in skill["section_order"]
        )
        for entry in entries:
            sections[entry["section"]].append(entry)
            write_text(
                _safe_render_path(route_dir, f"{entry['slug']}.md"),
                normalized_markdown(reference_template.render(entry=entry)),
            )

        route_aliases = aliases_by_skill[skill_key]
        for alias in route_aliases:
            alias_path = _safe_render_path(folder, alias["from_route"])
            write_text(
                alias_path,
                normalized_markdown(alias_template.render(alias=alias)),
            )

        section_payload = [
            {"name": name, "entries": section_entries}
            for name, section_entries in sections.items()
            if section_entries
        ]
        write_text(
            _safe_render_path(folder, "SKILL.md"),
            normalized_markdown(
                skill_template.render(
                    skill=skill,
                    sections=section_payload,
                    route_aliases=route_aliases,
                )
            ),
        )
        write_text(
            _safe_render_path(folder, "PROVENANCE.md"),
            normalized_markdown(
                provenance_template.render(
                    skill=skill,
                    entries=entries,
                    lock_index=lock_index,
                )
            ),
        )
        write_text(
            _safe_render_path(folder, "agents", "openai.yaml"),
            openai_template.render(interface=skill["interface"]).strip() + "\n",
        )


def _expected_rendered_files(
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> set[Path]:
    expected: list[Path] = []
    for skill_key, skill in config["skills"].items():
        folder = Path(skill["folder"])
        expected.extend(
            (
                folder / "SKILL.md",
                folder / "PROVENANCE.md",
                folder / "agents" / "openai.yaml",
            )
        )
        expected.extend(
            folder / skill["route_dir"] / f"{entry['slug']}.md"
            for entry in grouped[skill_key]
        )
        expected.extend(
            folder / alias["from_route"] for alias in aliases_by_skill[skill_key]
        )
    unique = set(expected)
    if len(unique) != len(expected):
        raise ValueError("render configuration maps multiple entries to one output path")
    return unique


def validate_rendered_tree(
    output_root: Path,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> None:
    """Reject incomplete, unexpected, empty, or malformed staged output."""

    expected = _expected_rendered_files(config, grouped, aliases_by_skill)
    actual = {
        path.relative_to(output_root)
        for path in output_root.rglob("*")
        if path.is_file()
    }
    errors: list[str] = []
    missing = sorted(str(path) for path in expected - actual)
    extra = sorted(str(path) for path in actual - expected)
    if missing:
        errors.append(f"missing files: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected files: {', '.join(extra)}")

    expected_directories: set[Path] = set()
    for relative in expected:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    actual_directories = {
        path.relative_to(output_root)
        for path in output_root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    missing_directories = sorted(
        str(path) for path in expected_directories - actual_directories
    )
    extra_directories = sorted(
        str(path) for path in actual_directories - expected_directories
    )
    if missing_directories:
        errors.append(f"missing directories: {', '.join(missing_directories)}")
    if extra_directories:
        errors.append(f"unexpected directories: {', '.join(extra_directories)}")

    unsafe_links = sorted(
        str(path.relative_to(output_root))
        for path in output_root.rglob("*")
        if path.is_symlink()
    )
    if unsafe_links:
        errors.append(f"symbolic links are not publishable: {', '.join(unsafe_links)}")

    empty = sorted(
        str(relative)
        for relative in expected & actual
        if not (output_root / relative).read_bytes()
    )
    if empty:
        errors.append(f"empty files: {', '.join(empty)}")

    for skill in config["skills"].values():
        metadata_path = (
            output_root / skill["folder"] / "agents" / "openai.yaml"
        )
        if metadata_path.relative_to(output_root) not in actual:
            continue
        metadata = read_yaml(metadata_path)
        if metadata.get("interface") != skill["interface"]:
            errors.append(
                f"{metadata_path.relative_to(output_root)}: interface metadata "
                "does not match configuration"
            )

    if errors:
        raise ValueError("staged render validation failed: " + "; ".join(errors))


def _atomic_rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a directory only when the destination is absent."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        try:
            rename_exclusive = libc.renamex_np
        except AttributeError as error:
            raise RenderTransactionError(
                "This macOS runtime lacks atomic no-replace rename support"
            ) from error
        rename_exclusive.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            source_bytes,
            destination_bytes,
            0x00000004,  # RENAME_EXCL from <sys/stdio.h>
        )
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as error:
            raise RenderTransactionError(
                "This Linux runtime lacks atomic no-replace rename support"
            ) from error
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            -100,  # AT_FDCWD
            source_bytes,
            -100,
            destination_bytes,
            1,  # RENAME_NOREPLACE from <linux/fs.h>
        )
    else:
        raise RenderTransactionError(
            "Atomic render replacement is supported only on macOS and Linux"
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> tuple[int, os.stat_result]:
    metadata = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(metadata.st_mode):
        raise RenderTransactionError(
            f"refusing non-directory render backup entry: {display_path}"
        )
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != metadata.st_dev
        or opened.st_ino != metadata.st_ino
    ):
        os.close(descriptor)
        raise RenderTransactionError(
            f"render backup changed while opening it: {display_path}"
        )
    return descriptor, opened


def _clear_directory_descriptor(
    descriptor: int,
    display_path: Path,
) -> None:
    current_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
    os.fchmod(
        descriptor,
        current_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )
    for name in sorted(os.listdir(descriptor)):
        child_path = display_path / name
        metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(metadata.st_mode):
            child_descriptor, opened = _open_directory_at(
                descriptor,
                name,
                child_path,
            )
            try:
                _clear_directory_descriptor(child_descriptor, child_path)
            finally:
                os.close(child_descriptor)
            observed = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_dev != opened.st_dev
                or observed.st_ino != opened.st_ino
            ):
                raise RenderTransactionError(
                    "render backup directory changed before removal: "
                    f"{child_path}"
                )
            os.rmdir(name, dir_fd=descriptor)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RenderTransactionError(
                f"refusing unexpected render backup entry: {child_path}"
            )
        os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)


def _remove_verified_backup(
    backup_root: Path,
    expected: RenderOutputState,
) -> None:
    """Delete only the exact prior tree through anchored descriptors."""

    _verify_moved_output_state(backup_root, expected)
    if expected.device is None or expected.inode is None:
        raise RenderTransactionError(
            f"render backup has no accepted identity: {backup_root}"
        )
    _remove_owned_directory(
        backup_root,
        expected.device,
        expected.inode,
    )


def _remove_owned_directory(
    directory: Path,
    expected_device: int,
    expected_inode: int,
) -> None:
    """Remove only the directory with the exact captured identity."""

    parent_descriptor = os.open(
        directory.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    backup_descriptor: int | None = None
    try:
        backup_descriptor, opened = _open_directory_at(
            parent_descriptor,
            directory.name,
            directory,
        )
        if (
            opened.st_dev != expected_device
            or opened.st_ino != expected_inode
        ):
            raise RenderTransactionError(
                "directory no longer matches its captured identity; "
                f"preserving {directory}"
            )
        _clear_directory_descriptor(backup_descriptor, directory)
        os.close(backup_descriptor)
        backup_descriptor = None
        observed = os.stat(
            directory.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_dev != opened.st_dev
            or observed.st_ino != opened.st_ino
        ):
            raise RenderTransactionError(
                "directory path changed before final removal; preserving "
                f"the observed path {directory}"
            )
        os.rmdir(directory.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if backup_descriptor is not None:
            os.close(backup_descriptor)
        os.close(parent_descriptor)


def _cleanup_staged_root(
    staged_root: Path,
    expected_device: int,
    expected_inode: int,
) -> None:
    try:
        _remove_owned_directory(
            staged_root,
            expected_device,
            expected_inode,
        )
    except FileNotFoundError:
        return
    except (OSError, RenderTransactionError) as cleanup_error:
        print(
            "WARNING: staged render cleanup was skipped because the path no "
            f"longer matched the created directory: {staged_root}: "
            f"{cleanup_error}"
        )


def _replace_rendered_tree(
    staged_root: Path,
    output_root: Path,
    expected_output_state: RenderOutputState,
) -> None:
    """Replace output_root and restore its prior state if the swap fails."""

    verify_output_root_state(output_root, expected_output_state)
    backup_root: Path | None = None
    if output_root.exists() or output_root.is_symlink():
        backup_root = output_root.parent / (
            f".{output_root.name}.backup-{uuid.uuid4().hex}"
        )
        try:
            _atomic_rename_no_replace(output_root, backup_root)
        except BaseException as move_error:
            if backup_root.exists() or backup_root.is_symlink():
                raise RenderTransactionError(
                    "The prior render-root move reported a failure after "
                    f"creating {backup_root}; preserving it for review"
                ) from move_error
            raise
        _verify_moved_output_state(backup_root, expected_output_state)

    try:
        _atomic_rename_no_replace(staged_root, output_root)
    except BaseException as swap_error:
        if backup_root is not None and backup_root.exists():
            try:
                _verify_moved_output_state(
                    backup_root,
                    expected_output_state,
                )
                if output_root.exists() or output_root.is_symlink():
                    raise RenderTransactionError(
                        "Rendered-tree swap failed and the output path gained "
                        "concurrent state. The accepted prior tree remains at "
                        f"{backup_root}"
                    )
                _atomic_rename_no_replace(backup_root, output_root)
                verify_output_root_state(
                    output_root,
                    expected_output_state,
                )
            except BaseException as restore_error:
                raise RenderTransactionError(
                    "Rendered-tree swap failed and the prior tree could not be "
                    f"restored. The prior tree remains at {backup_root}: "
                    f"{restore_error}"
                ) from swap_error
        raise
    else:
        if backup_root is not None:
            try:
                _remove_verified_backup(
                    backup_root,
                    expected_output_state,
                )
            except (OSError, RenderTransactionError) as cleanup_error:
                print(
                    "WARNING: rendered tree was committed, but the prior-tree "
                    f"backup could not be removed: {backup_root}: {cleanup_error}"
                )


def render_all(
    output_root: Path = BUILD_ROOT,
    content_root: Path = CONTENT_ROOT,
    config_path: Path | None = None,
) -> None:
    """Render, validate, and transactionally replace a complete skill tree."""

    output_root = _resolved_output_root(output_root)
    config = load_skill_config(config_path) if config_path else load_skill_config()
    validate_required_skills(config)
    validate_render_inputs(config, content_root)
    grouped, by_slug = prepare_catalog(config, content_root)
    aliases_by_skill = prepared_route_aliases(config, by_slug)
    expected_output_state = capture_output_root_state(output_root)

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staged_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root.name}.stage-",
            dir=output_root.parent,
        )
    )
    staged_metadata = staged_root.stat()
    try:
        _render_tree(staged_root, config, grouped, aliases_by_skill)
        validate_rendered_tree(
            staged_root,
            config,
            grouped,
            aliases_by_skill,
        )
        _replace_rendered_tree(
            staged_root,
            output_root,
            expected_output_state,
        )
    finally:
        _cleanup_staged_root(
            staged_root,
            staged_metadata.st_dev,
            staged_metadata.st_ino,
        )

    for skill in config["skills"].values():
        print(f"Rendered {output_root / skill['folder']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=BUILD_ROOT,
        help=(
            "Render into an absent, empty, or dedicated generated-tree "
            "directory instead of build/generated."
        ),
    )
    args = parser.parse_args(argv)
    render_all(output_root=args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
