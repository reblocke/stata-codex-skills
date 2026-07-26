from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import refresh_locks  # noqa: E402


class LockCandidatePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="lock-candidate-")
        self.root = Path(self.temporary.name) / "repo"
        self.raw = self.root / "raw"
        self.candidates = self.raw / "candidates"
        self.candidates.mkdir(parents=True)
        self.patches = ExitStack()
        self.patches.enter_context(
            patch.object(refresh_locks, "REPO_ROOT", self.root)
        )
        self.patches.enter_context(
            patch.object(refresh_locks, "RAW_ROOT", self.raw)
        )
        self.patches.enter_context(
            patch.object(
                refresh_locks,
                "CANDIDATE_ROOT",
                self.candidates,
            )
        )

    def tearDown(self) -> None:
        self.patches.close()
        self.temporary.cleanup()

    def publish(
        self,
        payload: dict | None = None,
        relative: Path = Path("upstream-lock.yaml"),
    ) -> Path:
        return refresh_locks.publish_lock_candidate(
            relative,
            payload or {"schema_version": 1, "value": "new"},
        )

    def test_success_is_deterministic_and_uses_fixed_recovery(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        public.write_text("schema_version: 1\nvalue: old\n", encoding="utf-8")
        payload = {"schema_version": 1, "value": "new", "items": ["a", "b"]}
        expected = refresh_locks.deterministic_yaml_bytes(payload)

        destination = self.publish(payload)

        self.assertEqual(public, destination)
        self.assertEqual(expected, public.read_bytes())
        recovery = self.candidates / ".upstream-lock.yaml.previous"
        self.assertTrue(recovery.is_file())
        self.assertEqual(
            b"schema_version: 1\nvalue: old\n",
            recovery.read_bytes(),
        )
        self.assertEqual(
            expected,
            refresh_locks.deterministic_yaml_bytes(payload),
        )

    def test_existing_public_name_remains_present_through_exchange(self) -> None:
        public = self.candidates / "upstream-lock.yaml"
        original = b"schema_version: 1\nvalue: old\n"
        public.write_bytes(original)
        payload = {"schema_version": 1, "value": "new"}
        expected = refresh_locks.deterministic_yaml_bytes(payload)
        real_exchange = refresh_locks.atomic_exchange_at
        real_rename = refresh_locks.atomic_rename_at_no_replace
        observations: list[tuple[str, bytes]] = []

        def observe_exchange(
            source_descriptor: int,
            source_name: str,
            destination_descriptor: int,
            destination_name: str,
            *,
            sync_directories: bool = True,
        ) -> None:
            self.assertTrue(public.is_file())
            observations.append(("before-exchange", public.read_bytes()))
            real_exchange(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
                sync_directories=sync_directories,
            )
            self.assertTrue(public.is_file())
            observations.append(("after-exchange", public.read_bytes()))

        def observe_recovery_move(
            source_descriptor: int,
            source_name: str,
            destination_descriptor: int,
            destination_name: str,
            *,
            sync_directories: bool = True,
        ) -> None:
            self.assertTrue(public.is_file())
            self.assertEqual(expected, public.read_bytes())
            real_rename(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
                sync_directories=sync_directories,
            )

        with patch.object(
            refresh_locks,
            "atomic_exchange_at",
            side_effect=observe_exchange,
        ), patch.object(
            refresh_locks,
            "atomic_rename_at_no_replace",
            side_effect=observe_recovery_move,
        ):
            self.publish(payload)

        self.assertEqual(
            [
                ("before-exchange", original),
                ("after-exchange", expected),
            ],
            observations,
        )

    def test_interrupt_after_exchange_preserves_type_and_attaches_state(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        original = b"schema_version: 1\nvalue: old\n"
        public.write_bytes(original)
        payload = {"schema_version": 1, "value": "new"}
        expected = refresh_locks.deterministic_yaml_bytes(payload)
        real_exchange = refresh_locks.atomic_exchange_at

        def interrupt_after_exchange(
            source_descriptor: int,
            source_name: str,
            destination_descriptor: int,
            destination_name: str,
            *,
            sync_directories: bool = True,
        ) -> None:
            real_exchange(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
                sync_directories=sync_directories,
            )
            raise KeyboardInterrupt("forced exchange interrupt")

        with patch.object(
            refresh_locks,
            "atomic_exchange_at",
            side_effect=interrupt_after_exchange,
        ), self.assertRaises(KeyboardInterrupt) as caught:
            self.publish(payload)

        self.assertEqual("forced exchange interrupt", str(caught.exception))
        notes = "\n".join(caught.exception.__notes__)
        self.assertIn(
            "Generated lock candidate is published at "
            "raw/candidates/upstream-lock.yaml",
            notes,
        )
        self.assertIn("Prior lock candidate survives unchanged", notes)
        self.assertIn("filesystem durability is unconfirmed", notes)
        self.assertEqual(expected, public.read_bytes())
        retained = list(
            self.candidates.glob(".upstream-lock.yaml.*.tmp")
        )
        self.assertEqual(1, len(retained))
        self.assertEqual(original, retained[0].read_bytes())

    def test_fixed_recovery_blocks_repeated_publication_without_new_output(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        original = b"schema_version: 1\nvalue: old\n"
        public.write_bytes(original)
        self.publish({"schema_version": 1, "value": "first"})
        recovery = self.candidates / ".upstream-lock.yaml.previous"
        published = public.read_bytes()

        with self.assertRaisesRegex(
            RuntimeError,
            "remove that exact entry explicitly",
        ):
            self.publish({"schema_version": 1, "value": "second"})

        self.assertEqual(published, public.read_bytes())
        self.assertEqual(original, recovery.read_bytes())
        self.assertEqual(
            [],
            list(self.candidates.glob(".upstream-lock.yaml.*.tmp")),
        )
        self.assertEqual(
            [recovery],
            list(self.candidates.glob(".upstream-lock.yaml.previous")),
        )

    def test_partial_writes_are_completed_before_publication(self) -> None:
        real_write = os.write
        calls = 0

        def partial_write(descriptor: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            return real_write(descriptor, data[:3] if calls == 1 else data)

        payload = {"schema_version": 1, "value": "partial-write"}
        with patch.object(refresh_locks.os, "write", side_effect=partial_write):
            self.publish(payload)

        self.assertGreater(calls, 1)
        self.assertEqual(
            refresh_locks.deterministic_yaml_bytes(payload),
            (self.candidates / "upstream-lock.yaml").read_bytes(),
        )

    def test_file_fsync_failure_retains_private_candidate(self) -> None:
        with patch.object(
            refresh_locks.os,
            "fsync",
            side_effect=OSError("forced fsync failure"),
        ), self.assertRaisesRegex(RuntimeError, "Generated lock candidate"):
            self.publish()

        self.assertFalse((self.candidates / "upstream-lock.yaml").exists())
        retained = list(self.candidates.glob(".upstream-lock.yaml.*.tmp"))
        self.assertEqual(1, len(retained))
        self.assertGreater(retained[0].stat().st_size, 0)

    def test_exchange_failure_reports_only_verified_surviving_entries(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        original = b"schema_version: 1\nvalue: old\n"
        public.write_bytes(original)

        with patch.object(
            refresh_locks,
            "atomic_exchange_at",
            side_effect=OSError("forced exchange failure"),
        ), self.assertRaises(RuntimeError) as caught:
            self.publish()

        self.assertEqual(original, public.read_bytes())
        self.assertEqual(
            1,
            len(list(self.candidates.glob(".upstream-lock.yaml.*.tmp"))),
        )
        self.assertFalse(
            (self.candidates / ".upstream-lock.yaml.previous").exists()
        )
        message = str(caught.exception)
        self.assertIn("Generated lock candidate survives unchanged", message)
        self.assertIn("Prior lock candidate survives unchanged", message)
        self.assertNotIn("prior candidate recovery", message.lower())

    def test_directory_fsync_failure_reports_published_and_prior_recovery(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        public.write_text("schema_version: 1\nvalue: old\n", encoding="utf-8")
        real_fsync = os.fsync
        directory_fsyncs = 0

        def fail_directory_fsync(descriptor: int) -> None:
            nonlocal directory_fsyncs
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_fsyncs += 1
                if directory_fsyncs == 1:
                    raise OSError("forced directory fsync failure")
            real_fsync(descriptor)

        with patch.object(
            refresh_locks.os,
            "fsync",
            side_effect=fail_directory_fsync,
        ), self.assertRaises(RuntimeError) as caught:
            self.publish()

        recovery = self.candidates / ".upstream-lock.yaml.previous"
        self.assertEqual(
            b"schema_version: 1\nvalue: old\n",
            recovery.read_bytes(),
        )
        self.assertIn(
            "Generated lock candidate is published at",
            str(caught.exception),
        )
        self.assertIn(
            "filesystem durability is unconfirmed",
            str(caught.exception),
        )
        self.assertIn("Prior lock candidate survives unchanged", str(caught.exception))

    def test_public_substitution_during_final_fsync_cannot_report_success(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        foreign = b"foreign: true\n"
        real_fsync = os.fsync
        substituted = False

        def substitute_public(descriptor: int) -> None:
            nonlocal substituted
            if (
                stat.S_ISDIR(os.fstat(descriptor).st_mode)
                and public.exists()
                and not substituted
            ):
                substituted = True
                public.unlink()
                public.write_bytes(foreign)
            real_fsync(descriptor)

        with patch.object(
            refresh_locks.os,
            "fsync",
            side_effect=substitute_public,
        ), self.assertRaisesRegex(RuntimeError, "Published lock candidate"):
            self.publish()

        self.assertTrue(substituted)
        self.assertEqual(foreign, public.read_bytes())

    def test_public_substitution_during_final_directory_check_is_rejected(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        foreign = b"foreign: true\n"
        real_assert = refresh_locks._assert_candidate_directory_current
        substituted = False

        def substitute_public(descriptor: int, relative: Path) -> None:
            nonlocal substituted
            real_assert(descriptor, relative)
            if public.exists() and not substituted:
                public.unlink()
                public.write_bytes(foreign)
                substituted = True

        with patch.object(
            refresh_locks,
            "_assert_candidate_directory_current",
            side_effect=substitute_public,
        ), self.assertRaisesRegex(RuntimeError, "Published lock candidate"):
            self.publish()

        self.assertTrue(substituted)
        self.assertEqual(foreign, public.read_bytes())

    def test_recovery_substitution_during_final_fsync_cannot_report_success(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        public.write_text("schema_version: 1\nvalue: old\n", encoding="utf-8")
        recovery = self.candidates / ".upstream-lock.yaml.previous"
        foreign = b"foreign: true\n"
        real_fsync = os.fsync
        substituted = False

        def substitute_recovery(descriptor: int) -> None:
            nonlocal substituted
            if (
                stat.S_ISDIR(os.fstat(descriptor).st_mode)
                and recovery.exists()
                and not substituted
            ):
                substituted = True
                recovery.unlink()
                recovery.write_bytes(foreign)
            real_fsync(descriptor)

        with patch.object(
            refresh_locks.os,
            "fsync",
            side_effect=substitute_recovery,
        ), self.assertRaisesRegex(RuntimeError, "Existing lock candidate"):
            self.publish()

        self.assertTrue(substituted)
        self.assertEqual(foreign, recovery.read_bytes())
        self.assertEqual(
            refresh_locks.deterministic_yaml_bytes(
                {"schema_version": 1, "value": "new"}
            ),
            public.read_bytes(),
        )

    def test_symlink_fifo_and_hardlink_destinations_are_rejected(self) -> None:
        victim = Path(self.temporary.name) / "victim"
        victim.write_bytes(b"do not change")
        public = self.candidates / "upstream-lock.yaml"
        cases = ("symlink", "fifo", "hardlink")
        for case in cases:
            with self.subTest(case=case):
                if os.path.lexists(public):
                    public.unlink()
                if case == "symlink":
                    public.symlink_to(victim)
                elif case == "fifo":
                    os.mkfifo(public)
                else:
                    os.link(victim, public)
                with self.assertRaises(RuntimeError):
                    self.publish()
                self.assertEqual(b"do not change", victim.read_bytes())

    def test_symlinked_ancestor_cannot_redirect_publication(self) -> None:
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        self.raw.rename(self.root / "raw-real")
        self.raw.symlink_to(external, target_is_directory=True)

        with self.assertRaises(RuntimeError):
            self.publish()

        self.assertEqual([], list(external.iterdir()))

    def test_unsafe_directory_permissions_are_rejected(self) -> None:
        self.candidates.chmod(0o777)
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "not group- or other-writable",
            ):
                self.publish()
        finally:
            self.candidates.chmod(0o700)

    def test_created_directory_descriptor_closes_when_metadata_setup_fails(
        self,
    ) -> None:
        self.candidates.rmdir()
        self.raw.rmdir()
        real_open = os.open
        opened: list[int] = []

        def record_open(*args: object, **kwargs: object) -> int:
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with patch.object(
            refresh_locks.os,
            "open",
            side_effect=record_open,
        ), patch.object(
            refresh_locks.os,
            "fchmod",
            side_effect=OSError("forced metadata failure"),
        ), self.assertRaisesRegex(OSError, "forced metadata failure"):
            refresh_locks._open_candidate_directory(Path(), create=True)

        self.assertGreaterEqual(len(opened), 2)
        for descriptor in opened:
            with self.subTest(descriptor=descriptor), self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_candidate_directory_substitution_retains_private_tree(
        self,
    ) -> None:
        displaced = self.raw / "displaced-candidates"
        real_assert = refresh_locks._assert_candidate_directory_current
        calls = 0

        def substitute(descriptor: int, relative: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.candidates.rename(displaced)
                self.candidates.mkdir()
            real_assert(descriptor, relative)

        with patch.object(
            refresh_locks,
            "_assert_candidate_directory_current",
            side_effect=substitute,
        ), self.assertRaisesRegex(RuntimeError, "directory changed"):
            self.publish()

        self.assertFalse(
            (self.candidates / "upstream-lock.yaml").exists()
        )
        self.assertEqual(
            1,
            len(list(displaced.glob(".upstream-lock.yaml.*.tmp"))),
        )

    def test_late_directory_substitution_cannot_report_public_success(
        self,
    ) -> None:
        displaced = self.raw / "late-displaced-candidates"
        real_rename = refresh_locks.atomic_rename_at_no_replace
        moved = False

        def substitute(
            source_descriptor: int,
            source_name: str,
            destination_descriptor: int,
            destination_name: str,
            *,
            sync_directories: bool = True,
        ) -> None:
            nonlocal moved
            if source_name.endswith(".tmp") and not moved:
                moved = True
                self.candidates.rename(displaced)
                self.candidates.mkdir()
            real_rename(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
                sync_directories=sync_directories,
            )

        with patch.object(
            refresh_locks,
            "atomic_rename_at_no_replace",
            side_effect=substitute,
        ), self.assertRaisesRegex(RuntimeError, "directory changed"):
            self.publish()

        self.assertTrue(moved)
        self.assertFalse(
            (self.candidates / "upstream-lock.yaml").exists()
        )
        self.assertTrue(
            (displaced / "upstream-lock.yaml").is_file()
        )

    def test_concurrent_public_substitution_is_preserved_and_rejected(
        self,
    ) -> None:
        public = self.candidates / "upstream-lock.yaml"
        public.write_text("schema_version: 1\nvalue: old\n", encoding="utf-8")
        real_exchange = refresh_locks.atomic_exchange_at
        substituted = False

        def substitute(
            source_descriptor: int,
            source_name: str,
            destination_descriptor: int,
            destination_name: str,
            *,
            sync_directories: bool = True,
        ) -> None:
            nonlocal substituted
            if destination_name == public.name and not substituted:
                substituted = True
                public.unlink()
                public.write_text("foreign: true\n", encoding="utf-8")
            real_exchange(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
                sync_directories=sync_directories,
            )

        with patch.object(
            refresh_locks,
            "atomic_exchange_at",
            side_effect=substitute,
        ), self.assertRaisesRegex(RuntimeError, "Prior lock candidate"):
            self.publish()

        self.assertTrue(substituted)
        self.assertEqual(b"foreign: true\n", public.read_bytes())

    def test_only_fixed_names_and_safe_package_slugs_are_accepted(self) -> None:
        invalid = (
            Path("../escape.yaml"),
            Path("packages/../escape.yaml"),
            Path("other/name.yaml"),
            Path("arbitrary.yaml"),
            Path("/absolute.yaml"),
        )
        for relative in invalid:
            with self.subTest(relative=relative), self.assertRaises(RuntimeError):
                self.publish(relative=relative)
        for slug in (
            "../escape",
            "a/b",
            ".hidden",
            "UPPER",
            "trailing-",
            "",
        ):
            with self.subTest(slug=slug), self.assertRaises(RuntimeError):
                refresh_locks.candidate_relative_path(
                    "packages",
                    package_slug=slug,
                )
        self.assertEqual(
            Path("packages/reghdfe.yaml"),
            refresh_locks.candidate_relative_path(
                "packages",
                package_slug="reghdfe",
            ),
        )
        self.assertEqual(
            Path("packages/foo_bar.yaml"),
            refresh_locks.candidate_relative_path(
                "packages",
                package_slug="foo_bar",
            ),
        )

    def test_package_candidate_paths_are_preflighted_before_stata_workspace(
        self,
    ) -> None:
        with patch.object(
            refresh_locks,
            "validated_installable_package_entries",
            return_value=[{"slug": "../escape"}],
        ), patch.object(
            refresh_locks.tempfile,
            "TemporaryDirectory",
        ) as temporary_directory, self.assertRaisesRegex(
            RuntimeError,
            "Unsafe package lock candidate slug",
        ):
            refresh_locks.package_lock_candidates(Path("/unused/stata"))

        temporary_directory.assert_not_called()

    def test_plugin_sdk_sources_are_validated_before_workspace_or_network(
        self,
    ) -> None:
        valid_sha = "0" * 64
        cases = (
            (
                "unsafe-filename",
                [
                    {
                        "filename": "../escape",
                        "url": "https://example.test/sdk.h",
                        "sha256": valid_sha,
                    }
                ],
                "duplicate or invalid filename",
            ),
            (
                "non-https",
                [
                    {
                        "filename": "stplugin.h",
                        "url": "http://example.test/sdk.h",
                        "sha256": valid_sha,
                    }
                ],
                "URL must use HTTPS",
            ),
            (
                "duplicate",
                [
                    {
                        "filename": "stplugin.h",
                        "url": "https://example.test/one",
                        "sha256": valid_sha,
                    },
                    {
                        "filename": "stplugin.h",
                        "url": "https://example.test/two",
                        "sha256": valid_sha,
                    },
                ],
                "duplicate or invalid filename",
            ),
            (
                "invalid-hash",
                [
                    {
                        "filename": "stplugin.h",
                        "url": "https://example.test/sdk.h",
                        "sha256": "not-a-hash",
                    }
                ],
                "invalid sha256",
            ),
        )
        for label, sources, expected in cases:
            with self.subTest(label=label), patch.object(
                refresh_locks,
                "read_yaml",
                return_value={"schema_version": 1, "sources": sources},
            ), patch.object(
                refresh_locks.tempfile,
                "TemporaryDirectory",
            ) as temporary_directory, patch.object(
                libskillpack,
                "download_binary",
            ) as download_binary, self.assertRaisesRegex(
                RuntimeError,
                expected,
            ):
                refresh_locks.plugin_sdk_candidate()
            temporary_directory.assert_not_called()
            download_binary.assert_not_called()

    def test_bounded_serialization_fails_before_directory_creation(self) -> None:
        self.candidates.rmdir()
        with patch.object(
            refresh_locks,
            "MAX_CANDIDATE_BYTES",
            16,
        ), self.assertRaisesRegex(RuntimeError, "exceeds"):
            self.publish({"schema_version": 1, "value": "too large"})

        self.assertFalse(self.candidates.exists())

    def test_successful_package_publication_creates_private_directory(
        self,
    ) -> None:
        package_root = self.candidates / "packages"
        destination = self.publish(
            {"schema_version": 1, "slug": "asdoc"},
            Path("packages/asdoc.yaml"),
        )

        self.assertEqual(package_root / "asdoc.yaml", destination)
        self.assertEqual(0, stat.S_IMODE(package_root.stat().st_mode) & 0o077)
        self.assertEqual(
            {"schema_version": 1, "slug": "asdoc"},
            yaml.safe_load(destination.read_text(encoding="utf-8")),
        )

    def test_cli_routes_all_outputs_through_safe_candidate_names(self) -> None:
        published: list[Path] = []

        def record(relative: Path, _payload: dict) -> Path:
            published.append(relative)
            return self.candidates / relative

        with patch.object(
            refresh_locks,
            "upstream_candidate",
            return_value={"schema_version": 1},
        ), patch.object(
            refresh_locks,
            "stata_help_candidate",
            return_value={"schema_version": 1},
        ), patch.object(
            refresh_locks,
            "plugin_sdk_candidate",
            return_value={"schema_version": 1},
        ), patch.object(
            refresh_locks,
            "detect_stata_binary",
            return_value=Path("/unused/stata"),
        ), patch.object(
            refresh_locks,
            "package_lock_candidates",
            return_value={"asdoc": {"schema_version": 1, "slug": "asdoc"}},
        ), patch.object(
            refresh_locks,
            "publish_lock_candidate",
            side_effect=record,
        ):
            result = refresh_locks.main(["--target", "all"])

        self.assertEqual(0, result)
        self.assertEqual(
            [
                Path("upstream-lock.yaml"),
                Path("stata-help-lock.yaml"),
                Path("plugin-sdk-lock.yaml"),
                Path("packages/asdoc.yaml"),
            ],
            published,
        )


if __name__ == "__main__":
    unittest.main()
