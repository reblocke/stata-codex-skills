from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import doctor  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_required_uv_version_must_be_one_exact_version(self) -> None:
        with TemporaryDirectory(prefix="doctor-version-") as temp_root:
            root = Path(temp_root)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                '[tool.uv]\nrequired-version = "==0.11.11"\n',
                encoding="utf-8",
            )
            with patch.object(doctor, "REPO_ROOT", root):
                self.assertEqual("0.11.11", doctor.required_uv_version())

            pyproject.write_text(
                '[tool.uv]\nrequired-version = ">=0.11"\n',
                encoding="utf-8",
            )
            with patch.object(doctor, "REPO_ROOT", root), self.assertRaisesRegex(
                ValueError,
                "one exact",
            ):
                doctor.required_uv_version()

    def test_installed_uv_version_rejects_unexpected_output(self) -> None:
        with patch.object(
            doctor.subprocess,
            "run",
            return_value=CompletedProcess(
                ["uv", "--version"],
                0,
                "uv 0.11.11 (Homebrew build)\n",
                "",
            ),
        ):
            self.assertEqual("0.11.11", doctor.installed_uv_version("uv"))

        with patch.object(
            doctor.subprocess,
            "run",
            return_value=CompletedProcess(
                ["uv", "--version"],
                0,
                "unknown\n",
                "",
            ),
        ), self.assertRaisesRegex(ValueError, "unexpected uv version output"):
            doctor.installed_uv_version("uv")

        with patch.object(
            doctor.subprocess,
            "run",
            side_effect=doctor.subprocess.TimeoutExpired(["uv"], 10),
        ), self.assertRaisesRegex(ValueError, "could not run uv --version"):
            doctor.installed_uv_version("uv")


if __name__ == "__main__":
    unittest.main()
