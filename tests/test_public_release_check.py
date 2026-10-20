import tempfile
import unittest
from pathlib import Path

from scripts.public_release_check import check


class PublicReleaseCheckTests(unittest.TestCase):
    def test_private_categories_are_rejected_in_public_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(
                [".planning/notes.md: private-release-category"],
                check(root, [".planning/notes.md"], public_candidate=True),
            )
            self.assertEqual(
                ["brains/precision-audit/config/models.yaml: private-release-category"],
                check(root, ["brains/precision-audit/config/models.yaml"], public_candidate=True),
            )

    def test_only_exact_reviewed_synthetic_file_is_exempt(self):
        name = "tests/test_brain_privacy.py"
        original = (Path(__file__).resolve().parents[1] / name).read_bytes()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / name
            target.parent.mkdir(parents=True)
            target.write_bytes(original)
            self.assertEqual([], check(root, [name], public_candidate=True))
            target.write_bytes(original + b"\n# Changed fixture requires new review.\n")
            findings = check(root, [name], public_candidate=True)
            self.assertIn(name + ": private-key-marker", findings)
            self.assertIn(name + ": aws-access-key", findings)

    def test_secret_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / ".env").write_text("PLACEHOLDER=1")
            self.assertIn(".env: sensitive-filename", check(p, [".env"]))

    def test_token_is_reported_without_value(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            token = "ghp_" + "A" * 36
            (p / "example.txt").write_text(token)
            findings = check(p, ["example.txt"])
            self.assertIn("example.txt: github-token", findings)
            self.assertNotIn(token, str(findings))

    def test_example_placeholder_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / ".env.example").write_text("OPENAI_API_KEY=replace-me")
            self.assertEqual([], check(p, [".env.example"]))


if __name__ == "__main__":
    unittest.main()
