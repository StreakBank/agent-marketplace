"""The 0.3.0 additions for emulated device profiles: find.scroll (swipes during a lookup), a match
that must be on the screen (an override size honoured), and the proxy's --map-host. The lookup is
run in the host's POSIX shells against stand-ins for the dump and for input; the proxy against
servers on 127.0.0.1, with every other connection refused. No device, no other network."""
import sys

sys.dont_write_bytecode = True

import asyncio
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import shapeproxy as sp  # noqa: E402
import test_steps_020 as s020  # noqa: E402  (its shell harness is reused; its tests are not imported here)
import ui_probe as u  # noqa: E402
from test_steps_020 import FIND_RUN_SCN, PKG, LookupDevice, dump, node, reject, scenario  # noqa: E402
from test_run_and_cli import main  # noqa: E402
from test_gate import DEFAULTS, SCN as GATE_SCN, base_steps, checks, make_run, vstep  # noqa: E402

SCROLL = {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 5}


def tap(**find):
    return scenario([{"id": "open", "do": "tap", "find": {"text": "Sign up", **find}}])


# --------------------------------------------------------------------------------------------
# find.scroll: validation, timeout, duration, the script line
# --------------------------------------------------------------------------------------------

class ScrollValidationTest(unittest.TestCase):
    def test_valid(self):
        u.validate_scenario(tap(scroll=SCROLL))
        u.validate_scenario(tap(scroll={**SCROLL, "ms": 300, "settle_ms": 0, "max": 20}))
        u.validate_scenario(tap(scroll={"from": [0.9, 0.5], "to": [0.8, 0.5], "max": 1}))     # sideways, 0.1

    def test_shape(self):
        reject(self, tap(scroll=[0.5, 0.8]), "'find.scroll' must be an object")
        reject(self, tap(scroll={**SCROLL, "speed": 2}), "unknown 'find.scroll' keys ['speed']")
        for end in ("from", "to"):
            for bad in (None, [0.5], [0.5, 1.2], [-0.1, 0.5], ["0.5", 0.5], [True, 0.5]):
                sc = {**SCROLL, end: bad}
                if bad is None:
                    del sc[end]
                reject(self, tap(scroll=sc), f"'find.scroll' needs '{end}' as [x, y] between 0 and 1")

    def test_a_swipe_that_barely_moves_is_refused(self):
        for to in ([0.5, 0.8], [0.55, 0.75], [0.59, 0.71]):
            reject(self, tap(scroll={**SCROLL, "to": to}), "'find.scroll' must move at least 0.1 of the screen")

    def test_max(self):
        for bad in (None, 0, 21, -1, 1.0, True, "3"):
            sc = {**SCROLL, "max": bad}
            if bad is None:
                del sc["max"]
            reject(self, tap(scroll=sc), "'find.scroll.max' must be an integer from 1 to 20")

    def test_ms_and_settle(self):
        for bad in (0, 0.5, -1, True, "600"):
            reject(self, tap(scroll={**SCROLL, "ms": bad}), "'find.scroll.ms' must be a number >= 1")
        for bad in (-1, True, "800"):
            reject(self, tap(scroll={**SCROLL, "settle_ms": bad}), "'find.scroll.settle_ms' must be a number >= 0")

    def test_only_on_a_find(self):
        reject(self, scenario([{"id": "s", "do": "swipe", "from": [0, 0], "to": [1, 1], "scroll": SCROLL}]),
               "keys ['scroll'] do not apply to a 'swipe' step")


class ScrollTimeoutAndDurationTest(unittest.TestCase):
    def test_the_default_timeout_holds_every_swipe(self):
        scn = tap(scroll=SCROLL)
        # quiet.max_ms 15000 + 5 x (600 ms swipe + 800 ms settle + 2500 ms read)
        self.assertEqual(u.find_timeout_ms(scn, scn["steps"][0]), 15000 + 5 * 3900)
        scn = tap(scroll={**SCROLL, "max": 2, "ms": 300, "settle_ms": 200})
        self.assertEqual(u.find_timeout_ms(scn, scn["steps"][0]), 15000 + 2 * 3000)
        scn = tap(scroll=SCROLL, timeout_ms=7000)              # an explicit timeout is the bound
        self.assertEqual(u.find_timeout_ms(scn, scn["steps"][0]), 7000)
        scn = tap()
        self.assertEqual(u.find_timeout_ms(scn, scn["steps"][0]), 15000)

    def test_the_worst_case_holds_the_swipes_and_the_expected_case_counts_none(self):
        plain, scrolled = tap(), tap(scroll=SCROLL)
        self.assertEqual(u.scenario_duration_s(scrolled, worst_case=False), u.scenario_duration_s(plain, worst_case=False))
        # 19.5 s more timeout, plus one swipe that runs on past it (1.4 s): 20.9 s
        self.assertEqual(u.scenario_duration_s(scrolled) - u.scenario_duration_s(plain), 21)


