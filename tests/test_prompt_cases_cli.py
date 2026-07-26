from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_prompt_cases  # noqa: E402


class PromptCasesCliTests(unittest.TestCase):
    def test_check_accepts_current_fixture_and_rejects_drift(self) -> None:
        with TemporaryDirectory(prefix="prompt-cases-") as temp_root:
            output = Path(temp_root) / "cases.yaml"
            self.assertEqual(
                0,
                render_prompt_cases.main(["--output", str(output)]),
            )
            self.assertEqual(
                0,
                render_prompt_cases.main(["--output", str(output), "--check"]),
            )
            payload = yaml.safe_load(output.read_text(encoding="utf-8"))
            payload["cases"].pop()
            output.write_text(
                yaml.safe_dump(payload, sort_keys=False),
                encoding="utf-8",
            )

            result = render_prompt_cases.main(
                ["--output", str(output), "--check"]
            )

        self.assertEqual(1, result)


if __name__ == "__main__":
    unittest.main()
