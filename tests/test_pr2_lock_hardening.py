from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
import refresh_locks  # noqa: E402
import render_skills  # noqa: E402


class PackageRefreshPathTests(unittest.TestCase):
    def test_invalid_content_slug_is_rejected_before_workspace_creation(
        self,
    ) -> None:
        source = REPO_ROOT / "content" / "packages" / "asdoc.yaml"
        entry = deepcopy(libskillpack.read_yaml(source))
        entry["slug"] = str(REPO_ROOT / "config" / "skills")

        with patch.object(
            refresh_locks,
            "iter_content_entries",
            return_value=[("packages", source, entry)],
        ), patch.object(
            refresh_locks.tempfile,
            "TemporaryDirectory",
        ) as temporary_directory, self.assertRaisesRegex(
            RuntimeError,
            "Package content validation failed before refresh",
        ):
            refresh_locks.package_lock_candidates(Path("/unused"))

        temporary_directory.assert_not_called()


class RenderPackageLockInputTests(unittest.TestCase):
    def copied_locks(self, root: Path) -> Path:
        lock_root = root / "locks"
        shutil.copytree(REPO_ROOT / "locks", lock_root)
        return lock_root

    def assert_render_rejected(
        self,
        lock_root: Path,
        target: Path,
        message: str,
    ) -> None:
        with patch.object(
            render_skills,
            "LOCK_ROOT",
            lock_root,
        ), self.assertRaisesRegex(ValueError, message):
            render_skills.render_all(output_root=target)

        self.assertFalse(target.exists())
        self.assertEqual(
            [],
            list(target.parent.glob(f".{target.name}.stage-*")),
        )

    def test_missing_required_package_lock_is_rejected_before_staging(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="render-package-lock-") as temp_root:
            root = Path(temp_root)
            lock_root = self.copied_locks(root)
            (lock_root / "packages" / "asdoc.yaml").unlink()

            self.assert_render_rejected(
                lock_root,
                root / "generated",
                "missing captured package locks: packages/asdoc.yaml",
            )

    def test_malformed_package_lock_is_rejected_before_staging(self) -> None:
        with TemporaryDirectory(prefix="render-package-lock-") as temp_root:
            root = Path(temp_root)
            lock_root = self.copied_locks(root)
            (lock_root / "packages" / "asdoc.yaml").write_text(
                "[:\n",
                encoding="utf-8",
            )

            self.assert_render_rejected(
                lock_root,
                root / "generated",
                "render package lock validation failed: .*invalid YAML",
            )

    def test_invalid_package_lock_schema_is_rejected_before_staging(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="render-package-lock-") as temp_root:
            root = Path(temp_root)
            lock_root = self.copied_locks(root)
            lock_path = lock_root / "packages" / "asdoc.yaml"
            package = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            package["slug"] = "wrong-slug"
            lock_path.write_text(
                yaml.safe_dump(package, sort_keys=False),
                encoding="utf-8",
            )

            self.assert_render_rejected(
                lock_root,
                root / "generated",
                "slug must match filename",
            )


if __name__ == "__main__":
    unittest.main()