class ScrollScriptTest(unittest.TestCase):
    def lines(self, scn):
        u.validate_scenario(scn)
        return u.build_device_script(scn, 1080, 2400, False, 60).splitlines()

    def test_the_scroll_reaches_the_lookup_in_ten_thousandths(self):
        lines = self.lines(tap(scroll={"from": [0.5, 0.8125], "to": [0.25, 0.3], "max": 3, "ms": 450, "settle_ms": 1200}))
        call = next(l for l in lines if l.startswith("if ! find_el"))
        self.assertIn(f"' package=\"{PKG}\"' '3 5000 8125 2500 3000 450 1.2'; then", call)
        lines = self.lines(tap())
        call = next(l for l in lines if l.startswith("if ! find_el"))
        self.assertIn("' '0 0 0 0 0 0 0'; then", call)

    def test_the_display_is_read_once_before_the_launch_when_a_step_looks_up(self):
        lines = self.lines(tap())
        self.assertEqual(sum(1 for l in lines if l.startswith("WMS=$(wm size")), 1)
        self.assertLess(lines.index("WMS=$(wm size 2>/dev/null)"), lines.index("mark launch.start"))
        self.assertIn('echo "$DS" > $D/display.txt', lines)
        self.assertFalse([l for l in self.lines(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}]))
                          if "wm size" in l])
        first = lines[4]
        for name in ("$D/vis.tmp", "$D/display.txt"):
            self.assertIn(name, first.split())


