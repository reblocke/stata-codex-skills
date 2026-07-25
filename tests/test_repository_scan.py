from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import scan_repository  # noqa: E402


class RepositoryScanTests(unittest.TestCase):
    def test_reviewable_paths_uses_one_combined_source_inventory(
        self,
    ) -> None:
        root = Path("/repository")
        inventory = scan_repository.SourcePathInventory(
            tracked=(Path("tracked.py"), Path("shared.md")),
            untracked=(Path("untracked.yaml"), Path("shared.md")),
            untracked_gate_inputs=(Path("ignored-script.py"),),
        )
        with patch.object(
            scan_repository,
            "source_path_inventory",
            return_value=inventory,
        ) as combined:
            observed = scan_repository.reviewable_paths(root)

        self.assertEqual(
            [
                Path("ignored-script.py"),
                Path("shared.md"),
                Path("tracked.py"),
                Path("untracked.yaml"),
            ],
            observed,
        )
        combined.assert_called_once_with(root)

    def test_make_build_scans_before_lint_and_render(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        build_recipe = makefile.split("build:", 1)[1].split("\n\n", 1)[0]

        scan_position = build_recipe.index("scripts/scan_repository.py")
        lint_position = build_recipe.index("scripts/lint_skill_pack.py")
        render_position = build_recipe.index("scripts/render_skills.py")

        self.assertLess(scan_position, lint_position)
        self.assertLess(scan_position, render_position)

    def test_repository_scan_rejects_ignored_untracked_gate_input(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-ignored-input-") as temp_root:
            root = Path(temp_root)
            source = root / "scripts" / "ignored_validator.py"
            source.parent.mkdir()
            source.write_text("raise SystemExit(0)\n", encoding="utf-8")
            inventory = scan_repository.SourcePathInventory(
                tracked=(),
                untracked=(),
                untracked_gate_inputs=(
                    Path("scripts/ignored_validator.py"),
                ),
            )
            with patch.object(
                scan_repository,
                "source_path_inventory",
                return_value=inventory,
            ):
                errors, count = scan_repository.repository_scan_errors(root)

        self.assertEqual(1, count)
        self.assertTrue(
            any(
                error
                == (
                    "scripts/ignored_validator.py: untracked validation input "
                    "must be tracked"
                )
                for error in errors
            )
        )

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

    def test_forbidden_root_alias_uses_filesystem_identity(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-case-") as temp_root:
            root = Path(temp_root)
            canonical = root / "build"
            canonical.mkdir()
            alias = root / "Build"
            try:
                alias.mkdir()
            except FileExistsError:
                case_insensitive = True
            else:
                case_insensitive = False
            candidate = alias / "generated" / "source.md"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("reviewed source\n", encoding="utf-8")

            errors = scan_repository.scan_paths(
                root,
                [Path("Build/generated/source.md")],
            )

        forbidden = any("forbidden" in error for error in errors)
        self.assertEqual(case_insensitive, forbidden)

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

    def test_symlinked_file_is_rejected_without_reading_external_bytes(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root) / "repository"
            root.mkdir()
            external = Path(temp_root) / "external.md"
            external.write_text(
                "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            linked = root / "linked.md"
            linked.symlink_to(external)

            errors = scan_repository.scan_paths(root, [Path("linked.md")])

        self.assertTrue(any("symbolic links" in error for error in errors))
        self.assertFalse(any("private key" in error for error in errors))

    def test_symlinked_ancestor_is_rejected_without_traversal(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root) / "repository"
            root.mkdir()
            external = Path(temp_root) / "external"
            external.mkdir()
            (external / "source.py").write_text(
                "reviewed-looking external bytes\n",
                encoding="utf-8",
            )
            (root / "linked").symlink_to(external, target_is_directory=True)

            errors = scan_repository.scan_paths(
                root,
                [Path("linked/source.py")],
            )

        self.assertTrue(
            any("symbolic-link or non-directory ancestor" in error for error in errors)
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation requires POSIX")
    def test_special_file_is_rejected_without_opening_it(self) -> None:
        with TemporaryDirectory(prefix="repo-scan-") as temp_root:
            root = Path(temp_root)
            fifo = root / "source.py"
            os.mkfifo(fifo)

            errors = scan_repository.scan_paths(root, [Path("source.py")])

        self.assertTrue(any("special files" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
