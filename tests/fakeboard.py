"""A CircuitPython board without the board: a pty that answers like the REPL, and a temp
directory that looks like a CIRCUITPY drive (boot_out.txt with a UID).

    python fakeboard.py            prints {"port": ..., "drive": ...} and stays up; register it with
                                   python devagent.py --add 'fake path=<drive> port=<port>'

The fake REPL (what the pty answers to):
  Ctrl-C            "\\r\\n>>> "
  Ctrl-E            paste mode: "=== " prompt, everything echoed until Ctrl-D, then the pasted
                    text is "run": a line  print("--devagent--")  prints the marker,
                    "SLEEP x" pauses x seconds, "OUT text" prints text - anything else is ignored
  Ctrl-D            soft reboot: "soft reboot", "code.py line 1/2" a second apart, then the prompt
  anything else     echoed
So a script sent by /repl is the sequence of OUT and SLEEP lines that describes the output the
board should produce, and its timing."""
import json, os, pty, threading, time, tempfile

UID = "DEADBEEF01"
BOOT_OUT = ("Adafruit CircuitPython 10.0.0 on 2026-01-01; Fake Board with rp2040\r\n"
            "Board ID:fake_board\r\nUID:%s\r\n")


class FakeBoard:
    def __init__(self, uid=UID):
        self.master, slave = pty.openpty()
        self.port = os.ttyname(slave)
        self.drive = tempfile.mkdtemp(prefix="fakecircuitpy-")
        self.uid = uid
        with open(os.path.join(self.drive, "boot_out.txt"), "w") as f:
            f.write(BOOT_OUT % uid)
        threading.Thread(target=self._loop, daemon=True).start()

    def say(self, text):
        os.write(self.master, text.encode())

    def _run_script(self, text):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('print("--devagent--")'):
                self.say("--devagent--\r\n")
            elif line.startswith("SLEEP "):
                time.sleep(float(line[6:]))
            elif line.startswith("OUT "):
                self.say(line[4:] + "\r\n")
        self.say(">>> ")

    def _reboot(self):
        self.say("soft reboot\r\n")
        time.sleep(1.0); self.say("code.py line 1\r\n")
        time.sleep(1.0); self.say("code.py line 2\r\n")
        self.say("\r\nCode done running.\r\n>>> ")

    def _loop(self):
        paste, script = False, ""
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            for ch in data.decode("utf-8", "replace"):
                if ch == "\x03":
                    paste = False; self.say("\r\n>>> ")
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