# --------------------------------------------------------------------------------------------
# the display size on the device, in the host's shells
# --------------------------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("sh"), "no sh")
class DisplaySizeInTheShellTest(unittest.TestCase):
    def size(self, shell, wm_output):
        lines = u.build_device_script(tap(), 1080, 2400, False, 60).splitlines()
        snippet = lines[lines.index("WMS=$(wm size 2>/dev/null)"):lines.index('echo "$DS" > $D/display.txt') + 1]
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp) / "bin"
            b.mkdir()
            (b / "wm").write_text("#!/bin/sh\nprintf '%s' \"$WM_OUT\"\n")
            (b / "wm").chmod(0o755)
            script = "\n".join([f"D={tmp}", f"PATH={b}:$PATH", *snippet, 'echo "W=$DW H=$DH"'])
            proc = subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=10,
                                  env={"WM_OUT": wm_output, "PATH": "/usr/bin:/bin"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip(), (Path(tmp) / "display.txt").read_text().strip()

    def test_physical_override_and_none(self):
        for shell in [s for s in ("sh", "dash", "bash") if shutil.which(s)]:
            self.assertEqual(self.size(shell, "Physical size: 1080x2400\n"), ("W=1080 H=2400", "1080 2400"), shell)
            self.assertEqual(self.size(shell, "Physical size: 1080x2400\nOverride size: 720x1600\n"),
                             ("W=720 H=1600", "720 1600"), shell)
            self.assertEqual(self.size(shell, "Error: no display\n"), ("W= H=", ""), shell)


# --------------------------------------------------------------------------------------------
# visibility and scrolling in find_el, in the host's shells
# --------------------------------------------------------------------------------------------

def at(bounds, text="Sign up"):
    return node(text, bounds=bounds)


SCROLL_ARG = "5 5000 8000 5000 3000 600 0.8"


@unittest.skipUnless(shutil.which("sh"), "no sh")
class VisibleLookupInTheShellTest(unittest.TestCase):
    setUp = s020.LookupInTheShellTest.setUp
    tearDown = s020.LookupInTheShellTest.tearDown
    look = s020.LookupInTheShellTest.look
    shells = s020.LookupInTheShellTest.shells

    def swipes(self, d):
        log = d / "input.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_a_match_off_the_screen_is_not_visible(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [], {"text": "Sign up"}, always=dump(at("[0,2500][100,2600]")))
            self.assertEqual(out, "NOT_FOUND", shell)
            self.assertEqual((rec["status"], rec["matches"], rec["visible"], rec["swipes"]), ("not_visible", 1, 0, 0), shell)
            self.assertEqual((rec["offscreen_bounds"], rec["display"]), ([0, 2500, 100, 2600], [1080, 2400]), shell)
            self.assertEqual(self.swipes(d), [], shell)

    def test_empty_bounds_and_a_centre_past_an_edge_are_not_visible(self):
        for bounds in ("[10,10][10,50]", "[10,50][40,50]", "[1070,0][1200,10]", "[0,2390][10,2420]"):
            for shell in self.shells():
                out, rec, _ = self.look(shell, [], {"text": "Sign up"}, always=dump(at(bounds)))
                self.assertEqual((out, rec["status"]), ("NOT_FOUND", "not_visible"), (shell, bounds))

    def test_a_partly_clipped_match_whose_centre_is_on_the_screen_counts(self):
        for shell in self.shells():
            out, _, _ = self.look(shell, [], {"text": "Sign up"}, always=dump(at("[-20,0][40,40]")))
            self.assertEqual(out, "FX=10 FY=20", shell)

    def test_the_override_size_decides(self):
        tree = dump(at("[0,1700][100,1800]"))
        for shell in self.shells():
            self.assertEqual(self.look(shell, [], {"text": "Sign up"}, always=tree)[0], "FX=50 FY=1750", shell)
            out, rec, _ = self.look(shell, [], {"text": "Sign up"}, always=tree, display="720 1600")
            self.assertEqual((out, rec["status"], rec["display"]), ("NOT_FOUND", "not_visible", [720, 1600]), shell)

    def test_only_matches_on_the_screen_count(self):
        tree = dump(at("[0,2500][10,2600]"), at("[0,100][10,200]"), at("[0,3000][10,3100]"), at("[0,300][10,400]"))
        for shell in self.shells():
            out, rec, _ = self.look(shell, [], {"text": "Sign up", "nth": 2}, nth=2, always=tree)
            self.assertEqual((out, rec["matches"], rec["visible"]), ("FX=5 FY=350", 4, 2), shell)
            # unique: one on the screen and one off it is a unique match
            out, _, _ = self.look(shell, [], {"text": "Sign up"}, always=dump(at("[0,2500][10,2600]"), at("[0,100][10,200]")))
            self.assertEqual(out, "FX=5 FY=150", shell)
            # two on the screen are not unique, whatever is off it: not_found, never not_visible
            out, rec, _ = self.look(shell, [], {"text": "Sign up"}, always=tree)
            self.assertEqual((out, rec["status"]), ("NOT_FOUND", "not_found"), shell)
            # nth 3 with two on the screen: the third is off it
            out, rec, _ = self.look(shell, [], {"text": "Sign up", "nth": 3}, nth=3, always=tree)
            self.assertEqual((out, rec["status"]), ("NOT_FOUND", "not_visible"), shell)

    def test_scroll_swipes_until_the_element_is_on_the_screen(self):
        off = dump(at("[0,2500][100,2600]"))
        for shell in self.shells():
            out, rec, d = self.look(shell, [off, dump(at("[0,0][1,1]", text="Other"))], {"text": "Sign up"},
                                    timeout_ms=60000, always=dump(at("[0,1000][100,1100]")), scroll=SCROLL_ARG)
            self.assertEqual(out, "FX=50 FY=1050", shell)
            # off the screen, swipe; not in the tree at all, swipe; on the screen twice: found
            self.assertEqual((rec["swipes"], rec["reads"]), (2, 4), shell)
            self.assertEqual(self.swipes(d), ["swipe 540 1920 540 720 600"] * 2, shell)

    def test_scroll_stops_at_max_and_the_lookup_ends_not_visible(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [], {"text": "Sign up"}, timeout_ms=3000,
                                    always=dump(at("[0,2500][100,2600]")), scroll="2 5000 8000 5000 3000 600 0.8")
            self.assertEqual((out, rec["status"], rec["swipes"]), ("NOT_FOUND", "not_visible", 2), shell)
            self.assertEqual(len(self.swipes(d)), 2, shell)

    def test_no_swipe_after_a_failed_read_or_while_a_match_waits_to_hold_still(self):
        tree = dump(at("[0,100][10,200]"))
        for shell in self.shells():
            out, rec, d = self.look(shell, [None, None], {"text": "Sign up"}, timeout_ms=60000, always=tree,
                                    scroll=SCROLL_ARG)
            self.assertEqual((out, rec["swipes"], rec["reads"]), ("FX=5 FY=150", 0, 4), shell)
            self.assertEqual(self.swipes(d), [], shell)

    def test_the_pixels_follow_the_display_size(self):
        for shell in self.shells():
            _, _, d = self.look(shell, [], {"text": "Sign up"}, timeout_ms=1000, always=dump(at("[0,2500][1,2600]")),
                                scroll="1 5000 8000 5000 3000 600 0.8", display="720 1600")
            self.assertEqual(self.swipes(d), ["swipe 360 1280 360 480 600"], shell)

    def test_an_unknown_display_size_stops_the_lookup_at_once(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [], {"text": "Sign up"}, always=dump(at("[0,0][10,10]")), display=" ")
            self.assertEqual((out, rec["status"], rec["reads"]), ("NOT_FOUND", "no_display_size", 0), shell)
            self.assertFalse((d / "ui-o.xml").exists(), shell)


# --------------------------------------------------------------------------------------------
# the host: the message, the record, the display size
# --------------------------------------------------------------------------------------------

class NotVisibleMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, scn, record):
        path = self.root / "scenario.json"
        path.write_text(json.dumps(scn))
        device = LookupDevice({"marks.txt": "T0 1\n", "lookup-failed.txt": "open\n", "find-open.txt": record})
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            return main("run", "--serial", "<serial>", "--scenario", str(path), "--out", str(self.root / "out"))

    RECORD = ("status not_visible\nmatches 1\nvisible 0\nreads 3\nswipes {s}\nelapsed_ms 6400\ndisplay 720 1600\n"
              "offscreen_bounds 0 2500 100 2600\n")

    def test_without_scroll_it_suggests_scroll(self):
        rc, _, err = self.run_cli(FIND_RUN_SCN, self.RECORD.format(s=0))
        self.assertEqual(rc, 2)
        self.assertIn("step 'open': the element with text \"Sign up\" was not on the screen within 6000 ms (3 reads): "
                      f"1 matched in {PKG} at the last read, 0 of them on the screen (exactly one needed): the element is "
                      "in the window tree but off the screen (bounds [0, 2500, 100, 2600] on a 720 x 1600 display); "
                      "add 'find.scroll' to swipe it into view", err)

    def test_with_scroll_it_counts_the_swipes(self):
        scn = json.loads(json.dumps(FIND_RUN_SCN))
        scn["steps"][1]["find"]["scroll"] = SCROLL
        rc, _, err = self.run_cli(scn, self.RECORD.format(s=5))
        self.assertEqual(rc, 2)
        self.assertIn("(3 reads, 5 swipes)", err)
        self.assertIn("; 5 swipes did not bring it onto the screen", err)
        self.assertNotIn("add 'find.scroll'", err)

    def test_an_unknown_display_size(self):
        rc, _, err = self.run_cli(FIND_RUN_SCN, "status no_display_size\nreads 0\nswipes 0\nelapsed_ms 0\n")
        self.assertEqual(rc, 2)
        self.assertIn("the display size could not be read on the device (`wm size`)", err)


