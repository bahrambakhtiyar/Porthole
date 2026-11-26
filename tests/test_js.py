import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
JS_TESTS = sorted(str(p) for p in (Path(__file__).resolve().parent / "js").glob("*.test.mjs"))


@unittest.skipUnless(NODE, "node not installed (only needed for the browser-code tests)")
class JsTest(unittest.TestCase):
    def test_browser_code_unit_tests(self):
        result = subprocess.run([NODE, "--test", *JS_TESTS], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
