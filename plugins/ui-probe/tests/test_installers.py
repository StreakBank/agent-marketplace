"""The two installers, run with --apply inside a temporary directory only. No network: the trace
processor installer is given a fake `curl` that fails."""
import sys

sys.dont_write_bytecode = True

import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PINNED = {("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "x86_64"), ("Linux", "aarch64")}


def run(script, *args, env):
    return subprocess.run(["sh", str(SCRIPTS / script), *args], capture_output=True, text=True, env=env, timeout=60)


@unittest.skipUnless(shutil.which("sh") and shutil.which("python3"), "needs sh and python3")
class InstallTest(unittest.TestCase):
    """Finding 23."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # every character that broke the old launcher: a double quote, a dollar, a backquote, a
        # single quote and a space
        self.data_home = root / "da\"ta $HOME `x` it's"
        self.bin = root / "b in"
        self.env = {**os.environ, "HOME": str(root), "XDG_DATA_HOME": str(self.data_home), "PREFIX": str(self.bin),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def tearDown(self):
        self.tmp.cleanup()

    def version(self):
        out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "ui_probe.py"), "--version"],
                             capture_output=True, text=True)
        return out.stdout.split()[1]

    def test_any_character_in_the_path(self):
        proc = run("install.sh", "--apply", env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        launcher = self.bin / "ui-probe"
        out = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"ui-probe {self.version()}")

    def test_a_different_copy_of_the_same_version_is_not_replaced(self):
        data = self.data_home / "ui-probe" / self.version()
        data.mkdir(parents=True)
        for f in ("ui_probe.py", "ui_video.py", "pftrace.py", "shapeproxy.py"):
            shutil.copy(SCRIPTS / f, data / f)
        with open(data / "ui_video.py", "a") as fh:
            fh.write("# edited\n")
        proc = run("install.sh", "--apply", env=self.env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to replace a different copy", proc.stderr)
        self.assertTrue((data / "ui_video.py").read_text().endswith("# edited\n"))
        self.assertFalse((self.bin / "ui-probe").exists())
        proc = run("install.sh", "--apply", "--force", env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((data / "ui_video.py").read_bytes(), (SCRIPTS / "ui_video.py").read_bytes())

    def test_an_identical_copy_is_reinstalled_without_force(self):
        self.assertEqual(run("install.sh", "--apply", env=self.env).returncode, 0)
        self.assertEqual(run("install.sh", "--apply", env=self.env).returncode, 0)

    def test_plan_only_changes_nothing(self):
        proc = run("install.sh", env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("plan only", proc.stdout)
        self.assertFalse(self.data_home.exists())


@unittest.skipUnless(shutil.which("sh") and (platform.system(), platform.machine()) in PINNED,
                     "no pinned trace processor build for this platform")
class InstallTraceProcessorTest(unittest.TestCase):
    """Finding 24."""

    def test_a_failed_download_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fakebin"
            fake.mkdir()
            curl = fake / "curl"
            curl.write_text('#!/bin/sh\n'
                            'while [ $# -gt 0 ]; do if [ "$1" = -o ]; then echo partial > "$2"; fi; shift; done\n'
                            'exit 22\n')
            curl.chmod(0o755)
            env = {**os.environ, "HOME": str(root), "XDG_DATA_HOME": str(root / "data"), "PREFIX": str(root / "bin"),
                   "PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '')}"}
            proc = run("install-trace-processor.sh", "--apply", env=env)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("download failed", proc.stderr)
            left = list((root / "data" / "ui-probe" / "tools").iterdir())
            self.assertEqual(left, [])
            self.assertFalse((root / "bin" / "trace_processor_shell").exists())


if __name__ == "__main__":
    unittest.main()
