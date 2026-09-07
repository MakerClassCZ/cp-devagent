"""devagent end to end, without hardware: a fake board on a pty (fakeboard.py) and the agent as
a subprocess in a temp directory (so the repo's devagent.json is never touched).

    python -m unittest tests.test_agent -v          # from the repo root; needs pyserial
    python tests/test_agent.py                      # the same

What the suite checks: the token gate, file operations with verified writes, parameter
validation and the status of a failed operation, the console paths a scripted run trips over
(REPL windows, long-poll, the non-consuming tail), the registry under concurrent edits and a
drive whose UID changed, the ffmpeg capture (when ffmpeg is installed), the Python client, and
that a shutdown leaves nothing behind."""
import hashlib, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, REPO)
from fakeboard import FakeBoard, BOOT_OUT, UID      # noqa: E402

TOKEN = "t0ken"
AGENT = None          # the subprocess
BOARD = None          # the FakeBoard
PORT = None
WORK = None


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def req(method, path, body=None, token=TOKEN, timeout=30, raw=False):
    """(status, json-or-text). A non-2xx answer is returned, not raised."""
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=body, method=method)
    if token is not None:
        r.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            data, status, ctype = resp.read(), resp.status, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        data, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    if raw:
        return status, data
    if ctype.startswith("application/json"):
        return status, json.loads(data.decode("utf-8") or "null")
    return status, data.decode("utf-8", "replace")


def setUpModule():
    global AGENT, BOARD, PORT, WORK
    try:
        import serial  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("pyserial is not installed (pip install pyserial)")
    WORK = tempfile.mkdtemp(prefix="devagent-test-")
    for name in ("devagent.py", "ui.html"):
        shutil.copy(os.path.join(REPO, name), WORK)
    BOARD = FakeBoard()
    PORT = free_port()
    log = open(os.path.join(WORK, "agent.log"), "w")
    AGENT = subprocess.Popen(
        [sys.executable, os.path.join(WORK, "devagent.py"), "--port", str(PORT), "--token", TOKEN,
         "--add", "fake path=%s port=%s" % (BOARD.drive, BOARD.port)],
        stdout=log, stderr=subprocess.STDOUT, cwd=WORK)
    log.close()
    for _ in range(50):
        time.sleep(0.2)
        try:
            status, h = req("GET", "/health")
            if status == 200 and h["boards"] and h["boards"][0]["serial_open"]:
                return
        except (OSError, urllib.error.URLError):
            pass
    raise RuntimeError("the agent did not come up; see %s/agent.log" % WORK)


def tearDownModule():
    if AGENT is None:
        return
    status, j = req("POST", "/shutdown", b"")
    assert status == 200 and j.get("shutdown"), (status, j)
    try:
        AGENT.wait(timeout=6)
    except subprocess.TimeoutExpired:
        AGENT.kill()
        raise AssertionError("the agent did not exit within 6 s of POST /shutdown")
    assert AGENT.returncode == 0, "agent exit code %s (see %s/agent.log)" % (AGENT.returncode, WORK)
    shutil.rmtree(BOARD.drive, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)


class Auth(unittest.TestCase):
    def test_no_token_is_401(self):
        self.assertEqual(req("GET", "/health", token=None)[0], 401)
        self.assertEqual(req("GET", "/boards", token="wrong")[0], 401)

    def test_panel_is_public_but_data_is_not(self):
        status, page = req("GET", "/", token=None)
        self.assertEqual(status, 200); self.assertIn("<title>", page)
        self.assertEqual(req("GET", "/list?board=fake", token=None)[0], 401)

    def test_token_in_query_works(self):
        self.assertEqual(req("GET", "/health?token=" + TOKEN, token=None)[0], 200)

    def test_health_shape(self):
        status, h = req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(h["auth"], True); self.assertTrue(h["pyserial"])
        b = h["boards"][0]
        self.assertEqual((b["id"], b["uid"], b["fs_mode"]), ("fake", UID, "msc"))
        self.assertTrue(b["serial_open"]); self.assertTrue(b["reader_alive"])   # (a pty is not an enumerable port)
        self.assertEqual(b["drive"], BOARD.drive)


