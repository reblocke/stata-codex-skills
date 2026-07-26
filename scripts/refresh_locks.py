#!/usr/bin/env python3
"""Generate reviewable lock candidates without mutating checked-in locks/content."""

from __future__ import annotations

from pathlib import Path
import argparse
import hashlib
import os
import plistlib
import shutil
import stat
import tempfile
import uuid

from runtime_guard import require_supported_runtime

require_supported_runtime()

import yaml

from libskillpack import (
    CONTENT_ROOT,
    LOCK_ROOT,
    RAW_ROOT,
    REPO_ROOT,
    STATA_ADO_BASE,
    STATA_ROOT,
    UPSTREAM_REPO_DIR,
    atomic_exchange_at,
    atomic_rename_at_no_replace,
    detect_stata_binary,
    ensure_dir,
    find_help_files_exact,
    is_safe_slug,
    iter_content_entries,
    load_skill_config,
    read_text,
    read_yaml,
    relative_to_stata,
    run_command,
    run_stata_do,
    sha256_file,
    sha256_stata_help_source,
    write_text,
)
from validate_skill_pack import (
    parse_stata_track,
    sanitize_diagnostics,
)


CANDIDATE_ROOT = RAW_ROOT / "candidates"
EXPECTED_UPSTREAM_COMMIT = "33a7efc85e92cd30edc7b907f1deb9d7038397bc"
TRACK_METADATA_FILES = {"stata.trk", "backup.trk"}
MAX_CANDIDATE_BYTES = 4 * 1024 * 1024
FIXED_CANDIDATE_NAMES = {
    "upstream": "upstream-lock.yaml",
    "stata-help": "stata-help-lock.yaml",
    "stata-help-harvest": "stata-help-candidates.yaml",
    "plugin-sdk": "plugin-sdk-lock.yaml",
}
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def candidate_relative_path(
    target: str,
    *,
    package_slug: str | None = None,
) -> Path:
    """Map one reviewed target to its only authorized candidate path."""

    if target == "packages":
        if (
            package_slug is None
            or not is_safe_slug(package_slug)
        ):
            raise RuntimeError("Unsafe package lock candidate slug")
        return Path("packages") / f"{package_slug}.yaml"
    if package_slug is not None or target not in FIXED_CANDIDATE_NAMES:
        raise RuntimeError("Unknown lock candidate target")
    return Path(FIXED_CANDIDATE_NAMES[target])


def deterministic_yaml_bytes(payload: dict) -> bytes:
    """Serialize one bounded review candidate deterministically."""

    rendered = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=False,
        width=100,
    ).encode("utf-8")
    if len(rendered) > MAX_CANDIDATE_BYTES:
        raise RuntimeError(
            f"Lock candidate exceeds {MAX_CANDIDATE_BYTES} bytes"
        )
    return rendered


