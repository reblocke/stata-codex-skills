#!/usr/bin/env python3
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import argparse
import ctypes
import fcntl
import hashlib
import os
import re
import stat
import sys
import uuid
from typing import Iterator

from jinja2 import DictLoader, Environment, StrictUndefined
import yaml

from libskillpack import (
    BUILD_ROOT,
    CONTENT_ROOT,
    LOCK_ROOT,
    REPO_ROOT,
    SKILL_CONFIG_PATH,
    TEMPLATES_ROOT,
    atomic_rename_at_no_replace as _shared_atomic_rename_at_no_replace,
    read_yaml,
)
from release_state import (
    CANONICAL_DIRECTORY_MODE,
    CANONICAL_FILE_MODE,
    SKILL_FOLDERS,
    tracked_source_paths,
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
    mode: int
    size: int
    content_sha256: str | None = None
    captured_data: bytes | None = None


@dataclass(frozen=True)
class RenderOutputState:
    exists: bool
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    tree_sha256: str | None = None
    entries: tuple[RenderTreeEntry, ...] = ()


@dataclass(frozen=True)
class RenderParentHandle:
    """Retained identity and descriptor for a render destination parent."""

    path: Path
    device: int
    inode: int
    descriptor: int


@dataclass(frozen=True)
class RenderParentAnchor:
    """Deepest existing destination ancestor retained before input reads."""

    parent_path: Path
    ancestor_path: Path
    device: int
    inode: int
    descriptor: int
    missing_components: tuple[str, ...]


@dataclass(frozen=True)
class RenderInputFile:
    """Stable bytes and identity for one renderer source file."""

    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class RenderContentEntry:
    """One parsed content entry bound to its captured source file."""

    skill_key: str
    path: Path
    entry: object
    source: RenderInputFile


@dataclass(frozen=True)
class RenderInputSnapshot:
    """All source bytes consulted by one render operation."""

    config: dict
    config_file: RenderInputFile
    content_root: Path
    content_entries: tuple[RenderContentEntry, ...]
    template_files: tuple[tuple[str, RenderInputFile], ...]
    lock_files: tuple[tuple[str, RenderInputFile | None], ...]


REQUIRED_SKILL_KEYS = ("core", "packages", "plugins")
REQUIRED_SKILLS = dict(zip(REQUIRED_SKILL_KEYS, SKILL_FOLDERS, strict=True))
PRIVATE_CLEANUP_PREFIX = ".render-cleanup-"
RENDER_TEMPLATE_NAMES = (
    "reference.md.j2",
    "skill.md.j2",
    "alias.md.j2",
    "provenance.md.j2",
    "openai.yaml.j2",
)
RENDER_LOCK_NAMES = (
    "upstream.yaml",
    "stata-help.yaml",
    "packages.yaml",
    "plugin-sdk.yaml",
)


def _close_descriptor_list(
    descriptors: list[int],
    context: str,
) -> tuple[BaseException, ...]:
    """Attempt each distinct descriptor close once and collect every failure."""

    close_errors: list[BaseException] = []
    attempted: set[int] = set()
    for descriptor in reversed(descriptors):
        if descriptor in attempted:
            continue
        attempted.add(descriptor)
        try:
            os.close(descriptor)
        except BaseException as error:
            close_errors.append(error)
    return tuple(close_errors)


def _raise_after_descriptor_finalization(
    primary_error: BaseException | None,
    primary_traceback: object | None,
    close_errors: tuple[BaseException, ...],
    context: str,
) -> None:
    """Preserve an active failure while still attempting every descriptor close."""

    if primary_error is not None:
        if close_errors:
            try:
                primary_error.add_note(
                    f"{context} descriptor finalization also encountered: "
                    + ", ".join(
                        type(error).__name__ for error in close_errors
                    )
                )
            except BaseException:
                pass
        raise primary_error.with_traceback(primary_traceback)
    if close_errors:
        first_error = close_errors[0]
        if len(close_errors) > 1:
            try:
                first_error.add_note(
                    f"{context} additional descriptor finalization failures: "
                    + ", ".join(
                        type(error).__name__ for error in close_errors[1:]
                    )
                )
            except BaseException:
                pass
        raise first_error


@contextmanager
def _descriptor_scope(
    descriptors: list[int],
    context: str,
) -> Iterator[None]:
    """Close every owned descriptor without replacing an active operation error."""

    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    close_errors = _close_descriptor_list(descriptors, context)
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        close_errors,
        context,
    )


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


def _validate_content_directory_names(config: dict) -> None:
    """Reject content roots that are not one ordinary relative component."""

    for skill_key, skill in config["skills"].items():
        content_dir = skill.get("content_dir")
        if not isinstance(content_dir, str) or not content_dir:
            raise ValueError(
                f"skill {skill_key} must define a content_dir"
            )
        candidate = Path(content_dir)
        if (
            candidate.is_absolute()
            or len(candidate.parts) != 1
            or candidate.parts[0] in (".", "..")
        ):
            raise ValueError(
                f"skill {skill_key} content_dir must be one relative component"
            )