class FindRecordTest(unittest.TestCase):
    def test_swipes_visible_and_the_display_in_run_json(self):
        text = ("status found\nmatches 2\nvisible 1\nreads 4\nswipes 3\nelapsed_ms 9000\nbounds 300 700 420 900\n"
                f"centre 360 800\npackage {PKG}\ndisplay 720 1600\n")
        step = {"id": "open", "do": "tap", "find": {"text": "Sign up", "scroll": SCROLL}}
        rec = u.find_record(text, step, {"width": 1080, "height": 2400}, None)
        self.assertEqual((rec["swipes"], rec["visible"], rec["matches"], rec["display_px"]), (3, 1, 2, [720, 1600]))
        # the fractions are of the display the lookup checked, not of the physical screen
        self.assertEqual(rec["centre"], [0.5, 0.5])

    def test_negative_bounds_parse(self):
        rec = u.parse_find("status not_visible\noffscreen_bounds -40 2500 -1 2600\ndisplay 1080 2400\n")
        self.assertEqual(rec["offscreen_bounds"], [-40, 2500, -1, 2600])


class OverrideSizeFactsTest(unittest.TestCase):
    def facts(self, wm_output):
        def fake_sh(serial, command, **kw):
            if command.startswith("wm size"):
                return wm_output
            if command.startswith("dumpsys package '"):
                return f"Package [{PKG}] (abc):\n userId=10100\n"
            return ""
        with mock.patch.object(u, "sh", fake_sh), mock.patch.object(u, "adb", lambda *a, **k: ""):
            return u.device_facts("<serial>", PKG)

    def test_the_override_size_is_the_one_taps_use(self):
        f = self.facts("Physical size: 1080x2400\nOverride size: 720x1600\n")
        self.assertEqual((f["width"], f["height"], f["physical_size"], f["override_size"]),
                         (720, 1600, [1080, 2400], [720, 1600]))
        f = self.facts("Physical size: 1080x2400\n")
        self.assertEqual((f["width"], f["height"], f["physical_size"], f["override_size"]), (1080, 2400, [1080, 2400], None))
        with self.assertRaises(u.ProbeError):
            self.facts("Error\n")