def _validate_directory_metadata(metadata: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError(
            f"{label} must be an owned real directory that is not group- "
            "or other-writable"
        )


def _open_candidate_directory(relative_parent: Path, *, create: bool) -> int:
    """Open the exact candidate directory without following path components."""

    repository = _absolute_without_resolving(REPO_ROOT)
    raw = _absolute_without_resolving(RAW_ROOT)
    candidates = _absolute_without_resolving(CANDIDATE_ROOT)
    if raw != repository / "raw" or candidates != raw / "candidates":
        raise RuntimeError(
            "Candidate root is not the dedicated repository raw/candidates directory"
        )
    if relative_parent.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_parent.parts
    ):
        raise RuntimeError("Unsafe lock candidate parent")

    descriptors: list[int] = []
    try:
        try:
            current = os.open(repository, DIRECTORY_OPEN_FLAGS)
        except OSError as error:
            raise RuntimeError(
                "Repository root must be a real, accessible directory"
            ) from error
        descriptors.append(current)
        _validate_directory_metadata(os.fstat(current), "Repository root")
        for part in ("raw", "candidates", *relative_parent.parts):
            created = False
            try:
                next_descriptor = os.open(
                    part,
                    DIRECTORY_OPEN_FLAGS,
                    dir_fd=current,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except OSError as error:
                    raise RuntimeError(
                        "Candidate directory changed while being created"
                    ) from error
                created = True
                try:
                    os.fsync(current)
                except OSError as error:
                    raise RuntimeError(
                        "Candidate directory creation was not durable"
                    ) from error
                try:
                    next_descriptor = os.open(
                        part,
                        DIRECTORY_OPEN_FLAGS,
                        dir_fd=current,
                    )
                except OSError as error:
                    raise RuntimeError(
                        "Created candidate directory could not be opened safely"
                    ) from error
            except OSError as error:
                raise RuntimeError(
                    "Candidate directory path must contain only real directories"
                ) from error
            descriptors.append(next_descriptor)
            if created:
                os.fchmod(next_descriptor, 0o700)
                os.fsync(next_descriptor)
            _validate_directory_metadata(
                os.fstat(next_descriptor),
                f"Candidate directory component {part!r}",
            )
            current = next_descriptor
        result = descriptors.pop()
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    for descriptor in reversed(descriptors):
        os.close(descriptor)
    return result


def _assert_candidate_directory_current(
    descriptor: int,
    relative_parent: Path,
) -> None:
    observed = _open_candidate_directory(relative_parent, create=False)
    try:
        held = os.fstat(descriptor)
        current = os.fstat(observed)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("Lock candidate directory changed during publication")
    finally:
        os.close(observed)


def _file_state(
    descriptor: int,
    *,
    label: str,
    expected_size: int | None = None,
) -> tuple[int, int, int, str]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > MAX_CANDIDATE_BYTES
        or (
            expected_size is not None
            and metadata.st_size != expected_size
        )
    ):
        raise RuntimeError(
            f"{label} must be one bounded, owned regular file that is not "
            "group- or other-writable"
        )
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise RuntimeError(f"{label} changed while being read")
        digest.update(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, position, os.SEEK_SET)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        digest.hexdigest(),
    )


def _verify_named_file(
    directory_descriptor: int,
    name: str,
    owner_descriptor: int,
    expected: tuple[int, int, int, str],
    *,
    label: str,
) -> None:
    observed = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_descriptor,
    )
    try:
        owner_state = _file_state(owner_descriptor, label=label)
        observed_state = _file_state(observed, label=label)
    finally:
        os.close(observed)
    if owner_state != expected or observed_state != expected:
        raise RuntimeError(f"{label} changed during publication")


def _unique_name(
    directory_descriptor: int,
    public_name: str,
    suffix: str,
) -> str:
    for _ in range(128):
        name = f".{public_name}.{uuid.uuid4().hex}.{suffix}"
        try:
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise RuntimeError("Could not allocate a private candidate name")


def _fixed_recovery_name(public_name: str) -> str:
    return f".{public_name}.previous"


def _candidate_relative_location(relative_parent: Path, name: str) -> Path:
    if relative_parent == Path("."):
        return Path("raw") / "candidates" / name
    return Path("raw") / "candidates" / relative_parent / name