def _input_file_version(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_render_input_file(path: Path) -> RenderInputFile:
    """Read one ordinary source file without following its final component."""

    path = Path(path)
    entry_metadata = path.lstat()
    if not stat.S_ISREG(entry_metadata.st_mode):
        raise ValueError(f"renderer input is not an ordinary file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with _descriptor_scope(
        [descriptor],
        f"renderer input capture for {path}",
    ):
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or _input_file_version(opened_metadata)
            != _input_file_version(entry_metadata)
        ):
            raise ValueError(
                f"renderer input changed while it was being opened: {path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        final_metadata = os.fstat(descriptor)
        if _input_file_version(final_metadata) != _input_file_version(
            opened_metadata
        ):
            raise ValueError(
                f"renderer input changed while it was being read: {path}"
            )

    public_metadata = path.lstat()
    if (
        not stat.S_ISREG(public_metadata.st_mode)
        or _input_file_version(public_metadata)
        != _input_file_version(final_metadata)
    ):
        raise ValueError(
            f"renderer input changed after it was read: {path}"
        )
    data = b"".join(chunks)
    return RenderInputFile(
        path=path,
        device=final_metadata.st_dev,
        inode=final_metadata.st_ino,
        size=final_metadata.st_size,
        mtime_ns=final_metadata.st_mtime_ns,
        ctime_ns=final_metadata.st_ctime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _parse_yaml_input(source: RenderInputFile) -> object:
    try:
        return yaml.safe_load(source.data)
    except yaml.YAMLError as error:
        raise ValueError(f"{source.path}: invalid YAML: {error}") from error


def _content_source_paths(
    config: dict,
    content_root: Path,
) -> tuple[tuple[str, Path], ...]:
    paths: list[tuple[str, Path]] = []
    for skill_key, skill in config.get("skills", {}).items():
        content_dir = content_root / skill["content_dir"]
        paths.extend(
            (skill_key, path)
            for path in sorted(content_dir.rglob("*.yaml"))
        )
    return tuple(paths)


def capture_render_inputs(
    config_path: Path,
    content_root: Path,
) -> RenderInputSnapshot:
    """Capture every source byte consulted by one render operation."""

    resolved_config_path = Path(config_path).expanduser().resolve(strict=True)
    resolved_content_root = Path(content_root).expanduser().resolve(strict=True)
    config_file = _capture_render_input_file(resolved_config_path)
    config = _parse_yaml_input(config_file)
    if not isinstance(config, dict):
        raise ValueError(
            f"{resolved_config_path}: expected a YAML mapping"
        )
    validate_required_skills(config)
    _validate_content_directory_names(config)

    content_entries: list[RenderContentEntry] = []
    for skill_key, path in _content_source_paths(
        config,
        resolved_content_root,
    ):
        source = _capture_render_input_file(path)
        content_entries.append(
            RenderContentEntry(
                skill_key=skill_key,
                path=path,
                entry=_parse_yaml_input(source),
                source=source,
            )
        )

    template_files = tuple(
        (
            name,
            _capture_render_input_file(TEMPLATES_ROOT / name),
        )
        for name in RENDER_TEMPLATE_NAMES
    )
    lock_files: list[tuple[str, RenderInputFile | None]] = []
    for name in RENDER_LOCK_NAMES:
        path = LOCK_ROOT / name
        try:
            source = _capture_render_input_file(path)
        except FileNotFoundError:
            source = None
        lock_files.append((name, source))
    return RenderInputSnapshot(
        config=config,
        config_file=config_file,
        content_root=resolved_content_root,
        content_entries=tuple(content_entries),
        template_files=template_files,
        lock_files=tuple(lock_files),
    )


def verify_render_inputs(snapshot: RenderInputSnapshot) -> None:
    """Require all captured input names, identities, and bytes to remain stable."""

    expected_content_paths = tuple(
        (entry.skill_key, entry.path)
        for entry in snapshot.content_entries
    )
    try:
        observed_content_paths = _content_source_paths(
            snapshot.config,
            snapshot.content_root,
        )
        if observed_content_paths != expected_content_paths:
            raise ValueError("renderer content file membership changed")

        sources = [
            snapshot.config_file,
            *(entry.source for entry in snapshot.content_entries),
            *(source for _, source in snapshot.template_files),
        ]
        for source in sources:
            if _capture_render_input_file(source.path) != source:
                raise ValueError(
                    f"renderer input changed after validation: {source.path}"
                )
        for name, source in snapshot.lock_files:
            path = LOCK_ROOT / name
            if source is None:
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                raise ValueError(
                    f"renderer lock appeared after validation: {path}"
                )
            if _capture_render_input_file(path) != source:
                raise ValueError(
                    f"renderer input changed after validation: {path}"
                )
    except (OSError, ValueError) as error:
        raise RenderTransactionError(
            "render inputs changed after validation; refusing replacement"
        ) from error


def validate_render_inputs(
    config: dict,
    content_entries: tuple[RenderContentEntry, ...],
) -> None:
    """Apply the linter's path/schema checks before constructing output paths."""

    from lint_skill_pack import lint_config, lint_entry, lint_route_aliases

    errors = lint_config(config)
    if errors:
        raise ValueError("render input validation failed: " + "; ".join(errors))

    slug_to_skill: dict[str, str] = {}
    route_paths: set[str] = set()
    for content_entry in content_entries:
        skill_key = content_entry.skill_key
        path = content_entry.path
        entry = content_entry.entry
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


def _safe_render_relative_path(*parts: str | Path) -> Path:
    """Return a nonempty relative path without traversal components."""

    candidate = Path(*parts)
    if (
        not candidate.parts
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ValueError(f"render path escapes staging root: {candidate}")
    return candidate


def _existing_ancestor_identities(
    path: Path,
) -> tuple[bool, tuple[tuple[int, int], ...]]:
    """Return path existence and identities from its deepest existing ancestor."""

    current = path
    path_exists = True
    while True:
        try:
            current.stat()
            break
        except FileNotFoundError:
            path_exists = False
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
            return path_exists, tuple(identities)
        current = parent


def _paths_share_location(first: Path, second: Path) -> bool:
    """Compare path locations by identity, with spelling fallback if absent."""

    resolved_first = Path(first).expanduser().resolve(strict=False)
    resolved_second = Path(second).expanduser().resolve(strict=False)
    if resolved_first == resolved_second:
        return True
    first_exists, first_ancestors = _existing_ancestor_identities(
        resolved_first
    )
    second_exists, second_ancestors = _existing_ancestor_identities(
        resolved_second
    )
    return (
        first_exists
        and second_exists
        and first_ancestors[0] == second_ancestors[0]
    )


def _path_contains(container: Path, candidate: Path) -> bool:
    """Return whether container equals or contains candidate by filesystem identity."""

    resolved_container = Path(container).expanduser().resolve(strict=False)
    resolved_candidate = Path(candidate).expanduser().resolve(strict=False)
    if (
        resolved_candidate == resolved_container
        or resolved_candidate.is_relative_to(resolved_container)
    ):
        return True
    container_exists, container_ancestors = _existing_ancestor_identities(
        resolved_container
    )
    _candidate_exists, candidate_ancestors = _existing_ancestor_identities(
        resolved_candidate
    )
    return (
        container_exists
        and container_ancestors[0] in candidate_ancestors
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    """Return whether either path equals or contains the other."""

    return _path_contains(first, second) or _path_contains(second, first)


def _validated_canonical_build_root(requested: Path) -> Path:
    """Anchor the build/generated exception beneath the real repository root."""

    repository_root = Path(os.path.abspath(REPO_ROOT.expanduser()))
    expected_build_root = repository_root / "build" / "generated"
    resolved_build_root = expected_build_root.resolve(strict=False)
    configured_build_root = Path(os.path.abspath(BUILD_ROOT.expanduser()))
    requested_root = Path(os.path.abspath(requested))
    if not _paths_share_location(configured_build_root, expected_build_root):
        raise ValueError(
            "canonical render output must be configured as "
            "REPO_ROOT/build/generated"
        )
    if not _paths_share_location(requested_root, expected_build_root):
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
        with _descriptor_scope(
            descriptors,
            "canonical render-root validation",
        ):
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
                    "repository root changed while validating canonical build "
                    "output"
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
                        "refusing canonical build output with a symbolic-link "
                        f"or non-directory component: {current_path}"
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
    if _paths_share_location(resolved, canonical_build_root):
        return _validated_canonical_build_root(requested)
    repository_metadata = repository_root.stat()
    repository_identity = (
        repository_metadata.st_dev,
        repository_metadata.st_ino,
    )
    _resolved_exists, resolved_ancestors = _existing_ancestor_identities(
        resolved
    )
    if repository_identity in resolved_ancestors:
        raise ValueError(
            "in-repository render output must be build/generated; "
            f"refusing {resolved}"
        )
    return resolved


def _anchor_render_parent(
    output_root: Path,
) -> RenderParentAnchor:
    """Retain the deepest existing destination ancestor without writing."""

    parent = output_root.parent
    ancestor = parent
    missing_components: list[str] = []
    while True:
        try:
            entry_metadata = ancestor.lstat()
            break
        except FileNotFoundError:
            if ancestor.parent == ancestor:
                raise
            missing_components.insert(0, ancestor.name)
            ancestor = ancestor.parent
        except NotADirectoryError as error:
            raise ValueError(
                f"render output parent has a non-directory ancestor: {parent}"
            ) from error
    if not stat.S_ISDIR(entry_metadata.st_mode):
        raise ValueError(
            f"render output ancestor is not an ordinary directory: {ancestor}"
        )
    descriptor = os.open(
        ancestor,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_metadata.st_mode)
            or opened_metadata.st_dev != entry_metadata.st_dev
            or opened_metadata.st_ino != entry_metadata.st_ino
        ):
            raise RenderTransactionError(
                f"render output ancestor changed while opening it: {ancestor}"
            )
    except BaseException as error:
        traceback = error.__traceback__
        _raise_after_descriptor_finalization(
            error,
            traceback,
            _close_descriptor_list(
                [descriptor],
                f"render output ancestor open for {ancestor}",
            ),
            f"render output ancestor open for {ancestor}",
        )
    return RenderParentAnchor(
        parent_path=parent,
        ancestor_path=ancestor,
        device=opened_metadata.st_dev,
        inode=opened_metadata.st_ino,
        descriptor=descriptor,
        missing_components=tuple(missing_components),
    )


def _assert_render_parent_anchor_current(
    anchor: RenderParentAnchor,
) -> None:
    try:
        public_metadata = anchor.ancestor_path.lstat()
        retained_metadata = os.fstat(anchor.descriptor)
    except OSError as error:
        raise RenderTransactionError(
            "render output ancestor changed after destination validation; "
            f"refusing filesystem use: {anchor.ancestor_path}"
        ) from error
    expected_identity = (anchor.device, anchor.inode)
    if (
        not stat.S_ISDIR(public_metadata.st_mode)
        or not stat.S_ISDIR(retained_metadata.st_mode)
        or (public_metadata.st_dev, public_metadata.st_ino)
        != expected_identity
        or (retained_metadata.st_dev, retained_metadata.st_ino)
        != expected_identity
    ):
        raise RenderTransactionError(
            "render output ancestor changed after destination validation; "
            f"refusing filesystem use: {anchor.ancestor_path}"
        )


def _materialize_render_parent(
    anchor: RenderParentAnchor,
) -> RenderParentHandle:
    """Create missing parent components relative to the retained ancestor."""

    _assert_render_parent_anchor_current(anchor)
    owned_descriptors = [os.dup(anchor.descriptor)]
    current_path = anchor.ancestor_path
    result: RenderParentHandle | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        for component in anchor.missing_components:
            descriptor = owned_descriptors[-1]
            current_path = current_path / component
            if _entry_metadata_at(descriptor, component) is not None:
                raise RenderTransactionError(
                    "render output parent component appeared after validation; "
                    f"refusing {current_path}"
                )
            try:
                os.mkdir(component, mode=0o500, dir_fd=descriptor)
            except FileExistsError as error:
                raise RenderTransactionError(
                    "render output parent component appeared during creation; "
                    f"refusing {current_path}"
                ) from error
            created = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            created_identity = (created.st_dev, created.st_ino)
            if (
                not stat.S_ISDIR(created.st_mode)
                or stat.S_IMODE(created.st_mode) != 0o500
            ):
                raise RenderTransactionError(
                    "new render output parent changed before identity capture; "
                    f"refusing {current_path}"
                )
            child_descriptor, opened = _open_directory_at(
                descriptor,
                component,
                current_path,
            )
            owned_descriptors.append(child_descriptor)
            public = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                (opened.st_dev, opened.st_ino) != created_identity
                or (public.st_dev, public.st_ino) != created_identity
                or stat.S_IMODE(opened.st_mode) != 0o500
                or stat.S_IMODE(public.st_mode) != 0o500
                or os.listdir(child_descriptor)
            ):
                raise RenderTransactionError(
                    "new render output parent changed before first use; "
                    f"refusing {current_path}"
                )
            os.fchmod(child_descriptor, CANONICAL_DIRECTORY_MODE)
            os.fsync(child_descriptor)
            held = os.fstat(child_descriptor)
            public = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if (
                (held.st_dev, held.st_ino) != created_identity
                or (public.st_dev, public.st_ino) != created_identity
                or stat.S_IMODE(held.st_mode)
                != CANONICAL_DIRECTORY_MODE
                or stat.S_IMODE(public.st_mode)
                != CANONICAL_DIRECTORY_MODE
            ):
                raise RenderTransactionError(
                    "new render output parent changed during initialization; "
                    f"refusing {current_path}"
                )
            os.fsync(descriptor)
        descriptor = owned_descriptors[-1]
        opened = os.fstat(descriptor)
        result = RenderParentHandle(
            path=anchor.parent_path,
            device=opened.st_dev,
            inode=opened.st_ino,
            descriptor=descriptor,
        )
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    if primary_error is not None:
        _raise_after_descriptor_finalization(
            primary_error,
            primary_traceback,
            _close_descriptor_list(
                owned_descriptors,
                "render-parent materialization",
            ),
            "render-parent materialization",
        )
    assert result is not None
    transferred_descriptor = result.descriptor
    close_errors = _close_descriptor_list(
        owned_descriptors[:-1],
        "render-parent materialization",
    )
    if close_errors:
        close_errors += _close_descriptor_list(
            [transferred_descriptor],
            "render-parent materialization",
        )
        _raise_after_descriptor_finalization(
            None,
            None,
            close_errors,
            "render-parent materialization",
        )
    return result


def _verified_render_parent_path(
    parent: RenderParentHandle,
) -> Path | None:
    """Return only a public path that still names the retained parent."""

    try:
        candidates = [parent.path, *parent.path.parent.iterdir()]
    except BaseException:
        candidates = [parent.path]
    matches: list[Path] = []
    checked: set[Path] = set()
    for candidate in candidates:
        if candidate in checked:
            continue
        checked.add(candidate)
        try:
            metadata = candidate.lstat()
        except BaseException:
            continue
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino)
            == (parent.device, parent.inode)
        ):
            matches.append(candidate)
    if len(matches) != 1:
        return None
    try:
        confirmed = matches[0].lstat()
        if (
            not stat.S_ISDIR(confirmed.st_mode)
            or (confirmed.st_dev, confirmed.st_ino)
            != (parent.device, parent.inode)
        ):
            return None
        return matches[0]
    except BaseException:
        return None


def _descriptor_reported_path(descriptor: int) -> Path | None:
    """Ask the supported host OS for an open descriptor's current pathname."""

    if sys.platform == "darwin":
        try:
            raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
            resolved = raw.split(b"\0", 1)[0]
            if resolved:
                return Path(os.fsdecode(resolved))
        except (OSError, ValueError):
            pass
    elif sys.platform.startswith("linux"):
        try:
            resolved = os.readlink(f"/proc/self/fd/{descriptor}")
            if resolved and not resolved.endswith(" (deleted)"):
                return Path(resolved)
        except OSError:
            pass
    return None


def _verified_descriptor_directory_path(
    descriptor: int,
    expected_identity: tuple[int, int],
) -> Path | None:
    """Return only a reported path that still names the open directory."""

    try:
        held = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(held.st_mode)
            or (held.st_dev, held.st_ino) != expected_identity
        ):
            return None
        candidate = _descriptor_reported_path(descriptor)
        if candidate is None or not candidate.is_absolute():
            return None
        public = candidate.lstat()
        held_after = os.fstat(descriptor)
        public_after = candidate.lstat()
        if (
            stat.S_ISDIR(public.st_mode)
            and stat.S_ISDIR(held_after.st_mode)
            and stat.S_ISDIR(public_after.st_mode)
            and (public.st_dev, public.st_ino) == expected_identity
            and (held_after.st_dev, held_after.st_ino) == expected_identity
            and (public_after.st_dev, public_after.st_ino) == expected_identity
        ):
            return candidate
    except (OSError, ValueError):
        pass
    return None


