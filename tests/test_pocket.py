"""The pocket guide math runs in the page. Node checks it."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class PocketGuideTest(unittest.TestCase):
    def test_heading_and_steps(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        completed = subprocess.run(
            [node, str(ROOT / "tests" / "test_pocket.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("pocket guide ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
