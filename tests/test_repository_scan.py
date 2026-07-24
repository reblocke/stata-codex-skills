from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scan_repository  # noqa: E402


class RepositoryScanTests(unittest.TestCase):
    def test_forbidden_stata_and_generated_artifacts_are_reported(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            artifacts = [
                Path("validator.log"),
                Path("package.mata"),
                Path("package.mlib"),
                Path("package.mo"),
                Path("plugin.o"),
                Path("plugin.obj"),
                Path("package.pkg"),
                Path("stata.toc"),
                Path("results.smcl"),
                Path("figure.gph"),
                Path("estimates.ster"),
                Path("generated.txt"),
            ]
            for relative in artifacts:
                (root / relative).write_text("output", encoding="utf-8")

            errors = scan_repository.scan_paths(root, artifacts)

        for relative in artifacts:
            with self.subTest(relative=relative):
                self.assertTrue(
                    any(
                        str(relative) in error and "forbidden" in error
                        for error in errors
                    )
                )

    def test_high_confidence_secret_patterns_are_reported(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            secret = root / "config.md"
            secret.write_text(
                "\n".join(
                    [
                        "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
                        "AS" + "IA" + "A" * 16,
                        "github_" + "pat_" + "A" * 40,
                        "sk-" + "proj-" + "A" * 24,
                    ]
                ),
                encoding="utf-8",
            )
            errors = scan_repository.scan_paths(root, [Path("config.md")])

        self.assertTrue(any("private key" in error for error in errors))
        self.assertTrue(any("AWS access key" in error for error in errors))
        self.assertTrue(any("GitHub token" in error for error in errors))
        self.assertTrue(any("OpenAI API key" in error for error in errors))

    def test_benign_extensions_under_generated_roots_are_reported(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            artifacts = [
                Path("build/README.md"),
                Path("raw/candidate.yaml"),
                Path(".venv/pyvenv.cfg"),
                Path(".cache/state.json"),
                Path(".pytest_cache/CACHEDIR.TAG"),
                Path("tests/tmp/notes.md"),
                Path(".codex/config.yaml"),
            ]
            for relative in artifacts:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("benign-looking generated content\n", encoding="utf-8")

            errors = scan_repository.scan_paths(root, artifacts)

        for relative in artifacts:
            with self.subTest(relative=relative):
                self.assertTrue(
                    any(
                        error.startswith(f"{relative}:")
                        and "forbidden" in error
                        for error in errors
                    )
                )

    def test_root_llms_text_is_the_only_allowed_text_path(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            allowed = root / "llms.txt"
            allowed.write_text("reviewed index\n", encoding="utf-8")
            nested = root / "nested" / "llms.txt"
            nested.parent.mkdir()
            nested.write_text("generated output\n", encoding="utf-8")

            errors = scan_repository.scan_paths(
                root,
                [Path("llms.txt"), Path("nested/llms.txt")],
            )

        self.assertFalse(any(error.startswith("llms.txt:") for error in errors))
        self.assertTrue(
            any(
                error.startswith("nested/llms.txt:") and "forbidden" in error
                for error in errors
            )
        )

    def test_normal_source_file_passes(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            sources = [
                Path("script.py"),
                Path("docs/build-guide.md"),
                Path("src/raw_parser.py"),
            ]
            for relative in sources:
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("reviewed source\n", encoding="utf-8")

            errors = scan_repository.scan_paths(root, sources)

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
