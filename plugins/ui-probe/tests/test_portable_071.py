"""0.7.1: what 0.7.0's tests and code assumed of the file system and the host (CI on Linux failed one).
A replaced log is told by its first bytes as well as its inode (see test_requests_070.SegmentTest); a
front's command line comes from /proc where there is one, so a container without `ps` still checks the
log's owner. No device, no simulator."""
import sys

sys.dont_write_bytecode = True

import builtins
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import httppipe as hp  # noqa: E402
import ui_probe as u  # noqa: E402

REAL_OPEN = builtins.open


def proc_open(cmdline: bytes | None):
    """open() with /proc/<pid>/cmdline answered by `cmdline` (None: as on a system without /proc)."""
    def fake(path, *a, **k):
        if str(path).startswith("/proc/"):
            if cmdline is None:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return io.BytesIO(cmdline)
        return REAL_OPEN(path, *a, **k)
    return fake


def ps(stdout="", rc=0, error=None):
    def fake(argv, *a, **k):
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")
    return fake


class ProcessCommandTest(unittest.TestCase):
    def test_proc_is_read_first(self):
        cmd = b"/usr/bin/python3\0-B\0ui_probe.py\0http-front\0--port\08081\0--log\0front.jsonl\0"
        with mock.patch("builtins.open", proc_open(cmd)), mock.patch.object(hp.subprocess, "run", ps(error=AssertionError("ps"))):
            self.assertEqual(hp.process_command(4242), "/usr/bin/python3 -B ui_probe.py http-front --port 8081 --log front.jsonl")
            self.assertEqual(hp.front_command(4242), "/usr/bin/python3 -B ui_probe.py http-front --port 8081 --log front.jsonl")

    def test_ps_where_there_is_no_proc(self):
        with mock.patch("builtins.open", proc_open(None)), \
                mock.patch.object(hp.subprocess, "run", ps("python3 tlsfront.py --log /x/l.jsonl\n")):
            self.assertEqual(hp.front_command(4242), "python3 tlsfront.py --log /x/l.jsonl")

    def test_neither_is_none(self):
        for runner in (ps(error=FileNotFoundError("ps")), ps("", rc=1), ps(""), ps("error: no such process\n", rc=1)):
            with mock.patch("builtins.open", proc_open(None)), mock.patch.object(hp.subprocess, "run", runner):
                self.assertIsNone(hp.process_command(4242))
                self.assertIsNone(hp.front_command(4242))
        with mock.patch("builtins.open", proc_open(b"")):              # a zombie's empty cmdline
            self.assertIsNone(hp.process_command(4242))

    def test_another_program_is_not_a_front(self):
        with mock.patch("builtins.open", proc_open(b"/bin/sleep\x0030\x00")):
            self.assertEqual(hp.process_command(4242), "/bin/sleep 30")
            self.assertIsNone(hp.front_command(4242))

    def test_this_process_on_this_host(self):
        # whichever way this host has: its own command line holds the interpreter it runs under
        self.assertIn("python", (hp.process_command(os.getpid()) or "").lower())


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("its first 4096 bytes", "kept in memory and never written",
                       "a different file is called replaced and only the same file grown shorter is called cut"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.7.1", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.7.1", (root / "PROVENANCE.md").read_text())
        # 0.8.0: the version moved on (test_url_080 pins it); the manifests still name the code's version
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], u.VERSION)
        self.assertIn("## 0.12.1", (root.parent.parent / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