def _retained_child_location(
    parent: RenderParentHandle,
    expected_identity: tuple[int, int] | None,
    retained_descriptor: int | None = None,
) -> str:
    """Describe one retained direct child without claiming a stale path."""

    if expected_identity is None:
        return "unknown pathname (no accepted root identity)"
    if retained_descriptor is not None:
        current_path = _verified_descriptor_directory_path(
            retained_descriptor,
            expected_identity,
        )
        if current_path is not None:
            return str(current_path)
    try:
        matches = [
            name
            for name in os.listdir(parent.descriptor)
            if (
                (metadata := _entry_metadata_at(parent.descriptor, name))
                is not None
                and (metadata.st_dev, metadata.st_ino)
                == expected_identity
            )
        ]
        if len(matches) == 1:
            name = matches[0]
            confirmed = _entry_metadata_at(parent.descriptor, name)
            current_parent = _verified_render_parent_path(parent)
            if (
                confirmed is not None
                and (confirmed.st_dev, confirmed.st_ino)
                == expected_identity
                and current_parent is not None
            ):
                candidate = current_parent / name
                public = candidate.lstat()
                if (public.st_dev, public.st_ino) == expected_identity:
                    return str(candidate)
    except BaseException:
        pass
    return (
        "unknown pathname beneath the retained output parent "
        f"(device={expected_identity[0]}, inode={expected_identity[1]})"
    )


@contextmanager
def _retained_workspace_scope(
    workspace: Path,
    label: str,
) -> Iterator[None]:
    """Retain and report a caller-owned workspace through open descriptors."""

    parent: RenderParentHandle | None = None
    anchor_descriptor: int | None = None
    workspace_descriptor: int | None = None
    workspace_identity: tuple[int, int] | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
    finalization_errors: list[BaseException] = []
    try:
        anchor = _anchor_render_parent(workspace)
        anchor_descriptor = anchor.descriptor
        parent = RenderParentHandle(
            path=anchor.parent_path,
            device=anchor.device,
            inode=anchor.inode,
            descriptor=anchor.descriptor,
        )
        anchor_descriptor = None
        if (
            anchor.missing_components
            or anchor.ancestor_path != anchor.parent_path
        ):
            raise RenderTransactionError(
                f"{label} workspace parent is not an existing directory: "
                f"{workspace.parent}"
            )
        named = os.stat(
            workspace.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        workspace_identity = (named.st_dev, named.st_ino)
        workspace_descriptor, opened = _open_directory_at(
            parent.descriptor,
            workspace.name,
            workspace,
        )
        public = os.stat(
            workspace.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != workspace_identity
            or (public.st_dev, public.st_ino) != workspace_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(public.st_mode) != 0o700
            or os.listdir(workspace_descriptor)
        ):
            raise RenderTransactionError(
                f"{label} workspace changed before first use: {workspace}"
            )
        yield
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    try:
        location = (
            _retained_child_location(
                parent,
                workspace_identity,
                workspace_descriptor,
            )
            if parent is not None
            else (
                "unknown pathname "
                "(workspace parent identity was not retained)"
            )
        )
        print(
            f"NOTICE: {label} workspace retained for explicit cleanup at: "
            f"{location}"
        )
    except BaseException as reporting_error:
        finalization_errors.append(reporting_error)
    finalization_errors.extend(
        _close_descriptor_list(
            [
                descriptor
                for descriptor in (
                    workspace_descriptor,
                    parent.descriptor if parent is not None else None,
                    anchor_descriptor,
                )
                if descriptor is not None
            ],
            f"{label} workspace retention",
        )
    )
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        tuple(finalization_errors),
        f"{label} workspace retention",
    )


def _assert_render_parent_current(parent: RenderParentHandle) -> None:
    """Require the public path to still name the retained render parent."""

    def recovery_location() -> str:
        recovered = _verified_render_parent_path(parent)
        if recovered is not None:
            return f"; retained output parent survives at {recovered}"
        return (
            "; retained output parent pathname is unknown "
            f"(device={parent.device}, inode={parent.inode})"
        )

    try:
        public_metadata = parent.path.lstat()
        retained_metadata = os.fstat(parent.descriptor)
    except OSError as error:
        raise RenderTransactionError(
            "render output parent changed after validation; refusing "
            f"filesystem use: {parent.path}{recovery_location()}"
        ) from error
    expected_identity = (parent.device, parent.inode)
    if (
        not stat.S_ISDIR(public_metadata.st_mode)
        or not stat.S_ISDIR(retained_metadata.st_mode)
        or (public_metadata.st_dev, public_metadata.st_ino)
        != expected_identity
        or (retained_metadata.st_dev, retained_metadata.st_ino)
        != expected_identity
    ):
        raise RenderTransactionError(
            "render output parent changed after validation; refusing "
            f"filesystem use: {parent.path}{recovery_location()}"
        )


def _create_staged_root_at(
    parent: RenderParentHandle,
    output_name: str,
) -> tuple[Path, os.stat_result]:
    """Create a private staging directory relative to the retained parent."""

    _assert_render_parent_current(parent)
    for _ in range(128):
        stage_name = f".{output_name}.stage-{uuid.uuid4().hex}"
        try:
            os.mkdir(stage_name, mode=0o500, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        created_identity: tuple[int, int] | None = None
        stage_descriptor: int | None = None
        result: tuple[Path, os.stat_result] | None = None
        primary_error: BaseException | None = None
        primary_traceback = None
        try:
            stage_metadata = os.stat(
                stage_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(stage_metadata.st_mode)
                or stat.S_IMODE(stage_metadata.st_mode) != 0o500
            ):
                raise RenderTransactionError(
                    "new render staging entry changed before identity capture"
                )
            created_identity = (
                stage_metadata.st_dev,
                stage_metadata.st_ino,
            )
            stage_descriptor, opened_stage = _open_directory_at(
                parent.descriptor,
                stage_name,
                parent.path / stage_name,
            )
            if (
                (opened_stage.st_dev, opened_stage.st_ino)
                != created_identity
                or stat.S_IMODE(opened_stage.st_mode) != 0o500
                or os.listdir(stage_descriptor)
            ):
                raise RenderTransactionError(
                    "new render staging entry changed before initialization"
                )
            os.fchmod(stage_descriptor, CANONICAL_DIRECTORY_MODE)
            os.fsync(stage_descriptor)
            stage_metadata = os.fstat(stage_descriptor)
            public_stage = os.stat(
                stage_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if (
                (stage_metadata.st_dev, stage_metadata.st_ino)
                != created_identity
                or (public_stage.st_dev, public_stage.st_ino)
                != created_identity
                or stat.S_IMODE(stage_metadata.st_mode)
                != CANONICAL_DIRECTORY_MODE
                or stat.S_IMODE(public_stage.st_mode)
                != CANONICAL_DIRECTORY_MODE
            ):
                raise RenderTransactionError(
                    "new render staging entry changed during initialization"
                )
            os.fsync(parent.descriptor)
            _assert_render_parent_current(parent)
            result = (parent.path / stage_name, stage_metadata)
        except BaseException as error:
            primary_error = error
            primary_traceback = error.__traceback__

        close_errors = _close_descriptor_list(
            [stage_descriptor] if stage_descriptor is not None else [],
            "render-stage initialization",
        )
        if primary_error is not None or close_errors:
            effective_error = (
                primary_error
                if primary_error is not None
                else close_errors[0]
            )
            retained_location = (
                _retained_child_location(parent, created_identity)
                if created_identity is not None
                else (
                    "unknown pathname (new stage identity was not captured; "
                    f"candidate name={stage_name})"
                )
            )
            close_note = (
                "; descriptor finalization also encountered "
                + ", ".join(
                    type(error).__name__ for error in close_errors
                )
                if close_errors
                else ""
            )
            message = (
                "render stage initialization failed; retained stage location: "
                f"{retained_location}{close_note}: {effective_error}"
            )
            if isinstance(effective_error, Exception):
                raise RenderTransactionError(message) from effective_error
            try:
                print(f"WARNING: {message}")
            except BaseException:
                pass
            if primary_error is not None:
                raise primary_error.with_traceback(primary_traceback)
            raise effective_error
        assert result is not None
        return result
    raise RenderTransactionError(
        f"could not allocate a unique render stage beneath {parent.path}"
    )


def _assert_staged_root_identity(
    parent: RenderParentHandle,
    staged_root: Path,
    staged_identity: tuple[int, int],
) -> None:
    if _entry_identity_at(parent, staged_root.name) != staged_identity:
        raise RenderTransactionError(
            f"render staging root changed after creation: {staged_root}"
        )


def _validate_renderer_source_separation(
    output_root: Path,
    content_root: Path,
    config_path: Path,
) -> None:
    """Keep a whole-root replacement separate from every renderer input."""

    sources = (
        ("skill configuration", config_path),
        ("content", content_root),
        ("templates", TEMPLATES_ROOT),
        ("locks", LOCK_ROOT),
    )
    for label, source in sources:
        if _paths_overlap(output_root, source):
            resolved_output = output_root.resolve(strict=False)
            resolved_source = Path(source).expanduser().resolve(strict=False)
            raise ValueError(
                "render output root overlaps a renderer source in either "
                f"direction ({label}): output={resolved_output}; "
                f"source={resolved_source}"
            )


def _assert_no_tracked_canonical_output_paths(output_root: Path) -> None:
    """Refuse to replace force-tracked files beneath build/generated."""

    repository_root = REPO_ROOT.resolve(strict=True)
    canonical_output = (repository_root / "build" / "generated").resolve(
        strict=False
    )
    if not _paths_share_location(output_root, canonical_output):
        return
    conflicts = tuple(
        relative
        for relative in tracked_source_paths(REPO_ROOT)
        if _path_contains(
            canonical_output,
            repository_root / relative,
        )
    )
    if conflicts:
        rendered = ", ".join(path.as_posix() for path in conflicts)
        raise ValueError(
            "canonical render output contains Git-tracked paths and cannot be "
            f"replaced: {rendered}"
        )


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
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=dir_fd,
    )
    with _descriptor_scope(
        [descriptor],
        f"render file hashing for {name}",
    ):
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


def _read_regular_file_no_follow(
    name: str | Path,
    *,
    dir_fd: int | None = None,
    expected_metadata: os.stat_result | None = None,
) -> bytes:
    """Read one stable regular file through a no-follow descriptor."""

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=dir_fd,
    )
    with _descriptor_scope(
        [descriptor],
        f"render file capture for {name}",
    ):
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RenderTransactionError(
                f"refusing to read a non-regular render entry: {name}"
            )
        if expected_metadata is not None and (
            _render_metadata_version(before)
            != _render_metadata_version(expected_metadata)
        ):
            raise RenderTransactionError(
                f"render entry changed before content capture: {name}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _render_metadata_version(after) != _render_metadata_version(before):
            raise RenderTransactionError(
                f"render entry changed during content capture: {name}"
            )
        return b"".join(chunks)


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
                mode=stat.S_IMODE(metadata.st_mode),
                size=metadata.st_size,
                content_sha256=content_sha256,
            )
        )
    return tuple(entries)


