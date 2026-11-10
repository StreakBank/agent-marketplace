"""The tracer is a real child process: start it, stop it, read what it printed.

Found on a device, not by a mock: stopping the tracer flushed its already closed stdin and raised.
A stand-in for adb on PATH lets the real subprocess plumbing run without a device."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

FAKE_ADB = """#!/bin/sh
# Stands in for adb. The trace command reads its config from stdin; nothing else reads stdin.
case "$*" in
  *"perfetto -c -"*) cat > "$FAKE_ADB_DIR/config.txt"; echo "trace written" ;;
esac
exit 0
"""


class TracerProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        adb = Path(self.tmp.name) / "adb"
        adb.write_text(FAKE_ADB)
        adb.chmod(adb.stat().st_mode | stat.S_IXUSR)
        self.old = {k: os.environ.get(k) for k in ("PATH", "FAKE_ADB_DIR")}
        os.environ["PATH"] = self.tmp.name + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_ADB_DIR"] = self.tmp.name

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_start_then_stop_returns_what_the_tracer_printed(self):
        tracer = u.start_tracer("<serial>", "duration_ms: 1000\n", "/data/misc/perfetto-traces/x.pftrace")
        self.assertIsNone(tracer.stdin)
        out = u.stop_tracer("<serial>", tracer)
        self.assertIn("trace written", out)
        self.assertEqual((Path(self.tmp.name) / "config.txt").read_text(), "duration_ms: 1000\n")
        self.assertEqual(tracer.returncode, 0)


if __name__ == "__main__":
    unittest.main()
