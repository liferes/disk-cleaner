"""Checks for the disk_cleaner. Builds a fake disk in temp, touches nothing real.

    python test_disk_cleaner.py
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import disk_cleaner


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
        self.assertEqual(disk_cleaner.classify("AppA\\Cache")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("AppC\\Code Cache")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("AppB\\GPUCache")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("AppB\\ShaderCache")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("AppB\\CacheStorage")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("AppB\\Cache_Data")[0], disk_cleaner.SAFE)

    def test_lookalike_names_are_not_safe(self):
        # Whole-name matching: these only look like caches.
        for name in ("Template", "Tempest", "MyProject\\Template", "App\\cache2", "App\\logs-old"):
            self.assertNotEqual(
                disk_cleaner.classify(name)[0], disk_cleaner.SAFE, "%s must not be called safe" % name
            )

    def test_real_data_is_not_safe(self):
        self.assertIsNone(disk_cleaner.classify("AppA\\Data")[0])
        self.assertIsNone(disk_cleaner.classify("SomeProject\\src")[0])

    def test_updater_is_never_safe(self):
        self.assertEqual(disk_cleaner.classify("AppData\\Local\\@opencodedesktop-updater")[0], disk_cleaner.ASK)
        self.assertEqual(disk_cleaner.classify("AppData\\Local\\antigravity-updater")[0], disk_cleaner.ASK)

    def test_temp_inherits_by_name(self):
        self.assertEqual(disk_cleaner.classify("Temp")[0], disk_cleaner.SAFE)
        self.assertEqual(disk_cleaner.classify("TempState")[0], disk_cleaner.SAFE)
        self.assertIsNone(disk_cleaner.classify("Local")[0])

    def test_absolute_path_does_not_leak(self):
        # A folder called Data that happens to live under Temp is still Data.
        self.assertIsNone(disk_cleaner.classify("AppA\\Data")[0])

    def test_documents_are_protected(self):
        self.assertTrue(disk_cleaner.protected("C:\\Users\\x\\Documents\\Cache"))
        self.assertTrue(disk_cleaner.protected("C:\\Users\\x\\Desktop\\cache"))
        self.assertFalse(disk_cleaner.protected("C:\\Users\\x\\AppData\\Local\\Temp"))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_agent_scratch_is_protected(self):
        # Tools keep working files here; removing it breaks a running session.
        base = os.environ.get("TEMP") or tempfile.gettempdir()
        self.assertTrue(disk_cleaner.protected(os.path.join(base, "opencode")))
        self.assertTrue(disk_cleaner.protected(os.path.join(base, "opencode", "tool-output")))


class Scanning(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="disk-cleaner-test-")
        build(self.root, SPEC)
        self.rows = []
        deadline = __import__("time").time() + 60
        for child in disk_cleaner.children(Path(self.root)):
            self.rows.append(disk_cleaner.analyse(child, deadline))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_skips_real_data(self):
        names = [os.path.basename(i["path"]) for i in disk_cleaner.plan(self.rows)]
        self.assertIn("Cache", names)
        self.assertIn("GPUCache", names)
        self.assertNotIn("Data", names)
        self.assertNotIn("Template", names)

    def test_plan_skips_protected(self):
        paths = [i["path"].lower() for i in disk_cleaner.plan(self.rows)]
        self.assertFalse(any("documents" in p for p in paths))

    def test_plan_is_smaller_than_disk(self):
        total = sum(r["size"] for r in self.rows)
        planned = sum(i["size"] for i in disk_cleaner.plan(self.rows))
        self.assertLess(planned, total)

    def test_app_with_data_is_not_fully_safe(self):
        app_a = [r for r in self.rows if r["name"] == "AppA"][0]
        self.assertEqual(app_a["verdict"], disk_cleaner.ASK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
