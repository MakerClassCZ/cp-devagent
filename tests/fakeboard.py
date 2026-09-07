"""A CircuitPython board without the board: a pty that answers like the REPL, and a temp
directory that looks like a CIRCUITPY drive (boot_out.txt with a UID).

    python fakeboard.py            prints {"port": ..., "drive": ...} and stays up; register it with
                                   python devagent.py --add 'fake path=<drive> port=<port>'

The fake REPL (what the pty answers to):
  Ctrl-C            "\\r\\n>>> " - or, while code.py "runs" (the 2 s after a reboot), a
                    KeyboardInterrupt traceback and "Press any key to enter the REPL", after which
                    the next byte only enters the REPL (it is not typed), as on a real board
  Ctrl-E            paste mode: "=== " prompt, everything echoed until Ctrl-D, then the pasted
                    text is "run": a line  print("--devagent--")  prints the marker,
                    "SLEEP x" pauses x seconds, "OUT text" prints text - anything else is ignored
  Ctrl-D            soft reboot: "soft reboot", "code.py output:", then code.py from the drive
                    (OUT/SLEEP lines, as in paste mode; a file without any = "code.py line 1/2"
                    a second apart), then "Code done running." and the any-key question
  anything else     echoed
So a script sent by /repl, or written as code.py by /run, is the sequence of OUT and SLEEP lines
that describes the output the board should produce, and its timing."""
import json, os, pty, threading, time, tempfile

UID = "DEADBEEF01"
PRESS_ANY_KEY = "Press any key to enter the REPL"
BOOT_OUT = ("Adafruit CircuitPython 10.0.0 on 2026-01-01; Fake Board with rp2040\r\n"
            "Board ID:fake_board\r\nUID:%s\r\n")


class FakeBoard:
    def __init__(self, uid=UID):
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.drive = tempfile.mkdtemp(prefix="fakecircuitpy-")
        self.uid = uid
        self.running = False                                # code.py "running" (after a reboot)
        self.any_key = False                                # at "Press any key to enter the REPL"
        with open(os.path.join(self.drive, "boot_out.txt"), "w") as f:
            f.write(BOOT_OUT % uid)
        threading.Thread(target=self._loop, daemon=True).start()

    def say(self, text):
        os.write(self.master, text.encode())

    def _play(self, text, interruptible=False):
        """Run a script in the fake's language. False if Ctrl-C ended it early (code.py only)."""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('print("--devagent--")'):
                self.say("--devagent--\r\n")
            elif line.startswith("SLEEP "):
                until = time.time() + float(line[6:])
                while time.time() < until:
                    time.sleep(0.05)
                    if interruptible and not self.running:
                        return False
            elif line.startswith("OUT "):
                self.say(line[4:] + "\r\n")
        return True

    def _run_script(self, text):
        self._play(text)
        self.say(">>> ")

    def _reboot(self):
        self.running = True
        self.say("soft reboot\r\n")
        self.say("Auto-reload is on. Simply save files over USB to run them or enter REPL to "
                 "disable.\r\ncode.py output:\r\n")
        code = "SLEEP 1\nOUT code.py line 1\nSLEEP 1\nOUT code.py line 2"
        try:
            with open(os.path.join(self.drive, "code.py")) as f:
                written = f.read()
            if any(l.strip().startswith(("OUT ", "SLEEP ")) for l in written.splitlines()):
                code = written                              # what /run wrote is what runs
        except OSError:
            pass
        if not self._play(code, interruptible=True):
            return                                          # Ctrl-C printed the traceback
        self.running = False
        self._code_done()

    def _code_done(self):
        """code.py is over: CircuitPython does not show the prompt yet, it asks for a key."""
        self.say("\r\nCode done running.\r\n\r\n%s. Use CTRL-D to reload.\r\n" % PRESS_ANY_KEY)
        self.any_key = True

    def _loop(self):
        paste, script = False, ""
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            for ch in data.decode("utf-8", "replace"):
                if self.any_key and ch != "\x04":
                    self.any_key = False                    # the key enters the REPL, nothing more
                    self.say("\r\nAdafruit CircuitPython 10.0.0 on 2026-01-01; Fake Board\r\n>>> ")
                elif ch == "\x03":
                    paste = False
                    if self.running:
                        self.running = False
                        self.say("\r\nTraceback (most recent call last):\r\n  File \"code.py\", "
                                 "line 1, in <module>\r\nKeyboardInterrupt: \r\n")
                        self._code_done()
                    else:
                        self.say("\r\n>>> ")
                elif ch == "\x05":
                    paste, script = True, ""
                    self.say("\r\npaste mode; Ctrl-C to cancel, Ctrl-D to finish\r\n=== ")
                elif ch == "\x04":
                    if paste:
                        paste = False
                        threading.Thread(target=self._run_script, args=(script,), daemon=True).start()
                    else:
                        threading.Thread(target=self._reboot, daemon=True).start()
                else:
                    if paste:
                        script += ch.replace("\r", "\n")
                    self.say(ch)                            # echo


if __name__ == "__main__":
    board = FakeBoard()
    print(json.dumps({"port": board.port, "drive": board.drive}), flush=True)
    while True:
        time.sleep(3600)
