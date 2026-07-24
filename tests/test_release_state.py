from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_state  # noqa: E402


def write_complete_tree(root: Path) -> None:
    for folder in release_state.SKILL_FOLDERS:
        skill = root / folder
        (skill / "agents").mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"# {folder}\n", encoding="utf-8")
        (skill / "PROVENANCE.md").write_text("# Provenance\n", encoding="utf-8")
        (skill / "agents" / "openai.yaml").write_text(
            f"display_name: {folder}\n",
            encoding="utf-8",
        )


class ReleaseDigestTests(unittest.TestCase):
    def test_source_digest_tracks_review_and_build_inputs(self) -> None:
        with TemporaryDirectory(prefix="release-source-") as temp_root:
            root = Path(temp_root)
            source = root / "content" / "core" / "sample.yaml"
            source.parent.mkdir(parents=True)
            source.write_text("slug: sample\n", encoding="utf-8")
            (root / "Makefile").write_text("check:\n\ttrue\n", encoding="utf-8")
            first = release_state.source_digest(root)
            source.write_text("slug: changed\n", encoding="utf-8")
            second = release_state.source_digest(root)

        self.assertNotEqual(first, second)

    def test_receipt_rejects_tree_or_source_drift(self) -> None:
        with TemporaryDirectory(prefix="release-receipt-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source-a"):
                release_state.write_validation_receipt(build, receipt)
                release_state.verify_validation_receipt(build, receipt)

            (build / "stata-core" / "SKILL.md").write_text(
                "# changed\n",
                encoding="utf-8",
            )
            with patch.object(release_state, "source_digest", return_value="source-a"):
                with self.assertRaisesRegex(ValueError, "build/generated"):
                    release_state.verify_validation_receipt(build, receipt)

            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source-b"):
                with self.assertRaisesRegex(ValueError, "source state"):
                    release_state.verify_validation_receipt(build, receipt)

    def test_receipt_is_not_written_when_state_changed_during_validation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-receipt-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            expected = {
                "source_sha256": "before",
                "tree_sha256": release_state.tree_digest(build),
            }
            with patch.object(
                release_state,
                "source_digest",
                return_value="after",
            ), self.assertRaisesRegex(ValueError, "changed during validation"):
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    expected_state=expected,
                )

        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