def _render_metadata_version(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_directory_descriptor_tree(
    descriptor: int,
    display_path: Path,
    relative_parts: tuple[str, ...] = (),
    capture_file_paths: frozenset[str] = frozenset(),
) -> tuple[RenderTreeEntry, ...]:
    """Capture a stable tree entirely beneath one retained directory."""

    before = os.fstat(descriptor)
    observed_names = sorted(os.listdir(descriptor))
    entries: list[RenderTreeEntry] = []
    for name in observed_names:
        child_path = display_path / name
        child_parts = (*relative_parts, name)
        relative_path = Path(*child_parts).as_posix()
        metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        kind = _entry_kind(metadata)
        content_sha256: str | None = None
        captured_data: bytes | None = None
        if kind == "directory":
            child_descriptor, opened = _open_directory_at(
                descriptor,
                name,
                child_path,
            )
            with _descriptor_scope(
                [child_descriptor],
                f"render subtree capture for {child_path}",
            ):
                if _render_metadata_version(opened) != _render_metadata_version(
                    metadata
                ):
                    raise RenderTransactionError(
                        "render directory changed before descriptor capture: "
                        f"{child_path}"
                    )
                child_entries = _capture_directory_descriptor_tree(
                    child_descriptor,
                    child_path,
                    child_parts,
                    capture_file_paths,
                )
            entries.extend(child_entries)
        elif kind == "file":
            if relative_path in capture_file_paths:
                captured_data = _read_regular_file_no_follow(
                    name,
                    dir_fd=descriptor,
                    expected_metadata=metadata,
                )
                content_sha256 = hashlib.sha256(captured_data).hexdigest()
            else:
                captured_data = None
                content_sha256 = _sha256_regular_file_no_follow(
                    name,
                    dir_fd=descriptor,
                    expected_metadata=metadata,
                )

        final_metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if _render_metadata_version(final_metadata) != _render_metadata_version(
            metadata
        ):
            raise RenderTransactionError(
                f"render entry changed during descriptor capture: {child_path}"
            )
        entries.append(
            RenderTreeEntry(
                relative_path=relative_path,
                kind=kind,
                device=final_metadata.st_dev,
                inode=final_metadata.st_ino,
                mode=stat.S_IMODE(final_metadata.st_mode),
                size=final_metadata.st_size,
                content_sha256=content_sha256,
                captured_data=captured_data,
            )
        )

    after_names = sorted(os.listdir(descriptor))
    after = os.fstat(descriptor)
    if (
        observed_names != after_names
        or _render_metadata_version(before) != _render_metadata_version(after)
    ):
        raise RenderTransactionError(
            f"render directory changed during descriptor capture: {display_path}"
        )
    return tuple(sorted(entries, key=lambda entry: entry.relative_path))


def _capture_output_state_at(
    parent: RenderParentHandle,
    name: str,
    display_path: Path,
    *,
    capture_file_paths: frozenset[str] = frozenset(),
) -> RenderOutputState:
    """Capture one retained-parent directory without resolving its pathname."""

    descriptor, opened = _open_directory_at(
        parent.descriptor,
        name,
        display_path,
    )
    with _descriptor_scope(
        [descriptor],
        f"render-root capture for {display_path}",
    ):
        entries = _capture_directory_descriptor_tree(
            descriptor,
            display_path,
            capture_file_paths=capture_file_paths,
        )
        final = os.fstat(descriptor)
        if _render_metadata_version(final) != _render_metadata_version(opened):
            raise RenderTransactionError(
                f"render root changed during descriptor capture: {display_path}"
            )
        return RenderOutputState(
            exists=True,
            device=final.st_dev,
            inode=final.st_ino,
            mode=stat.S_IMODE(final.st_mode),
            entries=entries,
        )


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
        mode=stat.S_IMODE(after.st_mode),
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


def _verify_output_state_at(
    parent: RenderParentHandle,
    name: str,
    display_path: Path,
    expected: RenderOutputState,
) -> None:
    """Verify a retained-parent child without resolving its parent pathname."""

    if not expected.exists or expected.device is None or expected.inode is None:
        raise RenderTransactionError(
            f"render entry has no accepted identity: {display_path}"
        )
    descriptor, opened = _open_directory_at(
        parent.descriptor,
        name,
        display_path,
    )
    with _descriptor_scope(
        [descriptor],
        f"render-state verification for {display_path}",
    ):
        if (
            opened.st_dev != expected.device
            or opened.st_ino != expected.inode
            or stat.S_IMODE(opened.st_mode) != expected.mode
        ):
            raise RenderTransactionError(
                f"render entry no longer matches its accepted identity: {display_path}"
            )
        _verify_directory_descriptor_tree(
            descriptor,
            display_path,
            {
                entry.relative_path: entry
                for entry in expected.entries
            },
        )


def build_environment(
    template_files: tuple[tuple[str, RenderInputFile], ...],
) -> Environment:
    return Environment(
        loader=DictLoader(
            {
                name: source.data.decode("utf-8")
                for name, source in template_files
            }
        ),
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
    content_entries: tuple[RenderContentEntry, ...],
) -> tuple[dict[str, list[dict]], dict[str, tuple[str, dict]]]:
    grouped: dict[str, list[dict]] = {
        skill_key: [] for skill_key in config.get("skills", {})
    }
    for content_entry in content_entries:
        entry = deepcopy(content_entry.entry)
        if not isinstance(entry, dict):
            raise ValueError(
                f"{content_entry.path}: expected a YAML mapping"
            )
        entry["_source_path"] = content_entry.path
        grouped[content_entry.skill_key].append(entry)
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


def embedded_lock_index(
    lock_files: tuple[tuple[str, RenderInputFile | None], ...],
) -> list[dict]:
    index: list[dict] = []
    labels = {
        "upstream.yaml": "Upstream repository",
        "stata-help.yaml": "Local Stata help",
        "packages.yaml": "Community package distributions",
        "plugin-sdk.yaml": "Plugin SDK",
    }
    for filename, source in lock_files:
        if source is None:
            continue
        data = _parse_yaml_input(source)
        if not isinstance(data, dict):
            raise ValueError(f"{source.path}: expected a YAML mapping")
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
                "label": labels[filename],
                "identity": identity,
                "sha256": source.sha256,
            }
        )
    return index


def _write_staged_text(
    parent: RenderParentHandle,
    staged_root: Path,
    staged_identity: tuple[int, int],
    directory_identities: dict[tuple[str, ...], tuple[int, int]],
    relative_path: Path,
    text: str,
) -> None:
    """Create one staged file entirely through retained directory descriptors."""

    relative_path = _safe_render_relative_path(relative_path)
    root_descriptor, opened_root = _open_directory_at(
        parent.descriptor,
        staged_root.name,
        staged_root,
    )
    descriptors = [root_descriptor]
    with _descriptor_scope(
        descriptors,
        f"staged render write for {relative_path}",
    ):
        if (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != staged_identity:
            raise RenderTransactionError(
                f"render staging root changed before writing {relative_path}"
            )
        current_descriptor = root_descriptor
        current_display_path = staged_root
        current_parts: tuple[str, ...] = ()
        for component in relative_path.parts[:-1]:
            current_display_path = current_display_path / component
            current_parts = (*current_parts, component)
            expected_identity = directory_identities.get(current_parts)
            created = False
            created_identity: tuple[int, int] | None = None
            if expected_identity is None:
                try:
                    os.mkdir(
                        component,
                        mode=0o500,
                        dir_fd=current_descriptor,
                    )
                    created = True
                except FileExistsError as error:
                    raise RenderTransactionError(
                        "unexpected pre-existing staging directory before "
                        f"writing {relative_path}: {current_display_path}"
                    ) from error
                created_metadata = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(created_metadata.st_mode)
                    or stat.S_IMODE(created_metadata.st_mode) != 0o500
                ):
                    raise RenderTransactionError(
                        "new staging directory changed before identity capture "
                        f"while writing {relative_path}: "
                        f"{current_display_path}"
                    )
                created_identity = (
                    created_metadata.st_dev,
                    created_metadata.st_ino,
                )
            else:
                existing = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(existing.st_mode)
                    or (existing.st_dev, existing.st_ino)
                    != expected_identity
                ):
                    raise RenderTransactionError(
                        "staging directory identity changed before writing "
                        f"{relative_path}: {current_display_path}"
                    )
            if created:
                os.fsync(current_descriptor)
            child_descriptor, opened = _open_directory_at(
                current_descriptor,
                component,
                current_display_path,
            )
            descriptors.append(child_descriptor)
            opened_identity = (opened.st_dev, opened.st_ino)
            if created:
                public = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
                if (
                    opened_identity != created_identity
                    or (public.st_dev, public.st_ino) != created_identity
                    or stat.S_IMODE(opened.st_mode) != 0o500
                    or stat.S_IMODE(public.st_mode) != 0o500
                    or os.listdir(child_descriptor)
                ):
                    raise RenderTransactionError(
                        "new staging directory changed before first use while "
                        f"writing {relative_path}: {current_display_path}"
                    )
                assert created_identity is not None
                directory_identities[current_parts] = created_identity
                os.fchmod(child_descriptor, CANONICAL_DIRECTORY_MODE)
                os.fsync(child_descriptor)
            elif (
                opened_identity != expected_identity
                or stat.S_IMODE(opened.st_mode)
                != CANONICAL_DIRECTORY_MODE
            ):
                raise RenderTransactionError(
                    "staging directory changed before writing "
                    f"{relative_path}: {current_display_path}"
                )
            current_descriptor = child_descriptor

        filename = relative_path.name
        descriptor = os.open(
            filename,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=current_descriptor,
        )
        descriptors.append(descriptor)
        os.fchmod(descriptor, CANONICAL_FILE_MODE)
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(
                    f"short write while rendering {relative_path}"
                )
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.fsync(current_descriptor)