# --------------------------------------------------------------------------------------------
# proxy --map-host
# --------------------------------------------------------------------------------------------

class MapHostArgumentTest(unittest.TestCase):
    def parse(self, *values):
        argv = []
        for v in values:
            argv += ["--map-host", v]
        return sp.parse_args(argv)

    def refused(self, *values):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.parse(*values)
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_valid(self):
        self.assertEqual(self.parse("10.0.2.2=127.0.0.1").host_map, {"10.0.2.2": "127.0.0.1"})
        self.assertEqual(self.parse("Backend.Example-1.test=127.0.0.1", "[fe80::1]=::1").host_map,
                         {"backend.example-1.test": "127.0.0.1", "fe80::1": "::1"})
        self.assertEqual(sp.parse_args([]).host_map, {})

    def test_refused(self):
        for bad in ("a", "a=b=c", "=b", "a=", "a:80=b", "a=b:80", "http://a=b", "a=b/x", "a b=c", "-a=b", "a-=b",
                    "1.2.3=b", "999.1.1.1=b", "[1.2.3.4]=b", "a..b=c", "a=" + "x" * 64):
            self.assertIn("--map-host", self.refused(bad), bad)
        for bad in ("a.test=b.test=c.test", "a.test"):
            self.assertIn("--map-host takes FROM=TO (one '=')", self.refused(bad), bad)
        self.assertIn("maps 127.0.0.1 to itself", self.refused("127.0.0.1=127.0.0.1"))
        self.assertIn("maps ::1 to itself", self.refused("[::1]=::1"))
        self.assertIn("maps a.test twice", self.refused("a.test=127.0.0.1", "A.test=::1"))


