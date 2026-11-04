import contextlib
import io
import unittest
from unittest.mock import patch

from scripts.check_public_files import PRIVATE_CAMPAIGN_FILES, allowed, main


class PublicFileTests(unittest.TestCase):
    def test_generic_source_and_configuration_are_allowed(self):
        for name in (
            "src/devis_mngt/waves.py", "tests/test_waves.py",
            "scripts/evaluate_local_ocr.py", ".env.sample", ".gitignore",
            "pyproject.toml", "infra/USAGE.txt", ".github/workflows/checks.yml",
            "infra/main.parameters.example.json", "README.md",
        ):
            with self.subTest(name=name):
                self.assertTrue(allowed(name))

    def test_private_campaign_paths_are_rejected_even_when_staged(self):
        for name in PRIVATE_CAMPAIGN_FILES:
            with self.subTest(name=name):
                self.assertFalse(allowed(name))
                self.assertFalse(allowed("./" + name))
        with patch("sys.argv", ["check_public_files.py", "--staged"]), \
             patch("scripts.check_public_files.subprocess.check_output",
                   return_value=b"scripts/select_wave2.py\0"), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)

    def test_documents_and_paths_outside_repository_are_rejected(self):
        for name in (
            "", ".", "../source.py", "/tmp/source.py", ".env",
            "outputs/compet.xlsx", "results/selection.json",
            "logs/events.jsonl", ".devis-cache/results.json", "devis/devis.pdf",
            "infra/main.parameters.json",
        ):
            with self.subTest(name=name):
                self.assertFalse(allowed(name))

    def test_precommit_mode_includes_untracked_but_not_ignored_files(self):
        with patch("sys.argv", ["check_public_files.py", "--include-untracked"]), \
             patch("scripts.check_public_files.subprocess.check_output",
                   return_value=b"src/devis_mngt/waves.py\0tests/test_waves.py\0") as git, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        git.assert_called_once_with(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        )