def _render_tree(
    output_root: Path,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
    template_files: tuple[tuple[str, RenderInputFile], ...],
    lock_files: tuple[tuple[str, RenderInputFile | None], ...],
    parent: RenderParentHandle,
    staged_identity: tuple[int, int],
) -> None:
    env = build_environment(template_files)
    lock_index = embedded_lock_index(lock_files)
    directory_identities: dict[
        tuple[str, ...],
        tuple[int, int],
    ] = {(): staged_identity}

    reference_template = env.get_template("reference.md.j2")
    skill_template = env.get_template("skill.md.j2")
    alias_template = env.get_template("alias.md.j2")
    provenance_template = env.get_template("provenance.md.j2")
    openai_template = env.get_template("openai.yaml.j2")

    for skill_key, skill in config["skills"].items():
        folder = _safe_render_relative_path(skill["folder"])
        route_dir = _safe_render_relative_path(
            folder,
            skill["route_dir"],
        )

        entries = grouped[skill_key]
        sections: OrderedDict[str, list[dict]] = OrderedDict(
            (section, []) for section in skill["section_order"]
        )
        for entry in entries:
            sections[entry["section"]].append(entry)
            _write_staged_text(
                parent,
                output_root,
                staged_identity,
                directory_identities,
                _safe_render_relative_path(
                    route_dir,
                    f"{entry['slug']}.md",
                ),
                normalized_markdown(reference_template.render(entry=entry)),
            )

        route_aliases = aliases_by_skill[skill_key]
        for alias in route_aliases:
            alias_path = _safe_render_relative_path(
                folder,
                alias["from_route"],
            )
            _write_staged_text(
                parent,
                output_root,
                staged_identity,
                directory_identities,
                alias_path,
                normalized_markdown(alias_template.render(alias=alias)),
            )

        section_payload = [
            {"name": name, "entries": section_entries}
            for name, section_entries in sections.items()
            if section_entries
        ]
        _write_staged_text(
            parent,
            output_root,
            staged_identity,
            directory_identities,
            _safe_render_relative_path(folder, "SKILL.md"),
            normalized_markdown(
                skill_template.render(
                    skill=skill,
                    sections=section_payload,
                    route_aliases=route_aliases,
                )
            ),
        )
        _write_staged_text(
            parent,
            output_root,
            staged_identity,
            directory_identities,
            _safe_render_relative_path(folder, "PROVENANCE.md"),
            normalized_markdown(
                provenance_template.render(
                    skill=skill,
                    entries=entries,
                    lock_index=lock_index,
                )
            ),
        )
        _write_staged_text(
            parent,
            output_root,
            staged_identity,
            directory_identities,
            _safe_render_relative_path(
                folder,
                "agents",
                "openai.yaml",
            ),
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
    mode_paths = [output_root, *sorted(output_root.rglob("*"))]
    for path in mode_paths:
        metadata = path.lstat()
        relative = (
            "."
            if path == output_root
            else path.relative_to(output_root).as_posix()
        )
        if stat.S_ISDIR(metadata.st_mode):
            expected_mode = CANONICAL_DIRECTORY_MODE
            kind = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            expected_mode = CANONICAL_FILE_MODE
            kind = "file"
        else:
            continue
        observed_mode = stat.S_IMODE(metadata.st_mode)
        if observed_mode != expected_mode:
            errors.append(
                f"{relative}: {kind} permissions {observed_mode:04o}; "
                f"expected {expected_mode:04o}"
            )
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


def validate_rendered_state(
    state: RenderOutputState,
    config: dict,
    grouped: dict[str, list[dict]],
    aliases_by_skill: dict[str, list[dict]],
) -> None:
    """Validate a staged tree solely from its descriptor-captured state."""

    expected = _expected_rendered_files(config, grouped, aliases_by_skill)
    entries_by_path = {
        entry.relative_path: entry for entry in state.entries
    }
    errors: list[str] = []
    if len(entries_by_path) != len(state.entries):
        errors.append("descriptor inventory contains duplicate paths")
    if not state.exists:
        errors.append("staging root is absent")
    if state.mode != CANONICAL_DIRECTORY_MODE:
        observed = state.mode if state.mode is not None else 0
        errors.append(
            f".: directory permissions {observed:04o}; "
            f"expected {CANONICAL_DIRECTORY_MODE:04o}"
        )

    actual_files = {
        Path(path)
        for path, entry in entries_by_path.items()
        if entry.kind == "file"
    }
    actual_directories = {
        Path(path)
        for path, entry in entries_by_path.items()
        if entry.kind == "directory"
    }
    unexpected_kinds = sorted(
        path
        for path, entry in entries_by_path.items()
        if entry.kind not in ("file", "directory")
    )
    if unexpected_kinds:
        errors.append(
            "non-file entries are not publishable: "
            + ", ".join(unexpected_kinds)
        )

    for relative_path, entry in sorted(entries_by_path.items()):
        if entry.kind == "directory":
            expected_mode = CANONICAL_DIRECTORY_MODE
        elif entry.kind == "file":
            expected_mode = CANONICAL_FILE_MODE
        else:
            continue
        if entry.mode != expected_mode:
            errors.append(
                f"{relative_path}: {entry.kind} permissions "
                f"{entry.mode:04o}; expected {expected_mode:04o}"
            )

    missing = sorted(str(path) for path in expected - actual_files)
    extra = sorted(str(path) for path in actual_files - expected)
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

    empty = sorted(
        str(relative)
        for relative in expected & actual_files
        if entries_by_path[relative.as_posix()].size == 0
    )
    if empty:
        errors.append(f"empty files: {', '.join(empty)}")

    for skill in config["skills"].values():
        relative_metadata_path = Path(
            skill["folder"],
            "agents",
            "openai.yaml",
        )
        entry = entries_by_path.get(relative_metadata_path.as_posix())
        if entry is None or entry.kind != "file":
            continue
        if entry.captured_data is None:
            errors.append(
                f"{relative_metadata_path}: metadata bytes were not captured"
            )
            continue
        try:
            metadata = yaml.safe_load(entry.captured_data)
        except yaml.YAMLError as error:
            errors.append(f"{relative_metadata_path}: invalid YAML: {error}")
            continue
        if (
            not isinstance(metadata, dict)
            or metadata.get("interface") != skill["interface"]
        ):
            errors.append(
                f"{relative_metadata_path}: interface metadata does not match "
                "configuration"
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

    try:
        _shared_atomic_rename_at_no_replace(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )
    except RuntimeError as error:
        raise RenderTransactionError(str(error)) from error


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
    primary_error: BaseException | None = None
    primary_traceback = None
    opened: os.stat_result | None = None
    try:
        opened = os.fstat(descriptor)
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    if primary_error is not None:
        _raise_after_descriptor_finalization(
            primary_error,
            primary_traceback,
            _close_descriptor_list(
                [descriptor],
                f"render directory open for {display_path}",
            ),
            f"render directory open for {display_path}",
        )
    assert opened is not None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != metadata.st_dev
        or opened.st_ino != metadata.st_ino
    ):
        identity_error = RenderTransactionError(
            f"render backup changed while opening it: {display_path}"
        )
        _raise_after_descriptor_finalization(
            identity_error,
            identity_error.__traceback__,
            _close_descriptor_list(
                [descriptor],
                f"render directory open for {display_path}",
            ),
            f"render directory open for {display_path}",
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
        and stat.S_IMODE(metadata.st_mode) == expected.mode
    )


def _verify_directory_descriptor_tree(
    descriptor: int,
    display_path: Path,
    expected_entries: dict[str, RenderTreeEntry],
    relative_parts: tuple[str, ...] = (),
) -> None:
    """Verify a complete anchored tree without deleting any of its entries."""

    before = os.fstat(descriptor)
    observed_names = sorted(os.listdir(descriptor))
    expected_names = sorted(
        Path(relative_path).name
        for relative_path in expected_entries
        if Path(relative_path).parts[:-1] == relative_parts
    )
    if observed_names != expected_names:
        raise RenderTransactionError(
            "render backup changed during private pre-delete verification at "
            f"{display_path}"
        )

    for name in observed_names:
        child_path = display_path / name
        child_parts = (*relative_parts, name)
        relative_path = Path(*child_parts).as_posix()
        expected_entry = expected_entries.get(relative_path)
        if expected_entry is None:
            raise RenderTransactionError(
                "render cleanup manifest is missing an entry for "
                f"{child_path}"
            )
        metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if not _entry_matches(metadata, expected_entry):
            raise RenderTransactionError(
                "render backup entry changed during private pre-delete "
                f"verification: {child_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            child_descriptor, opened = _open_directory_at(
                descriptor,
                name,
                child_path,
            )
            with _descriptor_scope(
                [child_descriptor],
                f"render subtree verification for {child_path}",
            ):
                if not _entry_matches(opened, expected_entry):
                    raise RenderTransactionError(
                        "render backup directory changed during private "
                        f"pre-delete verification: {child_path}"
                    )
                _verify_directory_descriptor_tree(
                    child_descriptor,
                    child_path,
                    expected_entries,
                    child_parts,
                )
        elif stat.S_ISREG(metadata.st_mode):
            digest = _sha256_regular_file_no_follow(
                name,
                dir_fd=descriptor,
                expected_metadata=metadata,
            )
            final_metadata = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            before_version = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            final_version = (
                final_metadata.st_dev,
                final_metadata.st_ino,
                final_metadata.st_size,
                final_metadata.st_mtime_ns,
                final_metadata.st_ctime_ns,
            )
            if (
                expected_entry.content_sha256 is None
                or digest != expected_entry.content_sha256
                or before_version != final_version
            ):
                raise RenderTransactionError(
                    "render backup file changed during private pre-delete "
                    f"verification: {child_path}"
                )
        else:
            raise RenderTransactionError(
                f"refusing unexpected render backup entry: {child_path}"
            )

        final_metadata = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if not _entry_matches(final_metadata, expected_entry):
            raise RenderTransactionError(
                "render backup entry changed after private pre-delete "
                f"verification: {child_path}"
            )

    after_names = sorted(os.listdir(descriptor))
    after = os.fstat(descriptor)
    before_version = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_version = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if observed_names != after_names or before_version != after_version:
        raise RenderTransactionError(
            "render backup changed across private pre-delete verification at "
            f"{display_path}"
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
    quarantine_path: Path,
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
            f"could not restore it at {display_path}; its bytes remain "
            f"preserved at {quarantine_path}"
        ) from restore_error


def _remove_fresh_private_cleanup_directory(
    parent_descriptor: int,
    cleanup_name: str,
    cleanup_path: Path,
    expected_metadata: os.stat_result,
) -> None:
    """Remove one freshly created private cleanup directory after verification."""

    observed_cleanup = os.stat(
        cleanup_name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed_cleanup.st_mode)
        or observed_cleanup.st_dev != expected_metadata.st_dev
        or observed_cleanup.st_ino != expected_metadata.st_ino
    ):
        raise RenderTransactionError(
            f"private render cleanup quarantine changed for {cleanup_path}"
        )
    # POSIX has no inode-conditional rmdir. Replacing this random entry after
    # the final check requires a same-user process racing the private quarantine.
    os.rmdir(cleanup_name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


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

    cleanup_name = f"{PRIVATE_CLEANUP_PREFIX}{uuid.uuid4().hex}"
    cleanup_path = display_path.parent / cleanup_name
    os.mkdir(cleanup_name, mode=0o700, dir_fd=parent_descriptor)
    cleanup_descriptor: int | None = None
    cleanup_opened: os.stat_result | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
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
                    cleanup_path / name,
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
                    cleanup_path / name,
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
            with _descriptor_scope(
                [moved_descriptor],
                f"empty-directory verification for {display_path}",
            ):
                if not matches_expected(opened) or os.listdir(moved_descriptor):
                    raise RenderTransactionError(
                        "verified empty render directory changed in private "
                        f"cleanup quarantine: {display_path}"
                    )
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
                    cleanup_path / name,
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
                    cleanup_path / name,
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
                    cleanup_path / name,
                )
            raise RenderTransactionError(
                "verified empty render directory could not be removed from "
                f"private cleanup quarantine and was preserved: {display_path}"
            ) from removal_error
        os.fsync(cleanup_descriptor)
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        _close_descriptor_list(
            [cleanup_descriptor] if cleanup_descriptor is not None else [],
            f"empty-directory cleanup quarantine for {display_path}",
        ),
        f"empty-directory cleanup quarantine for {display_path}",
    )

    if cleanup_opened is None:
        raise RenderTransactionError(
            f"render cleanup quarantine was not opened for {display_path}"
        )
    _remove_fresh_private_cleanup_directory(
        parent_descriptor,
        cleanup_name,
        cleanup_path,
        cleanup_opened,
    )


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

    quarantine_name = f"{PRIVATE_CLEANUP_PREFIX}{uuid.uuid4().hex}"
    os.mkdir(quarantine_name, mode=0o700, dir_fd=descriptor)
    quarantine_descriptor: int | None = None
    quarantine_opened: os.stat_result | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
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
                        display_path / quarantine_name / name,
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
                    display_path / quarantine_name / name,
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
                with _descriptor_scope(
                    [child_descriptor],
                    f"recursive render cleanup for {child_path}",
                ):
                    _clear_directory_descriptor(
                        child_descriptor,
                        child_path,
                        expected_entries,
                        child_parts,
                    )
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
                    display_path / quarantine_name / name,
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
                    display_path / quarantine_name / name,
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
                    display_path / quarantine_name / name,
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
                    display_path / quarantine_name / name,
                )
                raise RenderTransactionError(
                    "render backup file changed immediately before deletion "
                    f"and was preserved: {child_path}"
                )
            os.unlink(name, dir_fd=quarantine_descriptor)

        os.fsync(quarantine_descriptor)
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    _raise_after_descriptor_finalization(
        primary_error,
        primary_traceback,
        _close_descriptor_list(
            [quarantine_descriptor]
            if quarantine_descriptor is not None
            else [],
            f"render cleanup quarantine for {display_path}",
        ),
        f"render cleanup quarantine for {display_path}",
    )

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