class MapHostProxyTest(unittest.IsolatedAsyncioTestCase):
    """Connections from the proxy to anything but 127.0.0.1 are refused here, so a mapping that did
    not apply fails the test instead of reaching the network."""

    async def asyncSetUp(self):
        self.log = io.StringIO()
        self.servers = []
        self.dialled = []
        real = asyncio.open_connection

        async def guarded(host=None, port=None, **kw):
            self.dialled.append(host)
            if host != "127.0.0.1":
                raise OSError(f"no network in tests: {host}")
            return await real(host, port, **kw)
        self.real_open = real
        self.patch = mock.patch.object(sp.asyncio, "open_connection", guarded)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        for server in self.servers:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def proxy(self, *argv):
        server = await sp.start(sp.parse_args(["--port", "0", *argv]), self.log)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def entry(self):
        for _ in range(80):
            lines = [l for l in self.log.getvalue().splitlines() if l]
            if lines:
                return json.loads(lines[0])
            await asyncio.sleep(0.025)
        self.fail("no log line")

    async def test_a_connect_target_is_mapped(self):
        async def echo(r, w):
            w.write(await r.read(5))
            await w.drain()
            w.close()
        target = await self.serve(echo)
        port = await self.proxy("--map-host", "10.0.2.2=127.0.0.1", "--map-host", "backend.test=127.0.0.1")
        for named in ("10.0.2.2", "BACKEND.test"):
            self.log.seek(0)
            self.log.truncate()
            r, w = await self.real_open("127.0.0.1", port)
            w.write(f"CONNECT {named}:{target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await w.drain()
            self.assertTrue((await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)).startswith(b"HTTP/1.1 200"), named)
            w.write(b"hello")
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.read(5), 2), b"hello")
            w.close()
            e = await self.entry()
            self.assertEqual((e["target"], e["connected_to"], e["error"]), (f"{named}:{target}", f"127.0.0.1:{target}", None))
        self.assertNotIn("10.0.2.2", self.dialled)

    async def test_a_plain_request_is_mapped_and_passed_on_as_sent(self):
        heads = []

        async def http(r, w):
            heads.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        target = await self.serve(http)
        port = await self.proxy("--map-host", "10.0.2.2=127.0.0.1")
        r, w = await self.real_open("127.0.0.1", port)
        w.write(f"GET http://10.0.2.2:{target}/x HTTP/1.1\r\nHost: 10.0.2.2:{target}\r\n\r\n".encode())
        await w.drain()
        reply = await asyncio.wait_for(r.read(), 2)
        self.assertTrue(reply.endswith(b"ok"), reply)
        self.assertTrue(heads[0].startswith(f"GET http://10.0.2.2:{target}/x HTTP/1.1\r\nHost: 10.0.2.2:{target}\r\n".encode()))
        w.close()
        e = await self.entry()
        self.assertEqual((e["target"], e["connected_to"]), (f"10.0.2.2:{target}", f"127.0.0.1:{target}"))

    async def test_an_unmapped_target_is_connected_as_named(self):
        async def quiet(r, w):
            await r.read()
            w.close()
        target = await self.serve(quiet)
        port = await self.proxy("--map-host", "10.0.2.2=127.0.0.1")
        r, w = await self.real_open("127.0.0.1", port)
        w.write(f"CONNECT 127.0.0.1:{target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        w.close()
        e = await self.entry()
        self.assertEqual((e["target"], e["connected_to"]), (f"127.0.0.1:{target}", f"127.0.0.1:{target}"))


# --------------------------------------------------------------------------------------------
# a recorded change at rest while the app presented no frame
# --------------------------------------------------------------------------------------------

class ChangeWithoutAppFrameTest(unittest.TestCase):
    """A status-bar change makes the display compose; the recorder's re-encoding noise can then put
    a small change in the app's area. It is the app's doing only if the app presented a frame."""

    def gate(self, real=1, identical=0, scn=None, **rest_kw):
        run = make_run(base_steps(**rest_kw), [vstep("idle", identical=identical, real=real)])
        return u.gate_run(run, scn or GATE_SCN, dict(DEFAULTS))

    def test_both_counts_zero_is_a_note(self):
        res = self.gate(frames=0, frames_layers=0)
        self.assertEqual((res["verdict"], res["violations"]), ("PASS", []))
        note = next(n for n in res["notes"] if n.startswith("idle: the screen changed at rest"))
        self.assertIn("in 1 recorded frames in the app's area, but the app presented no frame by either count "
                      "(platform 0, compositor per layer 0)", note)
        self.assertIn("the change came from the recording's encoder or from a layer that is not the app's", note)
        self.assertIn("`ui-probe zoom <run-dir> <a> <b>`", note)

    def test_an_unknown_count_keeps_the_violation(self):
        for kw in ({"frames": None, "frames_layers": 0}, {"frames": 0}, {"frames": 0, "frames_layers": None}):
            res = self.gate(**kw)
            self.assertEqual(checks(res), ["rest-video"], kw)
            self.assertIn("the screen changed at rest: 1 frames in the app's area", res["violations"][0]["message"])

    def test_a_count_above_zero_keeps_the_violation(self):
        for kw in ({"frames": 2, "frames_layers": 0}, {"frames": 0, "frames_layers": 3},
                   {"frames": 0, "frames_layers": 0, "frames_compositor": 1}):
            self.assertEqual(sorted(checks(self.gate(**kw))), ["rest-frames", "rest-video"], kw)

    def test_frames_that_changed_nothing_while_the_app_presented_none_are_a_note(self):
        for kw in ({"frames": 0, "frames_layers": 0}, {"frames": 0, "frames_layers": 0, "frames_compositor": 0}):
            res = self.gate(real=0, identical=2, **kw)
            self.assertEqual((res["verdict"], res["violations"]), ("PASS", []), kw)
            note = next(n for n in res["notes"] if n.startswith("idle: 2 frames were composed at rest"))
            self.assertIn("changed nothing on screen, but the app presented no frame by any count (platform 0, compositor "
                          "per layer 0), so the display composed them for a layer that is not the app's", note)
            self.assertIn("`ui-probe zoom <run-dir> <a> <b>`", note)

    def test_one_note_covers_both_kinds(self):
        res = self.gate(real=1, identical=1, frames=0, frames_layers=0)
        self.assertEqual(res["verdict"], "PASS")
        mine = [n for n in res["notes"] if "presented no frame" in n]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].startswith("idle: 1 frames were composed at rest that changed nothing on screen and the "
                                           "screen changed in 1 recorded frames in the app's area, but the app presented no "
                                           "frame by any count"))

    def test_frames_that_changed_nothing_fail_when_a_count_is_unknown(self):
        for kw in ({"frames": None, "frames_layers": 0}, {"frames": 0}, {"frames": 0, "frames_layers": None}):
            res = self.gate(real=0, identical=2, **kw)
            self.assertEqual(checks(res), ["rest-video"], kw)
            self.assertIn("2 frames were composed at rest that changed nothing", res["violations"][0]["message"])

    def test_an_animation_nobody_can_see_still_fails_on_every_count(self):
        """Its frames are presented, so its counts are far above zero."""
        for kw in ({"frames": 30, "frames_layers": 30}, {"frames": 30, "frames_layers": 0},
                   {"frames": 0, "frames_layers": 30}, {"frames": 0, "frames_layers": 0, "frames_compositor": 30}):
            res = self.gate(real=0, identical=30, **kw)
            self.assertEqual(sorted(checks(res)), ["rest-frames", "rest-video"], kw)
            self.assertFalse(any("presented no frame" in n for n in res["notes"]), kw)

    def test_a_changed_frames_allowance_still_leaves_frames_that_changed_nothing_to_the_rule(self):
        res = self.gate(real=3, identical=2, scn=s020.allow_scn(changed_frames_max=1), frames=0, frames_layers=0)
        self.assertEqual(checks(res), ["rest-video"])
        self.assertIn("the screen changed at rest: 3 frames in the app's area, allowed 1", res["violations"][0]["message"])
        self.assertTrue(any(n.startswith("idle: 2 frames were composed at rest that changed nothing on screen, but")
                            for n in res["notes"]))

    def test_a_changed_frames_allowance_is_judged_as_before(self):
        scn = s020.allow_scn(changed_frames_max=1)
        res = self.gate(real=3, scn=scn, frames=0, frames_layers=0)
        self.assertEqual(checks(res), ["rest-video"])
        self.assertEqual((res["violations"][0]["allowed"], res["violations"][0]["limit_from"]), (1, "allowance"))
        res = self.gate(real=1, scn=scn, frames=0, frames_layers=0)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 1" in n for n in res["notes"]))
        self.assertFalse(any("presented no frame" in n for n in res["notes"]))


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = (root / "README.md").read_text()
        self.assertIn("the app presented no frame by any count", readme)
        self.assertIn("so its counts are above zero, and both the\n  frame check and this one still fail it", readme)
        self.assertIn("presented no frame", (root / "skills" / "ui-probe" / "SKILL.md").read_text())
        entry = " ".join((root / "CHANGELOG.md").read_text().split("## 0.2.0")[0].split())
        self.assertIn("the app presented no frame by any count", entry)
        self.assertIn("recorded frames that changed nothing and a recorded change in the app's area become a note", entry)
        for phrase in ('"scroll"', "--map-host 10.0.2.2=127.0.0.1", "`not_visible`", "the override\nsize when one is set",
                       "`connected_to`", "plus room for every swipe when `scroll` is given"):
            self.assertIn(phrase, readme)
        self.assertIn("## 0.3.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("find.scroll", (root / "skills" / "ui-probe" / "SKILL.md").read_text())
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], u.VERSION)
        self.assertEqual(u.VERSION, "0.3.0")


if __name__ == "__main__":
    unittest.main()
