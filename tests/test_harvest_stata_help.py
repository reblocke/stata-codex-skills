from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import harvest_stata_help  # noqa: E402
import refresh_locks  # noqa: E402


class StataHelpHarvestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="stata-help-harvest-")
        self.temp_root = Path(self.temporary.name)
        self.root = self.temp_root / "repo"
        self.raw = self.root / "raw"
        self.candidates = self.raw / "candidates"
        self.candidates.mkdir(parents=True)
        self.stata_root = self.temp_root / "stata"
        self.help_root = self.stata_root / "ado" / "base"
        self.help_root.mkdir(parents=True)
        libskillpack.help_index.cache_clear()
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
        self.patches.enter_context(
            patch.object(
                harvest_stata_help,
                "STATA_ADO_BASE",
                self.help_root,
            )
        )
        self.patches.enter_context(
            patch.object(
                refresh_locks,
                "STATA_ADO_BASE",
                self.help_root,
            )
        )
        self.patches.enter_context(
            patch.object(libskillpack, "STATA_ROOT", self.stata_root)
        )
        self.patches.enter_context(
            patch.object(libskillpack, "STATA_ADO_BASE", self.help_root)
        )

    def tearDown(self) -> None:
        libskillpack.help_index.cache_clear()
        self.patches.close()
        self.temporary.cleanup()

    @property
    def report_path(self) -> Path:
        return self.candidates / "stata-help-candidates.yaml"

    def run_harvest(
        self,
        report: dict | None = None,
        errors: list[str] | None = None,
    ) -> int:
        payload = report or {
            "schema_version": 1,
            "entries": [
                {
                    "skill": "core",
                    "slug": "regress",
                    "resolved_sources": [
                        {
                            "path": "ado/base/r/regress.sthlp",
                            "sha256": "0" * 64,
                        }
                    ],
                }
            ],
        }
        with patch.object(
            harvest_stata_help,
            "build_candidate_report",
            return_value=(payload, errors or []),
        ):
            return harvest_stata_help.main([])

    def assert_both_help_paths_reject_source(
        self,
        source: Path,
        reset_source: Callable[[], None],
        *,
        read_side_effect_factory: (
            Callable[[], Callable[[int, int], bytes]] | None
        ) = None,
    ) -> None:
        relative = str(source.relative_to(self.stata_root))
        entry = {
            "slug": source.stem,
            "provenance": {
                "local_help_topics": [source.stem],
                "local_help_globs": [],
                "local_help_files": [relative],
                "package_only": False,
                "upstream_only": False,
            },
        }
        content_path = REPO_ROOT / "content" / "core" / f"{source.stem}.yaml"

        for target in ("harvest", "refresh"):
            with self.subTest(target=target):
                reset_source()
                libskillpack.help_index.cache_clear()
                module = (
                    harvest_stata_help
                    if target == "harvest"
                    else refresh_locks
                )
                with ExitStack() as stack:
                    if read_side_effect_factory is not None:
                        stack.enter_context(
                            patch.object(
                                libskillpack.os,
                                "read",
                                side_effect=read_side_effect_factory(),
                            )
                        )
                    stack.enter_context(
                        patch.object(
                            module,
                            "load_skill_config",
                            return_value={},
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            module,
                            "iter_content_entries",
                            return_value=[("core", content_path, entry)],
                        )
                    )
                    publisher = stack.enter_context(
                        patch.object(
                            module,
                            "publish_lock_candidate",
                        )
                    )
                    output = StringIO()
                    started = time.monotonic()
                    with redirect_stdout(output):
                        if target == "harvest":
                            result = harvest_stata_help.main([])
                        else:
                            result = refresh_locks.main(
                                ["--target", "stata-help"]
                            )
                    elapsed = time.monotonic() - started

                self.assertEqual(1, result)
                self.assertLess(elapsed, 2.0)
                self.assertIn("ERROR:", output.getvalue())
                publisher.assert_not_called()
                self.assertFalse(self.report_path.exists())

    def test_harvest_entry_records_only_resolved_paths_and_hashes(self) -> None:
        source = self.help_root / "r" / "regress.sthlp"
        source.parent.mkdir()
        source.write_text("{smcl}{title:Regress}", encoding="utf-8")
        entry = {
            "slug": "regress",
            "provenance": {
                "local_help_topics": ["regress"],
                "local_help_globs": [],
                "local_help_files": ["ado/base/r/regress.sthlp"],
                "package_only": False,
                "upstream_only": False,
            },
        }
        before = {
            path.relative_to(self.temp_root)
            for path in self.temp_root.rglob("*")
            if path.is_file()
        }

        with patch.object(
            harvest_stata_help,
            "find_help_files_exact",
            return_value=([source], []),
        ), patch.object(
            harvest_stata_help,
            "relative_to_stata",
            return_value="ado/base/r/regress.sthlp",
        ):
            report, errors = harvest_stata_help.harvest_entry(
                "core",
                REPO_ROOT / "content" / "core" / "regress.yaml",
                entry,
            )

        after = {
            path.relative_to(self.temp_root)
            for path in self.temp_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual([], errors)
        self.assertEqual(before, after)
        self.assertEqual(
            [
                {
                    "path": "ado/base/r/regress.sthlp",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            ],
            report["resolved_sources"],
        )
        self.assertNotIn("harvested", report)
        self.assertNotIn("normalized_file", str(report))
        self.assertFalse((self.raw / "stata-help" / "normalized").exists())

    def test_group_writable_regular_help_source_is_accepted(self) -> None:
        source = self.help_root / "u" / "group-writable.sthlp"
        source.parent.mkdir()
        source.write_bytes(b"legitimate help bytes")
        source.chmod(0o660)

        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            libskillpack.sha256_stata_help_source(
                source,
                help_root=self.help_root,
            ),
        )

    def test_help_source_read_is_bounded(self) -> None:
        source = self.help_root / "u" / "oversized.sthlp"
        source.parent.mkdir()
        source.write_bytes(b"1234")

        with self.assertRaisesRegex(RuntimeError, "bounded regular file"):
            libskillpack.sha256_stata_help_source(
                source,
                help_root=self.help_root,
                max_bytes=3,
            )

    def test_real_resolver_rejects_fifo_source_without_publication(self) -> None:
        source = self.help_root / "u" / "fifo.sthlp"
        source.parent.mkdir()

        def reset_source() -> None:
            if os.path.lexists(source):
                source.unlink()
            os.mkfifo(source)

        self.assert_both_help_paths_reject_source(source, reset_source)

    def test_real_resolver_rejects_outside_symlink_without_publication(
        self,
    ) -> None:
        source = self.help_root / "u" / "symlinked.sthlp"
        source.parent.mkdir()
        victim = self.temp_root / "outside.sthlp"
        victim.write_bytes(b"outside bytes")

        def reset_source() -> None:
            if os.path.lexists(source):
                source.unlink()
            source.symlink_to(victim)

        self.assert_both_help_paths_reject_source(source, reset_source)
        self.assertEqual(b"outside bytes", victim.read_bytes())

    def test_real_resolver_rejects_hardlink_source_without_publication(
        self,
    ) -> None:
        source = self.help_root / "u" / "hardlinked.sthlp"
        source.parent.mkdir()
        victim = self.temp_root / "hardlink-victim.sthlp"
        victim.write_bytes(b"shared bytes")

        def reset_source() -> None:
            if os.path.lexists(source):
                source.unlink()
            os.link(victim, source)

        self.assert_both_help_paths_reject_source(source, reset_source)
        self.assertEqual(b"shared bytes", victim.read_bytes())

    def test_real_resolver_rejects_source_mutation_without_publication(
        self,
    ) -> None:
        source = self.help_root / "u" / "mutating.sthlp"
        source.parent.mkdir()
        original = b"stable source bytes"
        real_read = os.read

        def reset_source() -> None:
            source.write_bytes(original)

        def read_side_effect_factory() -> Callable[[int, int], bytes]:
            mutated = False

            def mutate_before_read(descriptor: int, size: int) -> bytes:
                nonlocal mutated
                if not mutated:
                    mutated = True
                    with source.open("ab") as handle:
                        handle.write(b"!")
                return real_read(descriptor, size)

            return mutate_before_read

        self.assert_both_help_paths_reject_source(
            source,
            reset_source,
            read_side_effect_factory=read_side_effect_factory,
        )

    def test_report_path_cannot_be_overridden(self) -> None:
        victim = self.temp_root / "victim.yaml"
        victim.write_text("keep\n", encoding="utf-8")

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
            harvest_stata_help.main(["--report", str(victim)])

        self.assertEqual(2, caught.exception.code)
        self.assertEqual("keep\n", victim.read_text(encoding="utf-8"))
        self.assertFalse(self.report_path.exists())

    def test_unsafe_report_entries_are_rejected_without_touching_victim(
        self,
    ) -> None:
        victim = self.temp_root / "victim"
        victim.write_bytes(b"do not change")
        public = self.report_path
        for case in ("symlink", "fifo", "hardlink"):
            with self.subTest(case=case):
                if os.path.lexists(public):
                    public.unlink()
                if case == "symlink":
                    public.symlink_to(victim)
                elif case == "fifo":
                    os.mkfifo(public)
                else:
                    os.link(victim, public)

                self.assertEqual(1, self.run_harvest())
                self.assertEqual(b"do not change", victim.read_bytes())

    def test_symlinked_report_ancestor_cannot_redirect_output(self) -> None:
        external = self.temp_root / "external"
        external.mkdir()
        victim = external / "victim"
        victim.write_bytes(b"do not change")
        self.raw.rename(self.root / "raw-real")
        self.raw.symlink_to(external, target_is_directory=True)

        self.assertEqual(1, self.run_harvest())

        self.assertEqual(b"do not change", victim.read_bytes())
        self.assertFalse((external / "candidates").exists())

    def test_successful_report_is_deterministic_and_fixed(self) -> None:
        payload = {
            "schema_version": 1,
            "source_root": "/Applications/Stata/ado/base",
            "entries": [
                {
                    "skill": "core",
                    "slug": "regress",
                    "resolved_sources": [
                        {
                            "path": "ado/base/r/regress.sthlp",
                            "sha256": "a" * 64,
                        }
                    ],
                    "review_required": False,
                }
            ],
        }
        expected = refresh_locks.deterministic_yaml_bytes(payload)

        self.assertEqual(0, self.run_harvest(payload))
        first = self.report_path.read_bytes()
        self.assertEqual(expected, first)
        self.assertEqual(0, self.run_harvest(payload))
        self.assertEqual(first, self.report_path.read_bytes())
        self.assertEqual(
            payload,
            yaml.safe_load(self.report_path.read_text(encoding="utf-8")),
        )
        self.assertFalse((self.raw / "stata-help" / "normalized").exists())

    def test_reviewed_selector_errors_still_publish_and_fail(self) -> None:
        payload = {
            "schema_version": 1,
            "entries": [
                {
                    "skill": "core",
                    "slug": "missing",
                    "missing_selectors": ["missing"],
                    "resolved_sources": [],
                    "review_required": True,
                }
            ],
        }

        self.assertEqual(
            1,
            self.run_harvest(
                payload,
                ["content/core/missing.yaml: exact selector matched nothing"],
            ),
        )

        self.assertEqual(
            payload,
            yaml.safe_load(self.report_path.read_text(encoding="utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
