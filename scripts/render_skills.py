#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import argparse
import ctypes
import hashlib
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
class RenderTreeEntry:
    """Identity of one accepted entry beneath a rendered-tree root."""

    relative_path: str
    kind: str
    device: int
    inode: int
    content_sha256: str | None = None


@dataclass(frozen=True)
class RenderOutputState:
    exists: bool
    device: int | None = None
    inode: int | None = None
    tree_sha256: str | None = None
    entries: tuple[RenderTreeEntry, ...] = ()


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


def _existing_ancestor_identities(path: Path) -> tuple[tuple[int, int], ...]:
    """Return filesystem identities from the deepest existing ancestor upward."""

    current = path
    while True:
        try:
            current.stat()
            break
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise
            current = parent
        except NotADirectoryError as error:
            raise ValueError(
                f"render output path has a non-directory ancestor: {path}"
            ) from error

    identities: list[tuple[int, int]] = []
    while True:
        metadata = current.stat()
        identities.append((metadata.st_dev, metadata.st_ino))
        parent = current.parent
        if parent == current:
            return tuple(identities)
        current = parent


def _validated_canonical_build_root(requested: Path) -> Path:
    """Anchor the build/generated exception beneath the real repository root."""

    repository_root = Path(os.path.abspath(REPO_ROOT.expanduser()))
    expected_build_root = repository_root / "build" / "generated"
    resolved_build_root = expected_build_root.resolve(strict=False)
    configured_build_root = Path(os.path.abspath(BUILD_ROOT.expanduser()))
    requested_root = Path(os.path.abspath(requested))
    if configured_build_root != expected_build_root:
        raise ValueError(
            "canonical render output must be configured as "
            "REPO_ROOT/build/generated"
        )
    if requested_root != expected_build_root:
        raise ValueError(
            "canonical build output must use the repository-anchored "
            f"path {expected_build_root}"
        )

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        repository_metadata = repository_root.lstat()
        if not stat.S_ISDIR(repository_metadata.st_mode):
            raise ValueError(
                "repository root is not an ordinary directory: "
                f"{repository_root}"
            )
        repository_descriptor = os.open(repository_root, directory_flags)
        descriptors.append(repository_descriptor)
        opened_repository = os.fstat(repository_descriptor)
        if (
            opened_repository.st_dev != repository_metadata.st_dev
            or opened_repository.st_ino != repository_metadata.st_ino
        ):
            raise ValueError(
                "repository root changed while validating canonical build output"
            )

        parent_descriptor = repository_descriptor
        current_path = repository_root
        for component in ("build", "generated"):
            current_path = current_path / component
            try:
                component_metadata = os.stat(
                    component,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                break
            if not stat.S_ISDIR(component_metadata.st_mode):
                raise ValueError(
                    "refusing canonical build output with a symbolic-link or "
                    f"non-directory component: {current_path}"
                )
            component_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            descriptors.append(component_descriptor)
            opened_component = os.fstat(component_descriptor)
            if (
                opened_component.st_dev != component_metadata.st_dev
                or opened_component.st_ino != component_metadata.st_ino
            ):
                raise ValueError(
                    "canonical build output changed while validating "
                    f"{current_path}"
                )
            parent_descriptor = component_descriptor
    except OSError as error:
        raise ValueError(
            "could not validate canonical build output without following "
            f"symbolic links: {expected_build_root}"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return resolved_build_root


def _resolved_output_root(output_root: Path) -> Path:
    """Canonicalize a render target while rejecting ambiguous destinations."""

    requested = Path(output_root).expanduser()
    if requested.is_symlink():
        raise ValueError(f"refusing symlinked render output root: {requested}")
    resolved = requested.resolve(strict=False)
    if resolved.parent == resolved:
        raise ValueError("refusing to render over a filesystem root")
    repository_root = REPO_ROOT.resolve(strict=True)
    canonical_build_root = BUILD_ROOT.resolve(strict=False)
    if resolved == canonical_build_root:
        return _validated_canonical_build_root(requested)
    repository_metadata = repository_root.stat()
    repository_identity = (
        repository_metadata.st_dev,
        repository_metadata.st_ino,
    )
    if repository_identity in _existing_ancestor_identities(resolved):
        raise ValueError(
            "in-repository render output must be build/generated; "
            f"refusing {resolved}"
        )
    return resolved


def preflight_existing_output_root(output_root: Path) -> None:
    """Require an existing target to be empty or recognizably generated."""

    if not output_root.exists():
        return
    output_metadata = output_root.lstat()
    if not stat.S_ISDIR(output_metadata.st_mode):
        raise ValueError(f"render output root is not a directory: {output_root}")
    if not any(output_root.iterdir()):
        return

    errors: list[str] = []
    for folder in SKILL_FOLDERS:
        skill_root = output_root / folder
        try:
            skill_metadata = skill_root.lstat()
        except FileNotFoundError:
            errors.append(f"{folder}: missing top-level skill root")
            continue
        if not stat.S_ISDIR(skill_metadata.st_mode):
            errors.append(
                f"{folder}: top-level skill root must be an ordinary directory"
            )
            continue
    if errors:
        raise ValueError(
            "refusing to replace a non-dedicated render output root: "
            + "; ".join(errors)
        )

    errors = validate_complete_skill_tree(output_root)
    for folder in SKILL_FOLDERS:
        skill_root = output_root / folder
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


def _sha256_regular_file_no_follow(
    name: str | Path,
    *,
    dir_fd: int | None = None,
    expected_metadata: os.stat_result | None = None,
) -> str:
    """Hash one regular file through a no-follow descriptor."""

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=dir_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RenderTransactionError(
                f"refusing to hash a non-regular render entry: {name}"
            )
        if expected_metadata is not None and (
            before.st_dev != expected_metadata.st_dev
            or before.st_ino != expected_metadata.st_ino
            or before.st_size != expected_metadata.st_size
            or before.st_mtime_ns != expected_metadata.st_mtime_ns
            or before.st_ctime_ns != expected_metadata.st_ctime_ns
        ):
            raise RenderTransactionError(
                f"render entry changed before content hashing: {name}"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise RenderTransactionError(
                f"render entry changed during content hashing: {name}"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _capture_tree_entries(root: Path) -> tuple[RenderTreeEntry, ...]:
    """Capture path and inode identity without following tree symlinks."""

    entries: list[RenderTreeEntry] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
        else:
            kind = "other"
        content_sha256 = (
            _sha256_regular_file_no_follow(
                path,
                expected_metadata=metadata,
            )
            if kind == "file"
            else None
        )
        entries.append(
            RenderTreeEntry(
                relative_path=path.relative_to(root).as_posix(),
                kind=kind,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                content_sha256=content_sha256,
            )
        )
    return tuple(entries)


def capture_output_root_state(output_root: Path) -> RenderOutputState:
    """Fingerprint an accepted target so staging cannot hide a later change."""

    if output_root.is_symlink():
        raise ValueError(f"refusing symlinked render output root: {output_root}")
    if not output_root.exists():
        return RenderOutputState(exists=False)

    preflight_existing_output_root(output_root)
    before = output_root.lstat()
    before_entries = _capture_tree_entries(output_root)
    digest = tree_digest(output_root)
    preflight_existing_output_root(output_root)
    after_entries = _capture_tree_entries(output_root)
    after = output_root.lstat()
    before_identity = (before.st_dev, before.st_ino)
    after_identity = (after.st_dev, after.st_ino)
    if before_identity != after_identity or before_entries != after_entries:
        raise RenderTransactionError(
            "render output root changed while its ownership was being checked"
        )
    return RenderOutputState(
        exists=True,
        device=after.st_dev,
        inode=after.st_ino,
        tree_sha256=digest,
        entries=after_entries,
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


def _atomic_rename_at_no_replace(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically move one descriptor-relative entry without replacement."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    if sys.platform == "darwin":
        try:
            rename_exclusive = libc.renameatx_np
        except AttributeError as error:
            raise RenderTransactionError(
                "This macOS runtime lacks descriptor-relative atomic "
                "no-replace rename support"
            ) from error
    elif sys.platform.startswith("linux"):
        try:
            rename_exclusive = libc.renameat2
        except AttributeError as error:
            raise RenderTransactionError(
                "This Linux runtime lacks descriptor-relative atomic "
                "no-replace rename support"
            ) from error
    else:
        raise RenderTransactionError(
            "Atomic render cleanup is supported only on macOS and Linux"
        )
    rename_exclusive.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exclusive.restype = ctypes.c_int
    result = rename_exclusive(
        source_descriptor,
        source_bytes,
        destination_descriptor,
        destination_bytes,
        0x00000004 if sys.platform == "darwin" else 1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination_name,
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
    try:
        opened = os.fstat(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
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


def _entry_kind(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _entry_matches(
    metadata: os.stat_result,
    expected: RenderTreeEntry,
) -> bool:
    return (
        _entry_kind(metadata) == expected.kind
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
    )


def _entry_metadata_at(
    descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _restore_quarantined_entry(
    quarantine_descriptor: int,
    directory_descriptor: int,
    name: str,
    moved_metadata: os.stat_result,
    display_path: Path,
) -> None:
    """Restore or preserve a mismatched entry without deleting its bytes."""

    try:
        _atomic_rename_at_no_replace(
            quarantine_descriptor,
            name,
            directory_descriptor,
            name,
        )
    except BaseException as restore_error:
        restored = _entry_metadata_at(directory_descriptor, name)
        quarantined = _entry_metadata_at(quarantine_descriptor, name)
        moved_identity = (moved_metadata.st_dev, moved_metadata.st_ino)
        if (
            restored is not None
            and (restored.st_dev, restored.st_ino) == moved_identity
            and quarantined is None
        ):
            return
        raise RenderTransactionError(
            "render cleanup moved a changed entry into private quarantine and "
            f"could not restore it at {display_path}; its bytes remain preserved"
        ) from restore_error


def _remove_verified_empty_directory_via_quarantine(
    parent_descriptor: int,
    name: str,
    display_path: Path,
    expected_metadata: os.stat_result,
) -> None:
    """Move an empty verified name into a private directory before removal."""

    def matches_expected(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == expected_metadata.st_dev
            and metadata.st_ino == expected_metadata.st_ino
        )

    cleanup_name = f".render-cleanup-{uuid.uuid4().hex}"
    cleanup_path = display_path.parent / cleanup_name
    os.mkdir(cleanup_name, mode=0o700, dir_fd=parent_descriptor)
    cleanup_descriptor: int | None = None
    cleanup_opened: os.stat_result | None = None
    try:
        cleanup_descriptor, cleanup_opened = _open_directory_at(
            parent_descriptor,
            cleanup_name,
            cleanup_path,
        )
        os.fchmod(cleanup_descriptor, 0o700)
        try:
            _atomic_rename_at_no_replace(
                parent_descriptor,
                name,
                cleanup_descriptor,
                name,
            )
        except BaseException as move_error:
            moved_after_error = _entry_metadata_at(cleanup_descriptor, name)
            source_after_error = _entry_metadata_at(parent_descriptor, name)
            if moved_after_error is None:
                raise RenderTransactionError(
                    "verified empty render directory could not be moved into "
                    f"private cleanup quarantine: {display_path}"
                ) from move_error
            if source_after_error is not None:
                raise RenderTransactionError(
                    "verified empty render directory moved into private cleanup "
                    "quarantine, but its public name gained concurrent state; "
                    f"both were preserved: {display_path}"
                ) from move_error
            if not matches_expected(moved_after_error):
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    name,
                    moved_after_error,
                    display_path,
                )
                raise RenderTransactionError(
                    "verified empty render directory changed while moving into "
                    f"private cleanup quarantine and was preserved: {display_path}"
                ) from move_error

        moved = os.stat(
            name,
            dir_fd=cleanup_descriptor,
            follow_symlinks=False,
        )
        source_after_move = _entry_metadata_at(parent_descriptor, name)
        if not matches_expected(moved):
            if source_after_move is None:
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    name,
                    moved,
                    display_path,
                )
            raise RenderTransactionError(
                "verified empty render directory changed while moving into "
                f"private cleanup quarantine and was preserved: {display_path}"
            )
        if source_after_move is not None:
            raise RenderTransactionError(
                "verified empty render directory public name gained concurrent "
                f"state; both entries were preserved: {display_path}"
            )

        try:
            moved_descriptor, opened = _open_directory_at(
                cleanup_descriptor,
                name,
                display_path,
            )
            try:
                if not matches_expected(opened) or os.listdir(moved_descriptor):
                    raise RenderTransactionError(
                        "verified empty render directory changed in private "
                        f"cleanup quarantine: {display_path}"
                    )
            finally:
                os.close(moved_descriptor)
        except BaseException as verification_error:
            current = _entry_metadata_at(cleanup_descriptor, name)
            if (
                current is not None
                and _entry_metadata_at(parent_descriptor, name) is None
            ):
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    name,
                    current,
                    display_path,
                )
            raise RenderTransactionError(
                "verified empty render directory changed in private cleanup "
                f"quarantine and was preserved: {display_path}"
            ) from verification_error

        observed = os.stat(
            name,
            dir_fd=cleanup_descriptor,
            follow_symlinks=False,
        )
        if not matches_expected(observed):
            if _entry_metadata_at(parent_descriptor, name) is None:
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    name,
                    observed,
                    display_path,
                )
            raise RenderTransactionError(
                "verified empty render directory changed before private removal "
                f"and was preserved: {display_path}"
            )
        try:
            os.rmdir(name, dir_fd=cleanup_descriptor)
        except BaseException as removal_error:
            current = _entry_metadata_at(cleanup_descriptor, name)
            if (
                current is not None
                and _entry_metadata_at(parent_descriptor, name) is None
            ):
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    name,
                    current,
                    display_path,
                )
            raise RenderTransactionError(
                "verified empty render directory could not be removed from "
                f"private cleanup quarantine and was preserved: {display_path}"
            ) from removal_error
        os.fsync(cleanup_descriptor)
    finally:
        if cleanup_descriptor is not None:
            os.close(cleanup_descriptor)

    if cleanup_opened is None:
        raise RenderTransactionError(
            f"render cleanup quarantine was not opened for {display_path}"
        )
    observed_cleanup = os.stat(
        cleanup_name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed_cleanup.st_mode)
        or observed_cleanup.st_dev != cleanup_opened.st_dev
        or observed_cleanup.st_ino != cleanup_opened.st_ino
    ):
        raise RenderTransactionError(
            f"private render cleanup quarantine changed for {display_path}"
        )
    os.rmdir(cleanup_name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _clear_directory_descriptor(
    descriptor: int,
    display_path: Path,
    expected_entries: dict[str, RenderTreeEntry],
    relative_parts: tuple[str, ...] = (),
) -> None:
    current_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
    os.fchmod(
        descriptor,
        current_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )
    observed_names = set(os.listdir(descriptor))
    expected_names = {
        Path(relative_path).name
        for relative_path in expected_entries
        if Path(relative_path).parts[:-1] == relative_parts
    }
    if observed_names != expected_names:
        unexpected = sorted(observed_names - expected_names)
        missing = sorted(expected_names - observed_names)
        details: list[str] = []
        if unexpected:
            details.append(f"unexpected entries: {', '.join(unexpected)}")
        if missing:
            details.append(f"missing entries: {', '.join(missing)}")
        raise RenderTransactionError(
            "render backup changed before cleanup at "
            f"{display_path}: {'; '.join(details)}"
        )

    quarantine_name = f".render-cleanup-{uuid.uuid4().hex}"
    os.mkdir(quarantine_name, mode=0o700, dir_fd=descriptor)
    quarantine_descriptor: int | None = None
    quarantine_opened: os.stat_result | None = None
    try:
        quarantine_descriptor, quarantine_opened = _open_directory_at(
            descriptor,
            quarantine_name,
            display_path / quarantine_name,
        )
        os.fchmod(quarantine_descriptor, 0o700)

        for name in sorted(expected_names):
            child_path = display_path / name
            child_parts = (*relative_parts, name)
            relative_path = Path(*child_parts).as_posix()
            metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            expected_entry = expected_entries.get(relative_path)
            if expected_entry is None:
                raise RenderTransactionError(
                    "render cleanup manifest is missing an entry for "
                    f"{child_path}"
                )
            if not _entry_matches(
                metadata,
                expected_entry,
            ):
                raise RenderTransactionError(
                    "render backup entry changed before quarantine: "
                    f"{child_path}"
                )

            try:
                _atomic_rename_at_no_replace(
                    descriptor,
                    name,
                    quarantine_descriptor,
                    name,
                )
            except BaseException as move_error:
                moved = _entry_metadata_at(quarantine_descriptor, name)
                source = _entry_metadata_at(descriptor, name)
                if moved is not None and not _entry_matches(
                    moved,
                    expected_entry,
                ):
                    _restore_quarantined_entry(
                        quarantine_descriptor,
                        descriptor,
                        name,
                        moved,
                        child_path,
                    )
                    raise RenderTransactionError(
                        "render backup entry changed while it was moved into "
                        f"private quarantine: {child_path}"
                    ) from move_error
                if moved is None:
                    raise RenderTransactionError(
                        "render backup entry could not be moved into private "
                        f"quarantine: {child_path}"
                    ) from move_error
                if source is not None:
                    raise RenderTransactionError(
                        "render backup entry moved into private quarantine, but "
                        f"its source path gained concurrent state: {child_path}"
                    ) from move_error

            moved = os.stat(
                name,
                dir_fd=quarantine_descriptor,
                follow_symlinks=False,
            )
            if not _entry_matches(
                moved,
                expected_entry,
            ):
                _restore_quarantined_entry(
                    quarantine_descriptor,
                    descriptor,
                    name,
                    moved,
                    child_path,
                )
                raise RenderTransactionError(
                    "render backup entry changed while it was moved into "
                    "private quarantine: "
                    f"{child_path}"
                )

            if stat.S_ISDIR(moved.st_mode):
                child_descriptor, opened = _open_directory_at(
                    quarantine_descriptor,
                    name,
                    child_path,
                )
                try:
                    _clear_directory_descriptor(
                        child_descriptor,
                        child_path,
                        expected_entries,
                        child_parts,
                    )
                finally:
                    os.close(child_descriptor)
                _remove_verified_empty_directory_via_quarantine(
                    quarantine_descriptor,
                    name,
                    child_path,
                    opened,
                )
                continue
            if not stat.S_ISREG(moved.st_mode):
                raise RenderTransactionError(
                    f"refusing unexpected render backup entry: {child_path}"
                )
            try:
                moved_digest = _sha256_regular_file_no_follow(
                    name,
                    dir_fd=quarantine_descriptor,
                    expected_metadata=moved,
                )
            except BaseException as hash_error:
                _restore_quarantined_entry(
                    quarantine_descriptor,
                    descriptor,
                    name,
                    moved,
                    child_path,
                )
                raise RenderTransactionError(
                    "render backup file changed while its quarantined bytes "
                    f"were being verified: {child_path}"
                ) from hash_error
            if (
                expected_entry.content_sha256 is None
                or moved_digest != expected_entry.content_sha256
            ):
                _restore_quarantined_entry(
                    quarantine_descriptor,
                    descriptor,
                    name,
                    moved,
                    child_path,
                )
                raise RenderTransactionError(
                    "render backup file contents changed before deletion and "
                    f"were preserved: {child_path}"
                )
            try:
                final_metadata = os.stat(
                    name,
                    dir_fd=quarantine_descriptor,
                    follow_symlinks=False,
                )
                final_digest = _sha256_regular_file_no_follow(
                    name,
                    dir_fd=quarantine_descriptor,
                    expected_metadata=final_metadata,
                )
                final_observed = os.stat(
                    name,
                    dir_fd=quarantine_descriptor,
                    follow_symlinks=False,
                )
            except BaseException as hash_error:
                current_metadata = (
                    _entry_metadata_at(quarantine_descriptor, name)
                    or moved
                )
                _restore_quarantined_entry(
                    quarantine_descriptor,
                    descriptor,
                    name,
                    current_metadata,
                    child_path,
                )
                raise RenderTransactionError(
                    "render backup file changed during its final pre-delete "
                    f"verification and was preserved: {child_path}"
                ) from hash_error
            final_version = (
                final_metadata.st_dev,
                final_metadata.st_ino,
                final_metadata.st_size,
                final_metadata.st_mtime_ns,
                final_metadata.st_ctime_ns,
            )
            observed_version = (
                final_observed.st_dev,
                final_observed.st_ino,
                final_observed.st_size,
                final_observed.st_mtime_ns,
                final_observed.st_ctime_ns,
            )
            if (
                not _entry_matches(final_metadata, expected_entry)
                or not _entry_matches(final_observed, expected_entry)
                or final_version != observed_version
                or expected_entry.content_sha256 is None
                or final_digest != expected_entry.content_sha256
            ):
                _restore_quarantined_entry(
                    quarantine_descriptor,
                    descriptor,
                    name,
                    final_observed,
                    child_path,
                )
                raise RenderTransactionError(
                    "render backup file changed immediately before deletion "
                    f"and was preserved: {child_path}"
                )
            os.unlink(name, dir_fd=quarantine_descriptor)

        os.fsync(quarantine_descriptor)
    finally:
        if quarantine_descriptor is not None:
            os.close(quarantine_descriptor)

    if quarantine_opened is None:
        raise RenderTransactionError(
            f"render cleanup quarantine was not opened at {display_path}"
        )
    _remove_verified_empty_directory_via_quarantine(
        descriptor,
        quarantine_name,
        display_path / quarantine_name,
        quarantine_opened,
    )
    remaining_names = sorted(os.listdir(descriptor))
    if remaining_names:
        raise RenderTransactionError(
            "render backup gained entries during cleanup at "
            f"{display_path}: {', '.join(remaining_names)}"
        )
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
        expected.entries,
    )


def _remove_owned_directory(
    directory: Path,
    expected_device: int,
    expected_inode: int,
    expected_entries: tuple[RenderTreeEntry, ...],
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
        entries_by_path = {
            entry.relative_path: entry
            for entry in expected_entries
        }
        _clear_directory_descriptor(
            backup_descriptor,
            directory,
            entries_by_path,
        )
        descriptor_to_close = backup_descriptor
        backup_descriptor = None
        os.close(descriptor_to_close)
        _remove_verified_empty_directory_via_quarantine(
            parent_descriptor,
            directory.name,
            directory,
            opened,
        )
        if _entry_metadata_at(parent_descriptor, directory.name) is not None:
            raise RenderTransactionError(
                "render backup public name reappeared during private cleanup; "
                f"preserving concurrent state at {directory}"
            )
    finally:
        try:
            if backup_descriptor is not None:
                os.close(backup_descriptor)
        finally:
            os.close(parent_descriptor)


def _cleanup_staged_root(
    staged_root: Path,
    expected_device: int,
    expected_inode: int,
    expected_entries: tuple[RenderTreeEntry, ...],
) -> None:
    try:
        _remove_owned_directory(
            staged_root,
            expected_device,
            expected_inode,
            expected_entries,
        )
    except FileNotFoundError:
        return
    except (OSError, RenderTransactionError) as cleanup_error:
        print(
            "WARNING: staged render cleanup was skipped because the path no "
            f"longer matched the created directory: {staged_root}: "
            f"{cleanup_error}"
        )


def _path_identity(path: Path) -> tuple[int, int] | None:
    """Return a directory-entry identity without following symbolic links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _state_root_identity(expected: RenderOutputState) -> tuple[int, int] | None:
    if (
        not expected.exists
        or expected.device is None
        or expected.inode is None
    ):
        return None
    return expected.device, expected.inode


def _preserve_installed_entry(
    output_root: Path,
    installed_identity: tuple[int, int],
) -> Path:
    """Move a rejected installed entry to a unique recovery path."""

    if _path_identity(output_root) != installed_identity:
        raise RenderTransactionError(
            "the rejected installed entry changed before it could be moved "
            f"from {output_root}"
        )
    recovery_root = output_root.parent / (
        f".{output_root.name}.recovery-{uuid.uuid4().hex}"
    )
    try:
        _atomic_rename_no_replace(output_root, recovery_root)
    except BaseException as move_error:
        if (
            _path_identity(recovery_root) == installed_identity
            and _path_identity(output_root) is None
        ):
            return recovery_root
        raise RenderTransactionError(
            "the rejected installed entry could not be moved from "
            f"{output_root}"
        ) from move_error
    if (
        _path_identity(recovery_root) != installed_identity
        or _path_identity(output_root) is not None
    ):
        raise RenderTransactionError(
            "the rejected installed entry changed while it was being "
            f"preserved at {recovery_root}"
        )
    return recovery_root


def _restore_prior_tree(
    backup_root: Path,
    output_root: Path,
    expected_output_state: RenderOutputState,
) -> None:
    """Restore the exact accepted prior tree without replacing new state."""

    _verify_moved_output_state(backup_root, expected_output_state)
    if _path_identity(output_root) is not None:
        raise RenderTransactionError(
            "the output path gained concurrent state before rollback"
        )
    try:
        _atomic_rename_no_replace(backup_root, output_root)
    except BaseException:
        if _state_root_identity(expected_output_state) != _path_identity(
            output_root
        ):
            raise
    verify_output_root_state(output_root, expected_output_state)


def _replace_rendered_tree(
    staged_root: Path,
    output_root: Path,
    expected_output_state: RenderOutputState,
    expected_staged_state: RenderOutputState,
    validate_staged_tree: Callable[[Path], None],
) -> None:
    """Replace output_root and restore its prior state if the swap fails."""

    verify_output_root_state(output_root, expected_output_state)
    verify_output_root_state(staged_root, expected_staged_state)
    validate_staged_tree(staged_root)
    verify_output_root_state(staged_root, expected_staged_state)

    backup_root: Path | None = None
    installed_identity: tuple[int, int] | None = None
    install_reported_success = False
    if expected_output_state.exists:
        backup_root = output_root.parent / (
            f".{output_root.name}.backup-{uuid.uuid4().hex}"
        )

    try:
        if backup_root is not None:
            _atomic_rename_no_replace(output_root, backup_root)
            _verify_moved_output_state(backup_root, expected_output_state)

        verify_output_root_state(staged_root, expected_staged_state)
        validate_staged_tree(staged_root)
        verify_output_root_state(staged_root, expected_staged_state)
        _atomic_rename_no_replace(staged_root, output_root)
        install_reported_success = True
        installed_identity = _path_identity(output_root)
        if installed_identity is None:
            raise RenderTransactionError(
                "the staged tree disappeared immediately after placement"
            )
        verify_output_root_state(output_root, expected_staged_state)
        validate_staged_tree(output_root)
        verify_output_root_state(output_root, expected_staged_state)
    except BaseException as transaction_error:
        recovery_root: Path | None = None
        recovery_identity = installed_identity
        if recovery_identity is None and (
            _path_identity(output_root)
            == _state_root_identity(expected_staged_state)
        ):
            recovery_identity = _state_root_identity(expected_staged_state)
        if recovery_identity is not None:
            try:
                recovery_root = _preserve_installed_entry(
                    output_root,
                    recovery_identity,
                )
            except BaseException as preserve_error:
                backup_message = (
                    f" The accepted prior tree remains at {backup_root}."
                    if backup_root is not None
                    and _path_identity(backup_root) is not None
                    else ""
                )
                raise RenderTransactionError(
                    "Rendered-tree replacement failed after placing an entry "
                    "at the output path, and that entry could not be safely "
                    f"preserved.{backup_message} {preserve_error}"
                ) from transaction_error
        elif _path_identity(output_root) is not None:
            if (
                not install_reported_success
                and _path_identity(output_root)
                == _state_root_identity(expected_output_state)
            ):
                raise
            backup_message = (
                f" The accepted prior tree remains at {backup_root}."
                if backup_root is not None
                and _path_identity(backup_root) is not None
                else ""
            )
            raise RenderTransactionError(
                "Rendered-tree replacement failed and the output path gained "
                f"concurrent state; that state was preserved.{backup_message}"
            ) from transaction_error

        if backup_root is not None and _path_identity(backup_root) is not None:
            try:
                _restore_prior_tree(
                    backup_root,
                    output_root,
                    expected_output_state,
                )
            except BaseException as restore_error:
                raise RenderTransactionError(
                    "Rendered-tree replacement failed and the prior tree could "
                    "not be restored; the prior tree remains at "
                    f"{backup_root}: "
                    f"{restore_error}"
                ) from transaction_error

        if recovery_root is not None:
            prior_status = (
                "the accepted prior tree was restored"
                if expected_output_state.exists
                else "the previously absent output path was restored"
            )
            raise RenderTransactionError(
                "The placed staged tree failed identity or content validation; "
                f"{prior_status}, and the rejected tree remains at "
                f"{recovery_root}"
            ) from transaction_error
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
    expected_staged_state: RenderOutputState | None = None
    try:
        _render_tree(staged_root, config, grouped, aliases_by_skill)
        validate_rendered_tree(
            staged_root,
            config,
            grouped,
            aliases_by_skill,
        )
        expected_staged_state = capture_output_root_state(staged_root)

        def validate_current_staged_tree(candidate: Path) -> None:
            validate_rendered_tree(
                candidate,
                config,
                grouped,
                aliases_by_skill,
            )

        _replace_rendered_tree(
            staged_root,
            output_root,
            expected_output_state,
            expected_staged_state,
            validate_current_staged_tree,
        )
    finally:
        if expected_staged_state is None:
            if _path_identity(staged_root) == (
                staged_metadata.st_dev,
                staged_metadata.st_ino,
            ):
                print(
                    "WARNING: staged render cleanup was skipped because no "
                    f"trusted entry manifest was captured: {staged_root}"
                )
        else:
            _cleanup_staged_root(
                staged_root,
                staged_metadata.st_dev,
                staged_metadata.st_ino,
                expected_staged_state.entries,
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