def _require_recovery_absent(
    directory_descriptor: int,
    relative_parent: Path,
    recovery_name: str,
) -> None:
    try:
        os.stat(
            recovery_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError(
            "Could not safely inspect the fixed lock candidate recovery entry"
        ) from error
    location = _candidate_relative_location(relative_parent, recovery_name)
    raise RuntimeError(
        f"Prior lock candidate recovery exists at {location}; review and "
        "remove that exact entry explicitly before refreshing again"
    )


def _named_file_matches(
    directory_descriptor: int,
    name: str,
    owner_descriptor: int,
    expected: tuple[int, int, int, str],
    *,
    label: str,
) -> bool:
    try:
        _verify_named_file(
            directory_descriptor,
            name,
            owner_descriptor,
            expected,
            label=label,
        )
    except (OSError, RuntimeError):
        return False
    return True


def _verified_surviving_name(
    directory_descriptor: int,
    names: list[str],
    owner_descriptor: int,
    expected: tuple[int, int, int, str],
    *,
    label: str,
) -> str | None:
    checked: set[str] = set()
    for name in names:
        if not name or name in checked:
            continue
        checked.add(name)
        if _named_file_matches(
            directory_descriptor,
            name,
            owner_descriptor,
            expected,
            label=label,
        ):
            return name
    return None


def _candidate_directory_is_current(
    directory_descriptor: int,
    relative_parent: Path,
) -> bool:
    try:
        _assert_candidate_directory_current(
            directory_descriptor,
            relative_parent,
        )
    except (OSError, RuntimeError):
        return False
    return True


def _surviving_entry_location(
    directory_descriptor: int,
    relative_parent: Path,
    name: str,
    *,
    directory_is_current: bool,
) -> str:
    if directory_is_current:
        return str(_candidate_relative_location(relative_parent, name))
    metadata = os.fstat(directory_descriptor)
    return (
        f"descriptor-relative basename {name!r} in the original candidate "
        f"directory (device {metadata.st_dev}, inode {metadata.st_ino})"
    )


def _survival_diagnostic(
    directory_descriptor: int,
    relative_parent: Path,
    *,
    public_name: str,
    possible_names: list[str],
    owner_descriptor: int,
    expected: tuple[int, int, int, str],
    label: str,
    directory_is_current: bool,
    durability_unconfirmed: bool,
) -> str:
    name = _verified_surviving_name(
        directory_descriptor,
        possible_names,
        owner_descriptor,
        expected,
        label=label,
    )
    if name is None:
        return f"{label} has no verified surviving directory entry"
    location = _surviving_entry_location(
        directory_descriptor,
        relative_parent,
        name,
        directory_is_current=directory_is_current,
    )
    if label == "Generated lock candidate" and name == public_name:
        if directory_is_current:
            status = f"Generated lock candidate is published at {location}"
        else:
            status = (
                "Generated lock candidate occupies the public basename at "
                f"{location}, but the canonical candidate directory changed"
            )
        if durability_unconfirmed:
            status += ", but filesystem durability is unconfirmed"
        return status
    status = f"{label} survives unchanged at {location}"
    if durability_unconfirmed:
        status += ", but filesystem durability is unconfirmed"
    return status


def publish_lock_candidate(relative_path: Path, payload: dict) -> Path:
    """Durably replace one candidate without following or overwriting unsafe paths."""

    if (
        relative_path.is_absolute()
        or len(relative_path.parts) not in {1, 2}
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or relative_path.suffix != ".yaml"
    ):
        raise RuntimeError("Unsafe lock candidate path")
    parent_relative = relative_path.parent
    if parent_relative not in {Path("."), Path("packages")}:
        raise RuntimeError("Unsafe lock candidate path")
    if parent_relative == Path("."):
        if relative_path.name not in FIXED_CANDIDATE_NAMES.values():
            raise RuntimeError("Unsafe fixed lock candidate name")
    else:
        slug = relative_path.stem
        if (
            relative_path.name != f"{slug}.yaml"
            or not is_safe_slug(slug)
        ):
            raise RuntimeError("Unsafe package lock candidate slug")

    rendered = deterministic_yaml_bytes(payload)
    directory_descriptor = _open_candidate_directory(
        Path() if parent_relative == Path(".") else parent_relative,
        create=True,
    )
    temporary_descriptor: int | None = None
    existing_descriptor: int | None = None
    temporary_name: str | None = None
    temporary_state: tuple[int, int, int, str] | None = None
    existing_state: tuple[int, int, int, str] | None = None
    recovery_name = _fixed_recovery_name(relative_path.name)
    candidate_durability_unconfirmed = False
    prior_durability_unconfirmed = False
    directory_sync_completed = False
    relative_parent = (
        Path() if parent_relative == Path(".") else parent_relative
    )
    try:
        _assert_candidate_directory_current(
            directory_descriptor,
            relative_parent,
        )
        _require_recovery_absent(
            directory_descriptor,
            relative_parent,
            recovery_name,
        )

        try:
            existing_descriptor = os.open(
                relative_path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            existing_descriptor = None
        if existing_descriptor is not None:
            existing_state = _file_state(
                existing_descriptor,
                label="Existing lock candidate",
            )

        temporary_name = _unique_name(
            directory_descriptor,
            relative_path.name,
            "tmp",
        )
        temporary_descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        candidate_durability_unconfirmed = True
        os.fchmod(temporary_descriptor, 0o600)
        offset = 0
        while offset < len(rendered):
            written = os.write(temporary_descriptor, rendered[offset:])
            if written <= 0:
                raise OSError("candidate write made no progress")
            offset += written
        temporary_state = _file_state(
            temporary_descriptor,
            label="Temporary lock candidate",
            expected_size=len(rendered),
        )
        os.fsync(temporary_descriptor)
        _verify_named_file(
            directory_descriptor,
            temporary_name,
            temporary_descriptor,
            temporary_state,
            label="Temporary lock candidate",
        )

        _assert_candidate_directory_current(
            directory_descriptor,
            relative_parent,
        )
        if existing_descriptor is None:
            atomic_rename_at_no_replace(
                directory_descriptor,
                temporary_name,
                directory_descriptor,
                relative_path.name,
                sync_directories=False,
            )
        else:
            assert existing_state is not None
            _verify_named_file(
                directory_descriptor,
                relative_path.name,
                existing_descriptor,
                existing_state,
                label="Existing lock candidate",
            )
            atomic_exchange_at(
                directory_descriptor,
                temporary_name,
                directory_descriptor,
                relative_path.name,
                sync_directories=False,
            )
            prior_durability_unconfirmed = True
            try:
                _verify_named_file(
                    directory_descriptor,
                    relative_path.name,
                    temporary_descriptor,
                    temporary_state,
                    label="Published lock candidate",
                )
                _verify_named_file(
                    directory_descriptor,
                    temporary_name,
                    existing_descriptor,
                    existing_state,
                    label="Prior lock candidate",
                )
            except (OSError, RuntimeError):
                if _named_file_matches(
                    directory_descriptor,
                    relative_path.name,
                    temporary_descriptor,
                    temporary_state,
                    label="Published lock candidate",
                ):
                    try:
                        atomic_exchange_at(
                            directory_descriptor,
                            relative_path.name,
                            directory_descriptor,
                            temporary_name,
                            sync_directories=False,
                        )
                        os.fsync(directory_descriptor)
                    except (OSError, RuntimeError):
                        pass
                    else:
                        directory_sync_completed = True
                        candidate_durability_unconfirmed = False
                        prior_durability_unconfirmed = False
                raise
            atomic_rename_at_no_replace(
                directory_descriptor,
                temporary_name,
                directory_descriptor,
                recovery_name,
                sync_directories=False,
            )
            _verify_named_file(
                directory_descriptor,
                recovery_name,
                existing_descriptor,
                existing_state,
                label="Existing lock candidate",
            )

        _verify_named_file(
            directory_descriptor,
            relative_path.name,
            temporary_descriptor,
            temporary_state,
            label="Published lock candidate",
        )
        os.fsync(directory_descriptor)
        _assert_candidate_directory_current(
            directory_descriptor,
            relative_parent,
        )
        if existing_descriptor is not None:
            assert existing_state is not None
            _verify_named_file(
                directory_descriptor,
                recovery_name,
                existing_descriptor,
                existing_state,
                label="Existing lock candidate",
            )
        _verify_named_file(
            directory_descriptor,
            relative_path.name,
            temporary_descriptor,
            temporary_state,
            label="Published lock candidate",
        )
        directory_sync_completed = True
        candidate_durability_unconfirmed = False
        prior_durability_unconfirmed = False
        return CANDIDATE_ROOT / relative_path
    except BaseException as error:
        directory_is_current = _candidate_directory_is_current(
            directory_descriptor,
            relative_parent,
        )
        if (
            not directory_sync_completed
            and existing_descriptor is not None
            and existing_state is not None
            and not _named_file_matches(
                directory_descriptor,
                relative_path.name,
                existing_descriptor,
                existing_state,
                label="Prior lock candidate",
            )
        ):
            prior_durability_unconfirmed = True
        recovery: list[str] = []
        if temporary_descriptor is not None and temporary_state is None:
            try:
                temporary_state = _file_state(
                    temporary_descriptor,
                    label="Generated lock candidate",
                )
            except (OSError, RuntimeError):
                recovery.append(
                    "Generated lock candidate state could not be fully "
                    "verified; no candidate entry was removed"
                )
        if temporary_descriptor is not None and temporary_state is not None:
            recovery.append(
                _survival_diagnostic(
                    directory_descriptor,
                    relative_parent,
                    public_name=relative_path.name,
                    possible_names=[relative_path.name, temporary_name or ""],
                    owner_descriptor=temporary_descriptor,
                    expected=temporary_state,
                    label="Generated lock candidate",
                    directory_is_current=directory_is_current,
                    durability_unconfirmed=candidate_durability_unconfirmed,
                )
            )
        if existing_descriptor is not None and existing_state is not None:
            recovery.append(
                _survival_diagnostic(
                    directory_descriptor,
                    relative_parent,
                    public_name=relative_path.name,
                    possible_names=[
                        relative_path.name,
                        temporary_name or "",
                        recovery_name,
                    ],
                    owner_descriptor=existing_descriptor,
                    expected=existing_state,
                    label="Prior lock candidate",
                    directory_is_current=directory_is_current,
                    durability_unconfirmed=prior_durability_unconfirmed,
                )
            )
        detail = "; ".join(recovery)
        if isinstance(error, Exception):
            suffix = f"; {detail}" if detail else ""
            raise RuntimeError(
                f"Could not publish lock candidate {relative_path}: "
                f"{type(error).__name__}: {error}{suffix}"
            ) from error
        note = (
            f"Lock candidate publication for {relative_path} was interrupted"
        )
        if detail:
            note += f"; {detail}"
        error.add_note(note)
        raise
    finally:
        for descriptor in (existing_descriptor, temporary_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        os.close(directory_descriptor)


def validated_installable_package_entries() -> list[dict]:
    """Validate package content before using any entry value as a path."""

    from lint_skill_pack import lint_config, lint_entry

    config = load_skill_config()
    errors = lint_config(config)
    if errors:
        raise RuntimeError(
            "Package content validation failed before refresh: "
            + "; ".join(errors)
        )

    entries: list[dict] = []
    seen_slugs: set[str] = set()
    for skill_key, path, entry in iter_content_entries(CONTENT_ROOT, config):
        if skill_key != "packages":
            continue
        errors.extend(
            lint_entry(
                skill_key,
                path,
                entry,
                config["skills"]["packages"],
            )
        )
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str):
            if slug in seen_slugs:
                errors.append(f"{path}: duplicate package slug {slug}")
            else:
                seen_slugs.add(slug)
        if (
            entry.get("validation_mode") == "stata"
            and entry.get("install_commands")
        ):
            entries.append(entry)

    if errors:
        raise RuntimeError(
            "Package content validation failed before refresh: "
            + "; ".join(errors)
        )
    return sorted(
        entries,
        key=lambda entry: (
            entry.get("order", 10**9),
            entry.get("slug", ""),
        ),
    )


def upstream_candidate() -> dict:
    result = run_command(
        ["git", "-C", str(UPSTREAM_REPO_DIR), "rev-parse", "HEAD"],
        timeout_seconds=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "Could not read raw upstream commit")
    commit = result.stdout.strip()
    config = load_skill_config()
    files: dict[str, dict] = {}
    for _, _, entry in iter_content_entries(CONTENT_ROOT, config):
        for relative in entry.get("provenance", {}).get("upstream_files", []):
            source = UPSTREAM_REPO_DIR / relative
            if not source.is_file():
                raise RuntimeError(f"Missing declared upstream file: {relative}")
            files[relative] = {"sha256": sha256_file(source)}
    return {
        "schema_version": 1,
        "repository": {
            "url": "https://github.com/dylantmoore/stata-skill.git",
            "commit": commit,
            "expected_commit": EXPECTED_UPSTREAM_COMMIT,
        },
        "files": dict(sorted(files.items())),
    }


def stata_release_identity() -> dict:
    app = STATA_ROOT / "StataBE.app"
    info_path = app / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise RuntimeError(f"Missing Stata application metadata: {info_path}")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    executable = info.get("CFBundleExecutable")
    return {
        "edition": "BE",
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "bundle_version": info.get("CFBundleShortVersionString"),
        "executable": executable,
        "platform": "macOS",
    }


def stata_help_candidate() -> dict:
    if not STATA_ADO_BASE.is_dir():
        raise RuntimeError(f"Missing local Stata help root: {STATA_ADO_BASE}")
    config = load_skill_config()
    selectors: dict[str, dict] = {}
    files: dict[str, dict] = {}
    errors: list[str] = []
    for skill_key, path, entry in iter_content_entries(CONTENT_ROOT, config):
        provenance = entry.get("provenance", {})
        declared = provenance.get("local_help_files", [])
        if not declared:
            continue
        exact = provenance.get("local_help_topics", [])
        globs = provenance.get("local_help_globs", [])
        resolved, missing = find_help_files_exact(exact, globs)
        resolved_relative = [relative_to_stata(source) for source in resolved]
        if missing:
            errors.append(f"{path}: missing selectors {missing}")
        if set(resolved_relative) != set(declared):
            errors.append(f"{path}: resolved files differ from curated declaration")
        key = f"{skill_key}/{entry['slug']}"
        selectors[key] = {
            "exact_stems": exact,
            "globs": globs,
            "files": declared,
        }
        for source in resolved:
            files[relative_to_stata(source)] = {
                "sha256": sha256_stata_help_source(
                    source,
                    help_root=STATA_ADO_BASE,
                )
            }
    if errors:
        raise RuntimeError("\n".join(errors))
    return {
        "schema_version": 1,
        "stata_release": stata_release_identity(),
        "selectors": dict(sorted(selectors.items())),
        "files": dict(sorted(files.items())),
    }


def plugin_sdk_candidate() -> dict:
    lock_path = LOCK_ROOT / "plugin-sdk.yaml"
    lock = read_yaml(lock_path)
    from lint_skill_pack import lint_plugin_sdk_lock_payload

    errors = lint_plugin_sdk_lock_payload(lock_path, lock)
    if errors:
        raise RuntimeError(
            "Plugin SDK lock validation failed before refresh: "
            + "; ".join(errors)
        )
    assert isinstance(lock, dict)
    sources = lock["sources"]
    refreshed: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="plugin-sdk-lock-") as temp_root:
        temp_path = Path(temp_root)
        from libskillpack import download_binary

        for source in sources:
            destination = temp_path / source["filename"]
            actual = download_binary(
                source["url"],
                destination,
                timeout_seconds=30,
            )
            refreshed.append(
                {
                    "filename": source["filename"],
                    "url": source["url"],
                    "sha256": actual,
                }
            )
    return {"schema_version": 1, "sources": refreshed}


def package_lock_candidates(
    stata_binary: Path,
    selected_slugs: set[str] | None = None,
) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    installable = validated_installable_package_entries()
    for entry in installable:
        candidate_relative_path(
            "packages",
            package_slug=entry["slug"],
        )
    known_slugs = {entry["slug"] for entry in installable}
    unknown = sorted((selected_slugs or set()) - known_slugs)
    if unknown:
        raise RuntimeError(f"Unknown package lock targets: {', '.join(unknown)}")
    if selected_slugs:
        installable = [
            entry for entry in installable if entry["slug"] in selected_slugs
        ]
    with tempfile.TemporaryDirectory(prefix="stata-package-lock-") as temp_root:
        work_root = Path(temp_root)
        for index, entry in enumerate(installable, start=1):
            install_commands = entry["install_commands"]
            slug = entry["slug"]
            print(
                f"Refreshing package lock {index}/{len(installable)}: {slug}",
                flush=True,
            )
            run_dir = ensure_dir(work_root / slug)
            plus_dir = ensure_dir(run_dir / "plus")
            ensure_dir(plus_dir / "personal")
            marker = f"CODEX_LOCK_CANDIDATE::{slug.upper()}::{uuid.uuid4().hex}"
            do_file = run_dir / f"{slug}_{uuid.uuid4().hex}.do"
            do_text = "\n".join(
                [
                    "clear all",
                    "set more off",
                    f'sysdir set PLUS "{plus_dir.as_posix()}"',
                    f'sysdir set PERSONAL "{(plus_dir / "personal").as_posix()}"',
                    *install_commands,
                    f'display "{marker}"',
                    "exit, clear",
                ]
            ) + "\n"
            write_text(do_file, do_text)
            result, log_path = run_stata_do(
                stata_binary,
                do_file,
                run_dir,
                completion_marker=marker,
                timeout_seconds=180,
            )
            if result.returncode != 0:
                diagnostics = read_text(log_path) if log_path.exists() else result.stderr
                safe_diagnostics = sanitize_diagnostics(
                    diagnostics,
                    work_root=work_root,
                    max_chars=2000,
                )
                raise RuntimeError(f"{slug} install failed:\n{safe_diagnostics}")
            track_path = plus_dir / "stata.trk"
            if not track_path.is_file():
                raise RuntimeError(f"{slug}: install did not produce stata.trk")
            modules = parse_stata_track(read_text(track_path))
            distributions: list[dict] = []
            tracked_files: set[str] = set()
            for module in modules:
                file_hashes: dict[str, str] = {}
                for relative in sorted(module["files"]):
                    installed = plus_dir / relative
                    if not installed.is_file():
                        raise RuntimeError(f"{slug}: tracked file missing: {relative}")
                    file_hashes[relative] = sha256_file(installed)
                    tracked_files.add(relative)
                distributions.append(
                    {
                        "source": module["source"],
                        "descriptor": module["descriptor"],
                        "distribution_date": module["distribution_date"],
                        "files": file_hashes,
                    }
                )
            generated_files = {
                str(path.relative_to(plus_dir)): sha256_file(path)
                for path in sorted(plus_dir.rglob("*"))
                if path.is_file()
                and str(path.relative_to(plus_dir)) not in TRACK_METADATA_FILES
                and str(path.relative_to(plus_dir)) not in tracked_files
            }
            packages[slug] = {
                "schema_version": 1,
                "slug": slug,
                "distributions": distributions,
                "generated_files": generated_files,
            }
    return packages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        action="append",
        choices=("upstream", "stata-help", "plugin-sdk", "packages", "all"),
        required=True,
    )
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help="Refresh only this package lock candidate; repeat as needed.",
    )
    args = parser.parse_args(argv)
    targets = {
        item
        for target in args.target
        for item in (
            ("upstream", "stata-help", "plugin-sdk", "packages")
            if target == "all"
            else (target,)
        )
    }
    if args.packages and "packages" not in targets:
        parser.error("--package requires --target packages or --target all")
    builders = {
        "upstream": upstream_candidate,
        "stata-help": stata_help_candidate,
        "plugin-sdk": plugin_sdk_candidate,
    }
    try:
        for target in ("upstream", "stata-help", "plugin-sdk"):
            if target not in targets:
                continue
            payload = builders[target]()
            relative = candidate_relative_path(target)
            destination = publish_lock_candidate(relative, payload)
            print(f"Wrote review candidate {destination}")
        if "packages" in targets:
            stata_binary = detect_stata_binary()
            if stata_binary is None:
                raise RuntimeError("Could not locate Stata for package lock refresh")
            payloads = package_lock_candidates(
                stata_binary,
                set(args.packages) if args.packages else None,
            )
            for slug, payload in sorted(payloads.items()):
                relative = candidate_relative_path(
                    "packages",
                    package_slug=slug,
                )
                destination = publish_lock_candidate(relative, payload)
                print(f"Wrote review candidate {destination}")
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