class Files(unittest.TestCase):
    def test_put_is_verified_then_readable(self):
        body = b"print('hello')\n" * 100
        status, j = req("PUT", "/file?board=fake&name=t.py", body)
        self.assertEqual(status, 200)
        self.assertTrue(j["matches_sent"]); self.assertEqual(j["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(req("GET", "/file?board=fake&name=t.py", raw=True), (200, body))
        self.assertEqual(req("DELETE", "/file?board=fake&name=t.py")[0], 200)
        self.assertEqual(req("DELETE", "/file?board=fake&name=t.py")[0], 404)

    def test_odd_names_and_directories(self):
        req("POST", "/mkdir?board=fake&name=sub%20dir", b"")
        req("PUT", "/file?board=fake&name=sub%20dir/it%27s%20%3Cb%3E.py", b"x")
        status, rows = req("GET", "/list?board=fake&dir=sub%20dir")
        self.assertEqual([(r["name"], r["dir"]) for r in rows], [("it's <b>.py", False)])
        status, rows = req("GET", "/list?board=fake&recursive=1")
        self.assertIn("sub dir/it's <b>.py", [r["name"] for r in rows])
        self.assertEqual(req("POST", "/mkdir?board=fake&name=sub%20dir", b"")[0], 200)     # idempotent
        self.assertEqual(req("DELETE", "/dir?board=fake&name=sub%20dir")[0], 409)          # not empty
        self.assertEqual(req("DELETE", "/dir?board=fake&name=sub%20dir&recursive=1")[0], 200)
        self.assertEqual(req("GET", "/list?board=fake&dir=sub%20dir")[0], 404)

    def test_paths_stay_inside_the_drive(self):
        for name in ("../x", "%2e%2e/x", "a/../../x"):
            self.assertEqual(req("GET", "/file?board=fake&name=" + name)[0], 400, name)
        status, body = req("GET", "/file?board=fake&name=/etc/passwd")      # absolute = drive-relative
        self.assertEqual(status, 404); self.assertNotIn("root:", str(body))
        self.assertEqual(req("GET", "/file?board=fake")[0], 400)              # ?name missing
        self.assertEqual(req("PUT", "/file?board=fake", b"x")[0], 400)
        self.assertEqual(req("GET", "/file?board=fake&name=nope.py")[0], 404)

    def test_bootloader_loop_guard(self):
        body = b"import microcontroller\nmicrocontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\n"
        status, j = req("PUT", "/file?board=fake&name=code.py", body)
        self.assertEqual(status, 409); self.assertIn("bootloader", j["error"])
        self.assertEqual(req("PUT", "/file?board=fake&name=code.py&force=1", body)[0], 200)
        req("DELETE", "/file?board=fake&name=code.py")

    def test_free(self):
        status, j = req("GET", "/free?board=fake")
        self.assertEqual(status, 200); self.assertGreater(j["total"], 0)


class Validation(unittest.TestCase):
    CASES = [
        ("GET", "/serial/read?board=fake&ms=x", 400), ("GET", "/serial/read?board=fake&ms=999999", 400),
        ("GET", "/serial/tail?board=fake&from=-5", 400), ("POST", "/reset?board=fake&mode=zap", 400),
        ("POST", "/bootloader/enter?board=fake&method=zzz", 400), ("POST", "/snippet?board=fake", 400),
        ("POST", "/snippet?board=fake&id=nope", 404), ("GET", "/nope", 404), ("GET", "/list?board=other", 404),
        ("GET", "/list?board=fake&dir=nope", 404),
    ]

    def test_status_codes(self):
        for method, path, want in self.CASES:
            status, j = req(method, path, b"" if method == "POST" else None)
            self.assertEqual(status, want, path); self.assertIn("error", j)

    def test_registry_input(self):
        for body in ('{x', '[1]', '{"id":"b2","baud":"fast"}', '{"id":"fake","fs":"bad"}',
                     '{"id":"fake","ocd":"foo"}', '{"id":"fake","video_size":"big"}', '{"id":"","port":"x"}',
                     '{"id":"bad id!","port":"x"}'):
            status, j = req("POST", "/boards", body.encode())
            self.assertEqual(status, 400, body); self.assertIn("error", j)
        status, b = req("GET", "/boards")
        self.assertTrue(b[0]["serial_open"], "a rejected edit must not touch the board")

    def test_failed_operation_is_not_200(self):
        # the fake board does not answer the info probe: the result carries an error -> 503
        status, j = req("GET", "/info?board=fake&refresh=1", timeout=60)
        self.assertEqual(status, 503); self.assertIn("error", j)


class Console(unittest.TestCase):
    def repl(self, code, ms=6000):
        t = time.time()
        status, j = req("POST", "/repl?board=fake&ms=%d" % ms, code.encode(), timeout=ms / 1000 + 10)
        self.assertEqual(status, 200, j)
        return j["output"], time.time() - t

    def test_repl_collects_until_the_prompt(self):
        out, took = self.repl("OUT first\nSLEEP 1\nOUT second")
        self.assertIn("first", out); self.assertIn("second", out)
        self.assertLess(took, 3, "returns as soon as the prompt is back, not after ms")

    def test_repl_first_output_late(self):
        out, _ = self.repl("SLEEP 0.6\nOUT late")
        self.assertIn("late", out)

    def test_repl_does_not_leak_between_calls(self):
        self.repl("OUT third")
        out, _ = self.repl("OUT fourth")
        self.assertIn("fourth", out); self.assertNotIn("third", out)

    def test_repl_window_expires_with_partial_output(self):
        out, took = self.repl("OUT early\nSLEEP 3\nOUT never", ms=1000)
        self.assertIn("early", out); self.assertNotIn("never", out); self.assertLess(took, 2.5)
        time.sleep(3.2)                                     # let the fake finish its script

    def test_run_sees_the_reboot(self):
        status, j = req("POST", "/run?board=fake&name=code.py&ms=8000", b"print(1)", timeout=20)
        self.assertEqual(status, 200)
        self.assertTrue(j["matches_sent"])
        self.assertIn("soft reboot", j["output"]); self.assertIn("code.py line 2", j["output"])
        req("DELETE", "/file?board=fake&name=code.py")

    def test_repl_while_code_runs(self):
        """Ctrl-C into a running code.py lands on "Press any key to enter the REPL"; the script
        must still arrive in paste mode (the any-key byte used to eat Ctrl-E)."""
        req("POST", "/serial/write?board=fake", b"\x04")     # reboot: code.py runs for ~2 s
        time.sleep(0.3)
        out, took = self.repl("OUT while\nOUT  indented")
        self.assertIn("while", out); self.assertIn("indented", out)
        self.assertLess(took, 4)

    def test_repl_from_press_any_key(self):
        """code.py finished on its own: the board sits at the any-key question."""
        req("POST", "/serial/write?board=fake", b"\x04")
        time.sleep(2.6)                                     # let the fake's code.py end
        status, j = req("GET", "/serial/tail?board=fake&from=-1&ms=0")
        out, _ = self.repl("OUT afterwards")
        self.assertIn("afterwards", out)

    def test_read_longpoll_returns_when_the_board_prints(self):
        req("GET", "/serial/read?board=fake&ms=0")           # drain what is pending
        t = time.time()
        threading.Timer(0.5, BOARD.say, ["ping\r\n"]).start()
        status, text = req("GET", "/serial/read?board=fake&ms=5000")
        self.assertEqual(status, 200); self.assertIn("ping", text)
        self.assertLess(time.time() - t, 2.5)

    def test_tail_does_not_consume(self):
        status, j = req("GET", "/serial/tail?board=fake&from=-1&ms=100")
        cursor = j["next"]
        BOARD.say("tailed\r\n")
        status, a = req("GET", "/serial/tail?board=fake&from=%d&ms=2000" % cursor)
        status, b = req("GET", "/serial/tail?board=fake&from=%d&ms=100" % cursor)
        self.assertIn("tailed", a["text"]); self.assertEqual(a["text"], b["text"])
        self.assertFalse(a.get("gap"))
        req("GET", "/serial/read?board=fake&ms=0")

    def test_write_reports_sent(self):
        status, j = req("POST", "/serial/write?board=fake", b"\x03")
        self.assertEqual((status, j["sent"]), (200, True))
        time.sleep(0.3); req("GET", "/serial/read?board=fake&ms=0")


class Registry(unittest.TestCase):
    def test_concurrent_adds_keep_the_config_valid(self):
        results = []
        def add(i):
            results.append(req("POST", "/boards", ('{"id":"b%d","path":"/nonexistent/%d"}' % (i, i)).encode())[0])
        ts = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(results, [200] * 8)
        with open(os.path.join(WORK, "devagent.json")) as f:
            cfg = json.load(f)
        self.assertEqual(len(cfg["boards"]), 9)
        self.assertEqual([n for n in os.listdir(WORK) if n.startswith("devagent.json.")], [], "temp files left")
        for i in range(8):
            self.assertEqual(req("DELETE", "/boards?id=b%d" % i)[1], {"removed": True})
        self.assertEqual(len(req("GET", "/boards")[1]), 1)

    def test_editing_nothing_changes_nothing(self):
        """POST /boards with just the id (the client's bare `board`) must not rebuild the board:
        it used to close the console and stop openocd for a no-op edit."""
        status, before = req("GET", "/boards")
        fake = next(b for b in before if b["id"] == "fake")
        req("POST", "/serial/write?board=fake", b"\x03")
        time.sleep(0.3)
        status, tail = req("GET", "/serial/tail?board=fake&from=-1&ms=0")
        status, j = req("POST", "/boards", json.dumps({"id": "fake"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual({k: j[k] for k in fake if k in j}, {k: fake[k] for k in fake if k in j})
        status, again = req("GET", "/serial/tail?board=fake&from=%d&ms=0" % tail["next"])
        self.assertGreaterEqual(again["next"], tail["next"], "a rebuilt board restarts its "
                                "console log at 0 - the cursor must not go backwards")

    def test_index_as_digit_string(self):
        status, j = req("POST", "/boards", b'{"id":"fake","index":"3"}')
        self.assertEqual((status, j["index"]), (200, 3))
        req("POST", "/boards", b'{"id":"fake","index":0}')

    def test_drive_with_another_uid_is_not_ours(self):
        boot = os.path.join(BOARD.drive, "boot_out.txt")
        with open(boot, "w") as f:
            f.write(BOOT_OUT % "0BADF00D99")
        try:
            for _ in range(30):
                time.sleep(0.2)
                if req("GET", "/boards")[1][0]["drive"] is None:
                    break
            status, b = req("GET", "/boards")
            self.assertIsNone(b[0]["drive"]); self.assertTrue(b[0]["drive_problem"])
            self.assertEqual(req("PUT", "/file?board=fake&name=x.txt", b"hi")[0], 503)
            self.assertEqual(req("GET", "/free?board=fake")[0], 503)
        finally:
            with open(boot, "w") as f:
                f.write(BOOT_OUT % UID)
        for _ in range(30):
            time.sleep(0.2)
            if req("GET", "/boards")[1][0]["drive"]:
                break
        self.assertEqual(req("GET", "/free?board=fake")[0], 200)

    def test_parse_add(self):
        import devagent
        self.assertEqual(devagent.parse_add(r"jam path=O:\ port=COM4 ocd=rp2350")["ocd"], "rp2350")
        self.assertEqual(devagent.parse_add("pico port=/dev/ttyACM0")["port"], "/dev/ttyACM0")
        for bad in (r"jam:O:\:COM4", "jam foo=1", "path=x"):
            with self.assertRaises(ValueError, msg=bad):
                devagent.parse_add(bad)


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class Capture(unittest.TestCase):
    def ffmpeg_children(self):
        if shutil.which("pgrep") is None:
            return None
        r = subprocess.run(["pgrep", "-P", str(AGENT.pid), "ffmpeg"], capture_output=True, text=True)
        return r.stdout.split()

    def test_snapshot_from_the_synthetic_source(self):
        status, data = req("GET", "/snapshot?board=fake&device=test", raw=True, timeout=60)
        self.assertEqual(status, 200); self.assertEqual(data[:3], b"\xff\xd8\xff")
        time.sleep(1.0)
        self.assertFalse(self.ffmpeg_children(), "ffmpeg still running after the snapshot")
        status, again = req("GET", "/snapshot?board=fake&device=test", raw=True, timeout=60)
        self.assertEqual(status, 200)

    def test_bad_device_is_502(self):
        status, j = req("GET", "/snapshot?board=fake&device=/dev/video99", timeout=60)
        self.assertEqual(status, 502); self.assertIn("error", j)


class Client(unittest.TestCase):
    def setUp(self):
        import devagent_client
        self.dc = devagent_client
        self.dev = devagent_client.Dev("127.0.0.1", PORT, token=TOKEN)

    def test_query_quoting(self):
        self.assertEqual(self.dc._qs(a="x y&z", b=True, c=None, d=0), "?a=x%20y%26z&b=1&d=0")

    def test_round_trip(self):
        local = os.path.join(WORK, "up.py")
        with open(local, "w") as f:
            f.write("OUT from file\n")
        self.dev.put(local, "up.py")
        self.assertEqual(self.dev.get("up.py"), b"OUT from file\n")
        self.assertIn("up.py", [r["name"] for r in self.dev.list()])
        self.assertIn("hi", self.dev.repl("OUT hi"))
        self.dev.rm("up.py")

    def test_errors_are_exceptions_with_status(self):
        with self.assertRaises(self.dc.DevError) as cm:
            self.dev.get("nope.py")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(self.dc.DevUnreachable):
            self.dc.Dev("127.0.0.1", free_port(), timeout=3).list()

    def test_cli(self):
        cli = [sys.executable, os.path.join(REPO, "devagent_client.py"), "--port", str(PORT), "--token", TOKEN]
        r = subprocess.run(cli + ["list"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("boot_out.txt", r.stdout)
        r = subprocess.run(cli + ["get", "nope.py"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1); self.assertIn("404", r.stderr)
        r = subprocess.run(cli + ["--port", str(free_port()), "health"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2); self.assertIn("unreachable", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
