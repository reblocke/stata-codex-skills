from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_skill_pack  # noqa: E402


class ValidateCoreTests(unittest.TestCase):
    def test_validate_core_stages_do_file_into_run_dir(self) -> None:
        with TemporaryDirectory(prefix="source path with spaces ") as source_root, TemporaryDirectory(
            prefix="validate-work-"
        ) as work_root:
            source_root_path = Path(source_root)
            work_root_path = Path(work_root)
            source_dir = source_root_path / "stata" / "core"
            source_dir.mkdir(parents=True)
            source_do_file = source_dir / "core_smoke.do"
            source_text = 'display "VALIDATION COMPLETE"\n'
            source_do_file.write_text(source_text, encoding="utf-8")

            run_dir = work_root_path / "core"
            log_path = run_dir / "core_smoke.log"
            captured: dict[str, Path] = {}

            def fake_run_stata_do(stata_binary: Path, do_file: Path, cwd: Path, timeout_seconds: int = 90):
                del stata_binary, timeout_seconds
                captured["do_file"] = do_file
                captured["cwd"] = cwd
                cwd.mkdir(parents=True, exist_ok=True)
                log_path.write_text("VALIDATION COMPLETE\n", encoding="utf-8")
                return CompletedProcess(["stata"], 0, "", ""), log_path

            with patch.object(validate_skill_pack, "TESTS_ROOT", source_root_path), patch.object(
                validate_skill_pack, "run_stata_do", side_effect=fake_run_stata_do
            ):
                success, log_text = validate_skill_pack.validate_core(Path("/fake/stata"), work_root_path)

            self.assertTrue(success)
            self.assertEqual("VALIDATION COMPLETE\n", log_text)
            self.assertEqual(run_dir / "core_smoke.do", captured["do_file"])
            self.assertEqual(run_dir, captured["cwd"])
            self.assertEqual(source_text, captured["do_file"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
