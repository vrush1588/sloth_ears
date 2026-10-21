import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def test_module_help_runs_from_project_root(self):
        result = subprocess.run(
            [sys.executable, "-m", "sloth_gist", "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertIn("--whisper", result.stdout)
        self.assertIn("--style", result.stdout)
        self.assertIn("--text", result.stdout)


if __name__ == "__main__":
    unittest.main()