def _retain_verified_backup(
    backup_root: Path,
    expected: RenderOutputState,
    parent: RenderParentHandle | None = None,
) -> Path:
    """Retain an exact prior tree for explicit, quiescent cleanup."""

    if parent is None:
        _verify_moved_output_state(backup_root, expected)
    else:
        _verify_output_state_at(
            parent,
            backup_root.name,
            backup_root,
            expected,
        )
    if expected.device is None or expected.inode is None:
        raise RenderTransactionError(
            f"render backup has no accepted identity: {backup_root}"
        )
    retained = _retain_owned_directory(
        backup_root,
        expected.device,
        expected.inode,
        expected.entries,
        expected_mode=expected.mode,
        parent_descriptor=(
            parent.descriptor if parent is not None else None
        ),
    )
    if parent is not None:
        _assert_render_parent_current(parent)
    return retained


def _retain_owned_directory(
    directory: Path,
    expected_device: int,
    expected_inode: int,
    expected_entries: tuple[RenderTreeEntry, ...],
    *,
    expected_mode: int | None = None,
    parent_descriptor: int | None = None,
) -> Path:
    """Verify an owned tree twice and retain every entry for explicit cleanup."""

    if parent_descriptor is None:
        owned_parent_descriptor = os.open(
            directory.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        owned_parent_descriptor = os.dup(parent_descriptor)
    descriptors = [owned_parent_descriptor]
    with _descriptor_scope(
        descriptors,
        f"retained render-tree verification for {directory}",
    ):
        descriptor, opened = _open_directory_at(
            owned_parent_descriptor,
            directory.name,
            directory,
        )
        descriptors.append(descriptor)
        if (
            opened.st_dev != expected_device
            or opened.st_ino != expected_inode
            or (
                expected_mode is not None
                and stat.S_IMODE(opened.st_mode) != expected_mode
            )
        ):
            raise RenderTransactionError(
                "directory no longer matches its captured identity; "
                f"preserving {directory}"
            )
        entries_by_path = {
            entry.relative_path: entry
            for entry in expected_entries
        }
        _verify_directory_descriptor_tree(
            descriptor,
            directory,
            entries_by_path,
        )
        held = os.fstat(descriptor)
        public = os.stat(
            directory.name,
            dir_fd=owned_parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(public.st_mode)
            or (held.st_dev, held.st_ino)
            != (expected_device, expected_inode)
            or (public.st_dev, public.st_ino)
            != (expected_device, expected_inode)
            or (
                expected_mode is not None
                and (
                    stat.S_IMODE(held.st_mode) != expected_mode
                    or stat.S_IMODE(public.st_mode) != expected_mode
                )
            )
        ):
            raise RenderTransactionError(
                "directory changed during retention verification; "
                f"preserving descriptor-bound state for {directory}"
            )
        _verify_directory_descriptor_tree(
            descriptor,
            directory,
            entries_by_path,
        )
        return directory


def _remove_owned_directory(
    directory: Path,
    expected_device: int,
    expected_inode: int,
    expected_entries: tuple[RenderTreeEntry, ...],
    *,
    expected_mode: int | None = None,
    parent_descriptor: int | None = None,
) -> None:
    """Delete an owned tree only under explicit, caller-proven quiescence.

    Automatic render and validation workflows must use
    :func:`_retain_owned_directory` instead.
    """

    if parent_descriptor is None:
        owned_parent_descriptor = os.open(
            directory.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        owned_parent_descriptor = os.dup(parent_descriptor)
    parent_descriptor = owned_parent_descriptor
    backup_descriptor: int | None = None
    cleanup_descriptor: int | None = None
    cleanup_name: str | None = None
    cleanup_path: Path | None = None
    cleanup_opened: os.stat_result | None = None
    moved_root_path: Path | None = None
    preserve_private_root = False
    try:
        backup_descriptor, opened = _open_directory_at(
            parent_descriptor,
            directory.name,
            directory,
        )
        if (
            opened.st_dev != expected_device
            or opened.st_ino != expected_inode
            or (
                expected_mode is not None
                and stat.S_IMODE(opened.st_mode) != expected_mode
            )
        ):
            raise RenderTransactionError(
                "directory no longer matches its captured identity; "
                f"preserving {directory}"
            )
        entries_by_path = {
            entry.relative_path: entry
            for entry in expected_entries
        }
        cleanup_name = f"{PRIVATE_CLEANUP_PREFIX}{uuid.uuid4().hex}"
        cleanup_path = directory.parent / cleanup_name
        moved_root_path = cleanup_path / directory.name
        os.mkdir(cleanup_name, mode=0o700, dir_fd=parent_descriptor)
        cleanup_descriptor, cleanup_opened = _open_directory_at(
            parent_descriptor,
            cleanup_name,
            cleanup_path,
        )
        os.fchmod(cleanup_descriptor, 0o700)
        move_error: BaseException | None = None
        try:
            _atomic_rename_at_no_replace(
                parent_descriptor,
                directory.name,
                cleanup_descriptor,
                directory.name,
            )
        except BaseException as error:
            move_error = error

        moved = _entry_metadata_at(cleanup_descriptor, directory.name)
        public = _entry_metadata_at(parent_descriptor, directory.name)
        moved_matches = (
            moved is not None
            and stat.S_ISDIR(moved.st_mode)
            and moved.st_dev == expected_device
            and moved.st_ino == expected_inode
            and (
                expected_mode is None
                or stat.S_IMODE(moved.st_mode) == expected_mode
            )
        )
        if not moved_matches:
            if moved is not None and public is None:
                _restore_quarantined_entry(
                    cleanup_descriptor,
                    parent_descriptor,
                    directory.name,
                    moved,
                    directory,
                    moved_root_path,
                )
            raise RenderTransactionError(
                "render backup root changed while moving into private cleanup "
                f"quarantine and was preserved: {directory}"
            ) from move_error
        if public is not None:
            preserve_private_root = True
            raise RenderTransactionError(
                "render backup public name reappeared during private cleanup; "
                f"both entries were preserved at {directory} and "
                f"{moved_root_path}"
            ) from move_error
        if move_error is not None:
            # The no-replace syscall reported an error after the observable move
            # completed. Continue only because both descriptor-bound identities
            # prove that the accepted root is privately quarantined.
            move_error = None

        held = os.fstat(backup_descriptor)
        if (
            not stat.S_ISDIR(held.st_mode)
            or held.st_dev != expected_device
            or held.st_ino != expected_inode
            or (
                expected_mode is not None
                and stat.S_IMODE(held.st_mode) != expected_mode
            )
        ):
            raise RenderTransactionError(
                "render backup root identity changed after private quarantine; "
                f"preserving {moved_root_path}"
            )
        _verify_directory_descriptor_tree(
            backup_descriptor,
            moved_root_path,
            entries_by_path,
        )
        moved_after_verification = _entry_metadata_at(
            cleanup_descriptor,
            directory.name,
        )
        public_cleanup_after_verification = _entry_metadata_at(
            parent_descriptor,
            cleanup_name,
        )
        public_names_after_verification = set(os.listdir(parent_descriptor))
        unexpected_cleanup_names = sorted(
            name
            for name in public_names_after_verification
            if name.startswith(PRIVATE_CLEANUP_PREFIX)
            and name != cleanup_name
        )
        if (
            moved_after_verification is None
            or not stat.S_ISDIR(moved_after_verification.st_mode)
            or moved_after_verification.st_dev != expected_device
            or moved_after_verification.st_ino != expected_inode
            or (
                expected_mode is not None
                and stat.S_IMODE(moved_after_verification.st_mode)
                != expected_mode
            )
        ):
            raise RenderTransactionError(
                "render backup root changed after private pre-delete "
                f"verification; preserving {moved_root_path}"
            )
        if (
            public_cleanup_after_verification is None
            or not stat.S_ISDIR(public_cleanup_after_verification.st_mode)
            or cleanup_opened is None
            or public_cleanup_after_verification.st_dev != cleanup_opened.st_dev
            or public_cleanup_after_verification.st_ino != cleanup_opened.st_ino
        ):
            raise RenderTransactionError(
                "private render cleanup root changed before recursive cleanup; "
                "aborting before accepted-tree deletion"
            )
        if directory.name in public_names_after_verification:
            preserve_private_root = True
            raise RenderTransactionError(
                "render backup public name reappeared before private recursive "
                f"cleanup; preserving both entries at {directory} and "
                f"{moved_root_path}"
            )
        if unexpected_cleanup_names:
            preserve_private_root = True
            raise RenderTransactionError(
                "unexpected private render cleanup sibling appeared before "
                "recursive cleanup; preserving the accepted tree at "
                f"{moved_root_path}: {', '.join(unexpected_cleanup_names)}"
            )

        _clear_directory_descriptor(
            backup_descriptor,
            moved_root_path,
            entries_by_path,
        )
        descriptor_to_close = backup_descriptor
        backup_descriptor = None
        os.close(descriptor_to_close)
        _remove_verified_empty_directory_via_quarantine(
            cleanup_descriptor,
            directory.name,
            moved_root_path,
            opened,
        )
        if _entry_metadata_at(parent_descriptor, directory.name) is not None:
            raise RenderTransactionError(
                "render backup public name reappeared during private cleanup; "
                f"preserving concurrent state at {directory}"
            )
        cleanup_descriptor_to_close = cleanup_descriptor
        cleanup_descriptor = None
        os.close(cleanup_descriptor_to_close)
        if cleanup_name is None or cleanup_path is None or cleanup_opened is None:
            raise RenderTransactionError(
                f"render cleanup quarantine was not anchored for {directory}"
            )
        _remove_fresh_private_cleanup_directory(
            parent_descriptor,
            cleanup_name,
            cleanup_path,
            cleanup_opened,
        )
    except BaseException as cleanup_error:
        recovery_errors: list[str] = []
        if cleanup_descriptor is not None and cleanup_name is not None:
            moved = _entry_metadata_at(cleanup_descriptor, directory.name)
            public = _entry_metadata_at(parent_descriptor, directory.name)
            if (
                moved is not None
                and public is None
                and not preserve_private_root
            ):
                try:
                    _restore_quarantined_entry(
                        cleanup_descriptor,
                        parent_descriptor,
                        directory.name,
                        moved,
                        directory,
                        cleanup_path / directory.name,
                    )
                except BaseException as restore_error:
                    recovery_errors.append(str(restore_error))
            try:
                cleanup_is_empty = not os.listdir(cleanup_descriptor)
            except OSError as inspect_error:
                recovery_errors.append(
                    f"private cleanup inspection failed: {inspect_error}"
                )
                cleanup_is_empty = False
            descriptor_to_close = cleanup_descriptor
            cleanup_descriptor = None
            try:
                os.close(descriptor_to_close)
            except BaseException as close_error:
                recovery_errors.append(
                    "private cleanup descriptor closure failed with "
                    f"{type(close_error).__name__}: {close_error}"
                )
            if (
                cleanup_is_empty
                and cleanup_path is not None
                and cleanup_opened is not None
            ):
                try:
                    _remove_fresh_private_cleanup_directory(
                        parent_descriptor,
                        cleanup_name,
                        cleanup_path,
                        cleanup_opened,
                    )
                except BaseException as removal_error:
                    recovery_errors.append(
                        f"empty private cleanup removal failed: {removal_error}"
                    )
        if recovery_errors:
            raise RenderTransactionError(
                f"{cleanup_error}; render cleanup recovery also reported: "
                + "; ".join(recovery_errors)
            ) from cleanup_error
        raise
    finally:
        close_errors = _close_descriptor_list(
            [
                descriptor
                for descriptor in (
                    parent_descriptor,
                    cleanup_descriptor,
                    backup_descriptor,
                )
                if descriptor is not None
            ],
            f"explicit render cleanup for {directory}",
        )
        if close_errors:
            active_error = sys.exception()
            _raise_after_descriptor_finalization(
                active_error,
                (
                    active_error.__traceback__
                    if active_error is not None
                    else None
                ),
                close_errors,
                f"explicit render cleanup for {directory}",
            )
    return None


def _retain_staged_root(
    staged_root: Path,
    expected_device: int,
    expected_inode: int,
    expected_entries: tuple[RenderTreeEntry, ...],
    expected_mode: int | None = None,
    parent: RenderParentHandle | None = None,
    committed_output_root: Path | None = None,
) -> None:
    expected_identity = (expected_device, expected_inode)

    def retained_location() -> str:
        if parent is not None:
            return _retained_child_location(parent, expected_identity)
        return str(staged_root)

    def committed_output_holds_identity() -> bool:
        return (
            parent is not None
            and committed_output_root is not None
            and _entry_identity_at(parent, committed_output_root.name)
            == expected_identity
        )

    try:
        _retain_owned_directory(
            staged_root,
            expected_device,
            expected_inode,
            expected_entries,
            expected_mode=expected_mode,
            parent_descriptor=(
                parent.descriptor if parent is not None else None
            ),
        )
        if parent is not None:
            _assert_render_parent_current(parent)
        print(
            "NOTICE: staged render tree retained for explicit cleanup at: "
            f"{retained_location()}"
        )
    except FileNotFoundError:
        if committed_output_holds_identity():
            return
        print(
            "WARNING: staged render path no longer names the accepted tree; "
            f"retained stage location: {retained_location()}"
        )
        return
    except (OSError, RenderTransactionError) as cleanup_error:
        if committed_output_holds_identity():
            print(
                "WARNING: the former render stage name contains unverified "
                f"state and was left unchanged: {staged_root}: {cleanup_error}"
            )
            return
        print(
            "WARNING: staged render cleanup was skipped because the path no "
            "longer matched the created directory; retained stage location: "
            f"{retained_location()}: "
            f"{cleanup_error}"
        )


def _path_identity(path: Path) -> tuple[int, int] | None:
    """Return a directory-entry identity without following symbolic links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _entry_identity_at(
    parent: RenderParentHandle,
    name: str,
) -> tuple[int, int] | None:
    metadata = _entry_metadata_at(parent.descriptor, name)
    if metadata is None:
        return None
    return metadata.st_dev, metadata.st_ino


def _rename_render_entry(
    parent: RenderParentHandle,
    source_name: str,
    destination_name: str,
) -> None:
    """Move one top-level transaction entry within the retained parent."""

    try:
        _atomic_rename_at_no_replace(
            parent.descriptor,
            source_name,
            parent.descriptor,
            destination_name,
        )
    except BaseException:
        try:
            os.fsync(parent.descriptor)
        except OSError:
            pass
        raise
    os.fsync(parent.descriptor)


def _state_root_identity(expected: RenderOutputState) -> tuple[int, int] | None:
    if (
        not expected.exists
        or expected.device is None
        or expected.inode is None
    ):
        return None
    return expected.device, expected.inode


def _preserve_installed_entry(
    parent: RenderParentHandle,
    output_root: Path,
    installed_identity: tuple[int, int],
) -> Path:
    """Move a rejected installed entry to a unique recovery path."""

    if _entry_identity_at(parent, output_root.name) != installed_identity:
        raise RenderTransactionError(
            "the rejected installed entry changed before it could be moved "
            f"from {output_root}"
        )
    recovery_name = f".{output_root.name}.recovery-{uuid.uuid4().hex}"
    recovery_root = parent.path / recovery_name
    try:
        _rename_render_entry(
            parent,
            output_root.name,
            recovery_name,
        )
    except BaseException as move_error:
        if (
            _entry_identity_at(parent, recovery_name) == installed_identity
            and _entry_identity_at(parent, output_root.name) is None
        ):
            return recovery_root
        raise RenderTransactionError(
            "the rejected installed entry could not be moved from "
            f"{output_root}"
        ) from move_error
    if (
        _entry_identity_at(parent, recovery_name) != installed_identity
        or _entry_identity_at(parent, output_root.name) is not None
    ):
        raise RenderTransactionError(
            "the rejected installed entry changed while it was being "
            f"preserved at {recovery_root}"
        )
    return recovery_root


def _restore_prior_tree(
    parent: RenderParentHandle,
    backup_root: Path,
    output_root: Path,
    expected_output_state: RenderOutputState,
) -> None:
    """Restore the exact accepted prior tree without replacing new state."""

    _verify_output_state_at(
        parent,
        backup_root.name,
        backup_root,
        expected_output_state,
    )
    if _entry_identity_at(parent, output_root.name) is not None:
        raise RenderTransactionError(
            "the output path gained concurrent state before rollback"
        )
    try:
        _rename_render_entry(
            parent,
            backup_root.name,
            output_root.name,
        )
    except BaseException:
        if _state_root_identity(
            expected_output_state
        ) != _entry_identity_at(
            parent,
            output_root.name,
        ):
            raise
    _verify_output_state_at(
        parent,
        output_root.name,
        output_root,
        expected_output_state,
    )


def _replace_rendered_tree(
    parent: RenderParentHandle,
    staged_root: Path,
    output_root: Path,
    expected_output_state: RenderOutputState,
    expected_staged_state: RenderOutputState,
    validate_staged_state: Callable[[RenderOutputState], None],
    verify_current_inputs: Callable[[], None],
) -> None:
    """Replace output_root and restore its prior state if the swap fails."""

    _assert_render_parent_current(parent)
    verify_output_root_state(output_root, expected_output_state)
    _verify_output_state_at(
        parent,
        staged_root.name,
        staged_root,
        expected_staged_state,
    )
    validate_staged_state(expected_staged_state)
    _verify_output_state_at(
        parent,
        staged_root.name,
        staged_root,
        expected_staged_state,
    )
    _assert_render_parent_current(parent)

    backup_root: Path | None = None
    installed_identity: tuple[int, int] | None = None
    install_reported_success = False
    prior_identity = _state_root_identity(expected_output_state)
    if expected_output_state.exists:
        backup_root = parent.path / (
            f".{output_root.name}.backup-{uuid.uuid4().hex}"
        )

    try:
        _assert_render_parent_current(parent)
        verify_current_inputs()
        _assert_render_parent_current(parent)
        _assert_no_tracked_canonical_output_paths(output_root)
        if backup_root is not None:
            _rename_render_entry(
                parent,
                output_root.name,
                backup_root.name,
            )
            _verify_output_state_at(
                parent,
                backup_root.name,
                backup_root,
                expected_output_state,
            )

        _assert_render_parent_current(parent)
        _verify_output_state_at(
            parent,
            staged_root.name,
            staged_root,
            expected_staged_state,
        )
        validate_staged_state(expected_staged_state)
        _verify_output_state_at(
            parent,
            staged_root.name,
            staged_root,
            expected_staged_state,
        )
        verify_current_inputs()
        _assert_render_parent_current(parent)
        _rename_render_entry(
            parent,
            staged_root.name,
            output_root.name,
        )
        install_reported_success = True
        installed_identity = _entry_identity_at(parent, output_root.name)
        if installed_identity is None:
            raise RenderTransactionError(
                "the staged tree disappeared immediately after placement"
            )
        _verify_output_state_at(
            parent,
            output_root.name,
            output_root,
            expected_staged_state,
        )
        _assert_render_parent_current(parent)
        validate_staged_state(expected_staged_state)
        _verify_output_state_at(
            parent,
            output_root.name,
            output_root,
            expected_staged_state,
        )
        verify_current_inputs()
        _assert_render_parent_current(parent)
    except BaseException as transaction_error:
        recovery_root: Path | None = None
        prior_restored = not expected_output_state.exists
        recovery_identity = installed_identity
        recovery_setup_errors: list[BaseException] = []
        preserve_error: BaseException | None = None
        identity_unknown = object()
        try:
            observed_output_identity: tuple[int, int] | None | object = (
                _entry_identity_at(parent, output_root.name)
            )
        except BaseException as setup_error:
            recovery_setup_errors.append(setup_error)
            observed_output_identity = identity_unknown
        if (
            recovery_identity is None
            and observed_output_identity
            == _state_root_identity(expected_staged_state)
        ):
            recovery_identity = _state_root_identity(expected_staged_state)
        if recovery_identity is not None:
            try:
                recovery_root = _preserve_installed_entry(
                    parent,
                    output_root,
                    recovery_identity,
                )
            except BaseException as caught_preserve_error:
                preserve_error = caught_preserve_error
        elif (
            observed_output_identity is not identity_unknown
            and observed_output_identity is not None
        ):
            if (
                not install_reported_success
                and observed_output_identity
                == _state_root_identity(expected_output_state)
            ):
                raise
            backup_message = (
                " The retained accepted prior-tree location is "
                f"{_retained_child_location(parent, prior_identity)}."
                if prior_identity is not None
                else ""
            )
            raise RenderTransactionError(
                "Rendered-tree replacement failed and the output path gained "
                f"concurrent state; that state was preserved.{backup_message}"
            ) from transaction_error

        if prior_identity is not None and not prior_restored:
            try:
                prior_restored = (
                    _entry_identity_at(parent, output_root.name)
                    == prior_identity
                )
            except BaseException as setup_error:
                recovery_setup_errors.append(setup_error)

        if (
            backup_root is not None
            and prior_identity is not None
            and not prior_restored
        ):
            backup_identity_unknown = False
            try:
                observed_backup_identity = _entry_identity_at(
                    parent,
                    backup_root.name,
                )
            except BaseException as setup_error:
                recovery_setup_errors.append(setup_error)
                observed_backup_identity = None
                backup_identity_unknown = True
            if (
                observed_backup_identity == prior_identity
                or backup_identity_unknown
            ):
                try:
                    _restore_prior_tree(
                        parent,
                        backup_root,
                        output_root,
                        expected_output_state,
                    )
                    prior_restored = True
                except BaseException as restore_error:
                    retained_location = _retained_child_location(
                        parent,
                        prior_identity,
                    )
                    rejected_location = (
                        _retained_child_location(
                            parent,
                            recovery_identity,
                        )
                        if recovery_root is not None
                        else None
                    )
                    rejected_message = (
                        "; rejected tree location: "
                        f"{rejected_location}"
                        if rejected_location is not None
                        else ""
                    )
                    setup_message = (
                        "; rollback setup also encountered "
                        + ", ".join(
                            type(error).__name__
                            for error in recovery_setup_errors
                        )
                        if recovery_setup_errors
                        else ""
                    )
                    raise RenderTransactionError(
                        "Rendered-tree replacement failed and the prior tree "
                        "could not be restored; it remains at retained "
                        "prior-tree location: "
                        f"{retained_location}{rejected_message}"
                        f"{setup_message}: "
                        f"{restore_error}"
                    ) from transaction_error
            else:
                retained_location = _retained_child_location(
                    parent,
                    prior_identity,
                )
                rejected_location = (
                    _retained_child_location(
                        parent,
                        recovery_identity,
                    )
                    if recovery_root is not None
                    else None
                )
                rejected_message = (
                    "; rejected tree location: "
                    f"{rejected_location}"
                    if rejected_location is not None
                    else ""
                )
                raise RenderTransactionError(
                    "Rendered-tree replacement failed and the prior tree could "
                    "not be restored because the expected backup name no "
                    "longer identifies the accepted identity; it remains at "
                    "retained prior-tree location: "
                    f"{retained_location}{rejected_message}"
                ) from transaction_error

        if recovery_root is not None:
            prior_status = (
                "the accepted prior tree was restored"
                if expected_output_state.exists and prior_restored
                else (
                    "the previously absent output path was restored"
                    if not expected_output_state.exists and prior_restored
                    else "the prior output state was not restored"
                )
            )
            raise RenderTransactionError(
                "The placed staged tree failed identity or content validation; "
                f"{prior_status}, and the rejected tree remains at "
                f"{_retained_child_location(parent, recovery_identity)}"
            ) from transaction_error
        if preserve_error is not None:
            backup_message = (
                " Retained accepted prior-tree location: "
                f"{_retained_child_location(parent, prior_identity)}."
                if prior_identity is not None
                else ""
            )
            rejected_message = (
                " Rejected tree location: "
                f"{_retained_child_location(parent, recovery_identity)}."
                if recovery_identity is not None
                else ""
            )
            raise RenderTransactionError(
                "Rendered-tree replacement failed after placing an entry at "
                "the output path, and preservation of that entry could not be "
                f"confirmed.{backup_message}{rejected_message} "
                f"{preserve_error}"
            ) from transaction_error
        if recovery_setup_errors:
            try:
                transaction_error.add_note(
                    "render rollback setup also encountered: "
                    + ", ".join(
                        type(error).__name__
                        for error in recovery_setup_errors
                    )
                )
            except BaseException:
                pass
        raise
    else:
        if backup_root is not None:
            try:
                _retain_verified_backup(
                    backup_root,
                    expected_output_state,
                    parent,
                )
                print(
                    "NOTICE: prior rendered tree retained for explicit cleanup "
                    "at: "
                    f"{_retained_child_location(parent, prior_identity)}"
                )
            except BaseException as cleanup_error:
                try:
                    print(
                        "WARNING: rendered tree was committed, but the "
                        "prior-tree backup could not be verified for retention: "
                        f"{_retained_child_location(parent, prior_identity)}: "
                        f"{cleanup_error}"
                    )
                except BaseException:
                    pass
                if not isinstance(
                    cleanup_error,
                    (OSError, RenderTransactionError),
                ):
                    raise
        _assert_render_parent_current(parent)


def render_all(
    output_root: Path = BUILD_ROOT,
    content_root: Path = CONTENT_ROOT,
    config_path: Path | None = None,
) -> None:
    """Render, validate, and transactionally replace a complete skill tree."""

    output_root = _resolved_output_root(output_root)
    parent_anchor = _anchor_render_parent(output_root)
    with _descriptor_scope(
        [parent_anchor.descriptor],
        "render-output ancestor",
    ):
        revalidated_output_root = _resolved_output_root(output_root)
        if revalidated_output_root != output_root:
            raise RenderTransactionError(
                "render output location changed during destination validation"
            )
        _assert_render_parent_anchor_current(parent_anchor)
        effective_config_path = (
            Path(config_path) if config_path is not None else SKILL_CONFIG_PATH
        )
        _validate_renderer_source_separation(
            output_root,
            Path(content_root),
            effective_config_path,
        )
        _assert_no_tracked_canonical_output_paths(output_root)
        render_inputs = capture_render_inputs(
            effective_config_path,
            Path(content_root),
        )
        config = render_inputs.config
        validate_render_inputs(config, render_inputs.content_entries)
        grouped, by_slug = prepare_catalog(
            config,
            render_inputs.content_entries,
        )
        aliases_by_skill = prepared_route_aliases(config, by_slug)
        verify_render_inputs(render_inputs)
        parent = _materialize_render_parent(parent_anchor)
        with _descriptor_scope(
            [parent.descriptor],
            "render-output parent",
        ):
            _assert_render_parent_current(parent)
            expected_output_state = capture_output_root_state(output_root)
            staged_root, staged_metadata = _create_staged_root_at(
                parent,
                output_root.name,
            )
            staged_identity = (
                staged_metadata.st_dev,
                staged_metadata.st_ino,
            )
            expected_staged_state: RenderOutputState | None = None
            primary_error: BaseException | None = None
            primary_traceback = None
            try:
                _assert_render_parent_current(parent)
                _assert_staged_root_identity(
                    parent,
                    staged_root,
                    staged_identity,
                )
                _render_tree(
                    staged_root,
                    config,
                    grouped,
                    aliases_by_skill,
                    render_inputs.template_files,
                    render_inputs.lock_files,
                    parent,
                    staged_identity,
                )
                _assert_render_parent_current(parent)
                _assert_staged_root_identity(
                    parent,
                    staged_root,
                    staged_identity,
                )
                expected_staged_state = _capture_output_state_at(
                    parent,
                    staged_root.name,
                    staged_root,
                    capture_file_paths=frozenset(
                        f"{skill['folder']}/agents/openai.yaml"
                        for skill in config["skills"].values()
                    ),
                )
                if (
                    expected_staged_state.device,
                    expected_staged_state.inode,
                ) != staged_identity:
                    raise RenderTransactionError(
                        "render staging root changed during capture"
                    )
                validate_rendered_state(
                    expected_staged_state,
                    config,
                    grouped,
                    aliases_by_skill,
                )
                _verify_output_state_at(
                    parent,
                    staged_root.name,
                    staged_root,
                    expected_staged_state,
                )
                verify_render_inputs(render_inputs)
                _assert_render_parent_current(parent)

                def validate_current_staged_state(
                    candidate: RenderOutputState,
                ) -> None:
                    validate_rendered_state(
                        candidate,
                        config,
                        grouped,
                        aliases_by_skill,
                    )

                _replace_rendered_tree(
                    parent,
                    staged_root,
                    output_root,
                    expected_output_state,
                    expected_staged_state,
                    validate_current_staged_state,
                    lambda: verify_render_inputs(render_inputs),
                )
            except BaseException as error:
                primary_error = error
                primary_traceback = error.__traceback__

            retention_error: BaseException | None = None
            try:
                if expected_staged_state is None:
                    retained_location = _retained_child_location(
                        parent,
                        staged_identity,
                    )
                    print(
                        "WARNING: staged render cleanup was skipped because "
                        "no trusted entry manifest was captured; retained stage "
                        f"location: {retained_location}"
                    )
                else:
                    _retain_staged_root(
                        staged_root,
                        staged_metadata.st_dev,
                        staged_metadata.st_ino,
                        expected_staged_state.entries,
                        expected_staged_state.mode,
                        parent,
                        output_root,
                    )
            except BaseException as error:
                retention_error = error

            if primary_error is not None:
                if retention_error is not None:
                    try:
                        print(
                            "WARNING: staged render retention also encountered "
                            f"{type(retention_error).__name__}; preserving the "
                            "primary render failure. Retained stage location: "
                            f"{_retained_child_location(parent, staged_identity)}"
                        )
                    except BaseException:
                        pass
                raise primary_error.with_traceback(primary_traceback)
            if retention_error is not None:
                raise retention_error
            _assert_render_parent_current(parent)

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
