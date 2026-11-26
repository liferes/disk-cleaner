"""Checks for the janitor. Builds a fake disk in temp, touches nothing real.

    python test_janitor.py
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import janitor


def build(root, spec):
    for rel, count in spec.items():
        d = Path(root) / rel
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (d / ("f%02d.bin" % i)).write_bytes(b"x" * 4096)


SPEC = {
    "AppA\\Cache": 12,
    "AppA\\Data": 6,
    "AppB\\GPUCache": 8,
    "AppB\\logs": 5,
    "Documents\\Cache": 7,
    "AppC\\Code Cache": 9,
    "Project\\Template": 4,
}


class Classification(unittest.TestCase):
    def test_cache_is_safe(self):
        self.assertEqual(janitor.classify("AppA\\Cache")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("AppC\\Code Cache")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("AppB\\GPUCache")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("AppB\\ShaderCache")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("AppB\\CacheStorage")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("AppB\\Cache_Data")[0], janitor.SAFE)

    def test_lookalike_names_are_not_safe(self):
        # Whole-name matching: these only look like caches.
        for name in ("Template", "Tempest", "MyProject\\Template", "App\\cache2", "App\\logs-old"):
            self.assertNotEqual(
                janitor.classify(name)[0], janitor.SAFE, "%s must not be called safe" % name
            )

    def test_real_data_is_not_safe(self):
        self.assertIsNone(janitor.classify("AppA\\Data")[0])
        self.assertIsNone(janitor.classify("SomeProject\\src")[0])

    def test_updater_is_never_safe(self):
        self.assertEqual(janitor.classify("AppData\\Local\\@opencodedesktop-updater")[0], janitor.ASK)
        self.assertEqual(janitor.classify("AppData\\Local\\antigravity-updater")[0], janitor.ASK)

    def test_temp_inherits_by_name(self):
        self.assertEqual(janitor.classify("Temp")[0], janitor.SAFE)
        self.assertEqual(janitor.classify("TempState")[0], janitor.SAFE)
        self.assertIsNone(janitor.classify("Local")[0])

    def test_absolute_path_does_not_leak(self):
        # A folder called Data that happens to live under Temp is still Data.
        self.assertIsNone(janitor.classify("AppA\\Data")[0])

    def test_documents_are_protected(self):
        self.assertTrue(janitor.protected("C:\\Users\\x\\Documents\\Cache"))
        self.assertTrue(janitor.protected("C:\\Users\\x\\Desktop\\cache"))
        self.assertFalse(janitor.protected("C:\\Users\\x\\AppData\\Local\\Temp"))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_agent_scratch_is_protected(self):
        # Tools keep working files here; removing it breaks a running session.
        base = os.environ.get("TEMP") or tempfile.gettempdir()
        self.assertTrue(janitor.protected(os.path.join(base, "opencode")))
        self.assertTrue(janitor.protected(os.path.join(base, "opencode", "tool-output")))


class Scanning(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="janitor-test-")
        build(self.root, SPEC)
        self.rows = []
        deadline = __import__("time").time() + 60
        for child in janitor.children(Path(self.root)):
            self.rows.append(janitor.analyse(child, deadline))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_skips_real_data(self):
        names = [os.path.basename(i["path"]) for i in janitor.plan(self.rows)]
        self.assertIn("Cache", names)
        self.assertIn("GPUCache", names)
        self.assertNotIn("Data", names)
        self.assertNotIn("Template", names)

    def test_plan_skips_protected(self):
        paths = [i["path"].lower() for i in janitor.plan(self.rows)]
        self.assertFalse(any("documents" in p for p in paths))

    def test_plan_is_smaller_than_disk(self):
        total = sum(r["size"] for r in self.rows)
        planned = sum(i["size"] for i in janitor.plan(self.rows))
        self.assertLess(planned, total)

    def test_app_with_data_is_not_fully_safe(self):
        app_a = [r for r in self.rows if r["name"] == "AppA"][0]
        self.assertEqual(app_a["verdict"], janitor.ASK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
