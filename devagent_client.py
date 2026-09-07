#!/usr/bin/env python3
"""devagent client - drive a board over the devagent HTTP bridge (drive + serial + probe).

    python devagent_client.py --host 192.168.1.150 --port 8101 [--board jam] [--token T] health
    ... list [--dir sub] [--recursive]     get REMOTE [LOCAL]     put LOCAL [REMOTE]
    ... rm REMOTE      mkdir DIR      rmdir DIR [--recursive]     free      ports
    ... read [--ms 3000]      reboot      run FILE [--as code.py] [--ms 20000]
    ... repl "python one-liner" [--ms 4000]
    ... boards      board port=COM7 [path=M:\\ ...]      forget [ID]     (edit / add / drop)
    ... info [refresh]      snippets      snippet i2c      reset [soft|hard]
    ... snapshot [OUT.jpg] [--width 640]                 (HDMI capture, if the board has one)
    ... ocd start|stop|status      ocd cmd "halt"      flash FIRMWARE.elf   (SWD)
    ... bootloader [enter]          uf2 FIRMWARE.uf2                        (BOOTSEL)
    ... shell                       (interactive: the same commands, host/board kept, tab completion)
    ... console                     (live terminal on the board's REPL; Ctrl-] leaves)

Importable too, which is the point - a session should not re-derive this:

    from devagent_client import Dev
    d = Dev("192.168.1.150", 8101)
    d.put("game.py"); print(d.repl("import game; game.main()", ms=6000))

`repl` pastes into the REPL WITHOUT writing the drive - use it for anything that resets the
board, since a reset left in code.py re-runs on every boot and traps the board in a loop.
Every put verifies against the sha256 the agent read back from the drive.
"""
import argparse
import cmd
import glob
import json
import shlex
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote


class Dev:
    def __init__(self, host, port=8100, timeout=120, board=None, token=None):
        self.base = "http://%s:%d" % (host, port)
        self.timeout = timeout
        self.board = board          # which board on this agent (only needed if it has several)
        self.token = token

    # ---- plumbing ----
    def _url(self, path):
        extra = []
        if self.board:
            extra.append("board=" + quote(self.board))
        if self.token:
            extra.append("token=" + quote(self.token))
        if not extra:
            return self.base + path
        return self.base + path + ("&" if "?" in path else "?") + "&".join(extra)

    def _req(self, method, path, data=None, timeout=None, raw=False):
        req = urllib.request.Request(self._url(path), data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
        except urllib.error.URLError as e:
            # An unreachable agent is the single most common failure here (host asleep,
            # agent not started, wrong port) - say that in one line, not a traceback.
            raise SystemExit("devagent unreachable at %s (%s).\n"
                             "  Is it running on the host? python devagent.py --path ... --port %s"
                             % (self.base, e.reason, self.base.rsplit(":", 1)[1]))
        if raw:
            return body
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return body.decode("utf-8", "replace")

    # ---- info ----
    def health(self):
        return self._req("GET", "/health")

    def version(self):
        return self._req("GET", "/version")

    def ports(self):
        """Serial ports, drives, OpenOCD configs and capture devices the host can see."""
        return self._req("GET", "/discover")

    def boards(self):
        return self._req("GET", "/boards")

    def set_board(self, id=None, **fields):
        """Add or edit a board: `set_board(port="COM7")` changes one field of the selected board,
        the rest keep their values (None clears one). A new id needs at least a drive or a port.
        Not needed for the same board on a new COM number / drive letter - the agent follows
        it by UID on its own."""
        return self._req("POST", "/boards", json.dumps(dict(fields, id=id or self.board)).encode())

    def forget_board(self, id=None):
        return self._req("DELETE", "/boards?id=%s" % (id or self.board))

    def info(self, refresh=False):
        """Board identity (CP version, board id, free RAM). Probed once, then cached by the agent."""
        return self._req("GET", "/info?refresh=%d" % (1 if refresh else 0))

    def snippets(self):
        return self._req("GET", "/snippets")

    def snippet(self, sid, ms=6000):
        """Run one of the agent's canned scripts (i2c, pins, mem, ...) and return its output."""
        return self._req("POST", "/snippet?id=%s&ms=%d" % (quote(sid), ms), data=b"")

    def reset(self, mode="soft"):
        return self._req("POST", "/reset?mode=" + mode, data=b"")

    def snapshot(self, path=None, width=640):
        """One frame from the board's HDMI capture. Returns the JPEG, or writes it to `path`.
        This is how a session SEES the board's screen - use it to check what a game renders."""
        data = self._req("GET", "/snapshot?w=%d" % width, raw=True)
        if data[:2] != b"\xff\xd8":
            raise RuntimeError(data[:300].decode("utf-8", "replace"))
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def free(self):
        return self._req("GET", "/free")

    # ---- files ----
    def list(self, dir="", recursive=False):
        return self._req("GET", "/list?dir=%s&recursive=%d" % (dir, 1 if recursive else 0))

    def get(self, remote):
        return self._req("GET", "/file?name=" + remote, raw=True)

    def put(self, local, remote=None):
        with open(local, "rb") as f:
            data = f.read()
        return self._req("PUT", "/file?name=" + (remote or os.path.basename(local)), data=data)

    def rm(self, remote):
        return self._req("DELETE", "/file?name=" + remote)

    def mkdir(self, name):
        return self._req("POST", "/mkdir?name=" + name, data=b"")

    def rmdir(self, name, recursive=False):
        return self._req("DELETE", "/dir?name=%s&recursive=%d" % (name, 1 if recursive else 0))

    def put_tree(self, local_dir, remote_dir=""):
        """Upload a folder, creating directories as needed. Returns per-file results."""
        out = {}
        for root, _dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir).replace("\\", "/")
            rel = "" if rel == "." else rel
            target = "/".join(p for p in (remote_dir, rel) if p)
            if target:
                self.mkdir(target)
            for n in sorted(files):
                dest = "/".join(p for p in (target, n) if p)
                out[dest] = self.put(os.path.join(root, n), dest)
        return out

    # ---- console ----
    def read(self, ms=1500):
        return self._req("GET", "/serial/read?ms=%d" % ms, raw=True).decode("utf-8", "replace")

    def tail(self, frm=-1, ms=1500):
        """Non-consuming console read (agent >= 6): returns {'next', 'text'[, 'gap']}; pass the
        previous reply's 'next' as `frm`. frm=-1 = just take a cursor at the current end."""
        return self._req("GET", "/serial/tail?from=%d&ms=%d" % (frm, ms))

    def write(self, text):
        return self._req("POST", "/serial/write", data=text.encode())

    def reboot(self):
        return self._req("POST", "/serial/reboot", data=b"")

    def run(self, local, as_name="code.py", ms=8000):
        with open(local, "rb") as f:
            data = f.read()
        return self._req("POST", "/run?name=%s&ms=%d" % (as_name, ms), data=data, timeout=ms / 1000 + 30)

    def repl(self, line, ms=4000, settle=0.4):
        """Paste code into the REPL and return what it printed - no filesystem write.

        Goes through the agent (POST /repl), which reads via a tap: doing it here with
        write-then-read raced the web panel's console poll, and whoever asked first got
        the output. Falls back to the old way against a pre-v5 agent."""
        r = self._req("POST", "/repl?ms=%d" % ms, data=line.encode("utf-8"))
        if isinstance(r, dict) and "output" in r:
            return r["output"]
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])
        return self._repl_legacy(line, ms, settle)

    def _repl_legacy(self, line, ms=4000, settle=0.4):
        """Ctrl-C into the REPL and run one line. The first char after an interrupt is
        eaten by the prompt, so a bare newline goes first - learned the hard way."""
        self.write("\x03")
        time.sleep(settle)
        self.write("\r\n")
        time.sleep(0.2)
        self.read(500)                                   # drop the banner/prompt
        self.write(line + "\r\n")
        time.sleep(ms / 1000.0)
        return self.read(2000)

    # ---- bootloader / UF2 ----
    def bootloader_status(self):
        return self._req("GET", "/bootloader/status")

    def bootloader_enter(self, method="repl", timeout=20):
        return self._req("POST", "/bootloader/enter?method=%s&timeout=%d" % (method, timeout),
                         data=b"", timeout=timeout + 20)

    def uf2(self, path, enter=True, wait=True):
        """Flash a .uf2 the bootloader way: enter BOOTSEL, copy, wait for the board back."""
        with open(path, "rb") as f:
            data = f.read()
        return self._req("POST", "/uf2?enter=%d&wait=%d" % (int(enter), int(wait)),
                         data=data, timeout=300)

    # ---- debug probe ----
    def ocd(self, action, cfg=None):
        if action == "status":
            return self._req("GET", "/ocd/status")
        qs = "?cfg=" + cfg if (action == "start" and cfg) else ""
        return self._req("POST", "/ocd/" + action + qs, data=b"")

    def ocd_cmd(self, cmd):
        return self._req("POST", "/ocd/cmd", data=cmd.encode())

    def flash(self, path, verify=True, reset=True):
        with open(path, "rb") as f:
            data = f.read()
        return self._req("POST", "/ocd/flash?verify=%d&reset=%d" % (int(verify), int(reset)),
                         data=data, timeout=900)


# ---- interactive: a live console and a command shell ----------------------------------------
def _keystrokes():
    """Yield raw keyboard input (no line buffering, no local echo) until the generator is closed.
    Windows: msvcrt, arrow/Home/End/Del keys mapped to the ANSI sequences the CP REPL understands.
    POSIX: the tty in raw mode, so Ctrl-C / Ctrl-D / escape sequences pass through as bytes."""
    if os.name == "nt":
        import msvcrt
        special = {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C",
                   "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~"}
        while True:
            try:
                ch = msvcrt.getwch()
            except KeyboardInterrupt:          # a console in cooked mode turns Ctrl-C into a signal
                yield "\x03"
                continue
            if ch in ("\x00", "\xe0"):
                yield special.get(msvcrt.getwch(), "")
            else:
                yield ch
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    data = os.read(fd, 256)
                    if not data:
                        return
                    yield data.decode("utf-8", "replace")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def console(d, label=None):
    """A live terminal on the board's REPL, like `screen` on a serial port but through the agent:
    keystrokes go to /serial/write, output comes from /serial/tail (which consumes nothing, so the
    web panel keeps working next to it). Ctrl-] leaves; Ctrl-C and Ctrl-D go to the board."""
    if not sys.stdin.isatty():
        raise SystemExit("console needs a terminal (stdin is not a tty)")
    first = d.tail(-1, 0)
    if not isinstance(first, dict) or "next" not in first:
        raise SystemExit("this agent has no /serial/tail (agent v6+ needed): %r" % (first,))
    label = label or d.board or d.base
    sys.stdout.write("--- console on %s: Ctrl-] leaves, Ctrl-C / Ctrl-D go to the board ---\r\n" % label)
    sys.stdout.flush()
    stop = threading.Event()

    def pump():
        cursor = first["next"]
        while not stop.is_set():
            try:
                r = d.tail(cursor, 1500)
            except (Exception, SystemExit):            # agent away: keep trying, quietly
                time.sleep(0.5)
                continue
            if isinstance(r, dict) and "next" in r:
                cursor = r["next"]
                if r.get("text"):
                    sys.stdout.write(r["text"])
                    sys.stdout.flush()

    threading.Thread(target=pump, daemon=True).start()
    warned = False
    try:
        for keys in _keystrokes():
            leave = "\x1d" in keys                                 # Ctrl-]
            if leave:
                keys = keys[:keys.index("\x1d")]
            if keys:
                r = d.write(keys)
                if not (isinstance(r, dict) and r.get("sent")) and not warned:
                    why = r.get("error") if isinstance(r, dict) else r
                    sys.stdout.write("\r\n[devagent] not sent: %s\r\n"
                                     % (why or "serial port not open - board missing or busy?"))
                    sys.stdout.flush()
                    warned = True
            if leave:
                break
    finally:
        stop.set()
    sys.stdout.write("\r\n--- console closed ---\n")
    return 0


class Shell(cmd.Cmd):
    """`devagent_client.py shell`: the CLI commands at a prompt, host/port/token kept between them,
    the current board in the prompt (`use ID` switches), history + tab completion via readline
    where it exists. Every line is dispatched through main(), so shell and CLI cannot drift."""
    LOCAL_ARG = ("put", "puttree", "run", "uf2", "flash")             # first arg = a local path
    REMOTE_ARG = ("get", "rm", "rmdir", "mkdir")                       # first arg = a remote path
    WORDS = {"ocd": ["start", "stop", "status", "cmd"], "reset": ["soft", "hard"],
             "bootloader": ["enter"], "info": ["refresh"]}
    COMMANDS = sorted(["health", "version", "ports", "free", "list", "get", "put", "puttree", "rm",
                       "mkdir", "rmdir", "read", "reboot", "run", "repl", "ocd", "bootloader",
                       "uf2", "boards", "board", "forget", "info", "snippets", "snippet", "reset",
                       "snapshot", "flash", "console", "use", "help", "quit"])

    def __init__(self, host, port, token=None, board=None):
        super().__init__()
        self.base = ["--host", host, "--port", str(port)] + (["--token", token] if token else [])
        self.board = board
        self.dev = Dev(host, port, board=board, token=token)
        try:
            import readline                 # history + line editing, where the platform has it
            readline.set_completer_delims(" \t\n")
        except ImportError:
            pass

    # -- prompt / banner
    def _prompt(self):
        self.prompt = "%s> " % (self.board or self.dev.base.split("//", 1)[-1])

    def preloop(self):
        try:
            boards = self.dev.boards()
            ids = [b["id"] for b in boards]
            if len(ids) == 1 and not self.board:
                self.board = self.dev.board = ids[0]
            print("devagent %s · boards: %s · `use ID` switches, `console` = live REPL, "
                  "Ctrl-D quits" % (self.dev.base, ", ".join(ids) or "-"))
        except Exception as e:
            print("devagent %s: %s" % (self.dev.base, e))
        self._prompt()

    def postcmd(self, stop, line):
        self._prompt()
        return stop

    def emptyline(self):
        return False

    # -- the two shell-only commands
    def do_use(self, arg):
        """use ID          make ID the current board (the prompt shows it)"""
        arg = arg.strip()
        if not arg:
            print(self.board or "(no board selected)")
        else:
            self.board = arg
            self.dev.board = arg

    def do_console(self, arg):
        """console         live terminal on the current board's REPL (Ctrl-] leaves)"""
        try:
            console(self.dev, self.board)
        except SystemExit as e:
            print(e)

    def do_quit(self, arg):
        return True

    do_exit = do_EOF = do_quit

    def do_help(self, arg):
        usage = __doc__.split("Importable")[0].strip().split("\n", 1)[1]
        print(usage.rstrip())
        print("    ... use ID                      (switch board)      quit / Ctrl-D")

    # -- everything else = the CLI, argv rebuilt per line
    def default(self, line):
        try:
            argv = shlex.split(line)
        except ValueError as e:
            print("parse error: %s" % e)
            return
        if not argv:
            return
        if argv[0] not in self.COMMANDS:
            print("unknown command %r - `help` lists them" % argv[0])
            return
        argv = self.base + (["--board", self.board] if self.board else []) + argv
        try:
            main(argv)
        except SystemExit as e:                # argparse errors / explicit exits: stay in the shell
            if e.code not in (0, None):
                print("(exit %s)" % e.code)
        except (IndexError, KeyError):
            print("missing argument - `help` shows the syntax")
        except KeyboardInterrupt:
            print("^C")
        except Exception as e:
            print("%s: %s" % (type(e).__name__, e))

    # -- completion
    def completenames(self, text, *ignored):
        return [c for c in self.COMMANDS if c.startswith(text)]

    def completedefault(self, text, line, begidx, endidx):
        words = line[:begidx].split()
        head = words[0] if words else ""
        nth = len(words)                   # which argument is being completed (1 = first)
        try:
            if head == "use":
                cands = [b["id"] for b in self.dev.boards()]
            elif head == "snippet":
                cands = [sn["id"] for sn in self.dev.snippets()]
            elif head in self.WORDS and nth == 1:
                cands = self.WORDS[head]
            elif head in self.LOCAL_ARG and nth == 1:
                cands = [p + ("/" if os.path.isdir(p) else "") for p in glob.glob(text + "*")]
            elif head in self.REMOTE_ARG or (head in self.LOCAL_ARG and nth == 2):
                folder, _, stem = text.rpartition("/")
                cands = [folder + ("/" if folder else "") + e["name"] + ("/" if e.get("dir") else "")
                         for e in self.dev.list(folder)]
                return [c for c in cands if c.startswith(text)]
            else:
                return []
        except Exception:
            return []
        return [c for c in cands if c.startswith(text)]

    complete_use = complete_console = completedefault


def shell(host, port, token=None, board=None):
    try:
        Shell(host, port, token, board).cmdloop()
    except KeyboardInterrupt:
        print()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--ms", type=int, default=None)
    ap.add_argument("--dir", default="")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--as", dest="as_name", default="code.py")
    ap.add_argument("--board", default=None, help="board id (only needed if the agent has several)")
    ap.add_argument("--token", default=None)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    d = Dev(a.host, a.port, board=a.board, token=a.token)
    c, rest = a.cmd, a.args

    def show(x):
        print(json.dumps(x, indent=1) if isinstance(x, (dict, list)) else x)

    def emit(text):                        # console output: as-is, but end on a fresh line
        sys.stdout.write(text if not text or text.endswith("\n") else text + "\n")

    if c == "health":
        show(d.health())
    elif c == "version":
        show(d.version())
    elif c == "ports":
        show(d.ports())
    elif c == "free":
        f = d.free()
        if isinstance(f, dict) and "free" in f:
            print("%.1f KB free of %.1f KB" % (f["free"] / 1024.0, f["total"] / 1024.0))
        else:
            show(f)
    elif c == "list":
        for e in d.list(a.dir, a.recursive):
            print("%-40s %s" % (e["name"] + ("/" if e.get("dir") else ""),
                                "" if e.get("dir") else e["size"]))
    elif c == "get":
        data = d.get(rest[0])
        if len(rest) > 1:
            open(rest[1], "wb").write(data)
            print("wrote %s (%d B)" % (rest[1], len(data)))
        else:
            sys.stdout.write(data.decode("utf-8", "replace"))
    elif c == "put":
        show(d.put(rest[0], rest[1] if len(rest) > 1 else None))
    elif c == "puttree":
        show(d.put_tree(rest[0], rest[1] if len(rest) > 1 else ""))
    elif c == "rm":
        show(d.rm(rest[0]))
    elif c == "mkdir":
        show(d.mkdir(rest[0]))
    elif c == "rmdir":
        show(d.rmdir(rest[0], a.recursive))
    elif c == "read":
        emit(d.read(a.ms or 1500))
    elif c == "reboot":
        show(d.reboot())
    elif c == "run":
        r = d.run(rest[0], a.as_name, a.ms or 8000)
        sys.stdout.write(r.pop("output", "") if isinstance(r, dict) else "")
        show(r)
    elif c == "repl":
        emit(d.repl(rest[0], a.ms or 4000))
    elif c == "ocd":
        if rest and rest[0] == "cmd":
            show(d.ocd_cmd(rest[1]))
        else:
            show(d.ocd(rest[0] if rest else "status", rest[1] if len(rest) > 1 else None))
    elif c == "bootloader":
        if rest and rest[0] == "enter":
            show(d.bootloader_enter(rest[1] if len(rest) > 1 else "repl"))
        else:
            show(d.bootloader_status())
    elif c == "uf2":
        r = d.uf2(rest[0])
        show(r)
    elif c == "boards":
        for b in d.boards():
            print("%-10s drive=%-14s serial=%-8s ocd=%-7s video=%-12s uid=%s"
                  % (b["id"], b.get("drive") or "-", b.get("port") or "-",
                     b.get("ocd") or "-", b.get("video") or "-", b.get("uid") or "-"))
    elif c == "board":                                   # board port=COM7 path=M:\  (edit / add)
        fields = dict(kv.split("=", 1) for kv in rest)
        for k, v in fields.items():
            fields[k] = None if v in ("", "none", "null") else v
        show(d.set_board(**fields))
    elif c == "forget":
        show(d.forget_board(rest[0] if rest else None))
    elif c == "info":
        show(d.info(bool(rest and rest[0] == "refresh")))
    elif c == "snippets":
        for sn in d.snippets():
            print("%-10s %s" % (sn["id"], sn["label"]))
    elif c == "snippet":
        r = d.snippet(rest[0], a.ms or 6000)
        emit(r.pop("output", "") if isinstance(r, dict) else str(r))
    elif c == "reset":
        show(d.reset(rest[0] if rest else "soft"))
    elif c == "snapshot":
        out = rest[0] if rest else "screen.jpg"
        d.snapshot(out, a.width)
        print("wrote %s" % out)
    elif c == "shell":
        return shell(a.host, a.port, a.token, a.board)
    elif c == "console":
        return console(d)
    elif c == "flash":
        r = d.flash(rest[0])
        out = r.pop("output", "") if isinstance(r, dict) else ""
        show(r)
        if out:
            print(out[-1500:])
    else:
        ap.error("unknown command %r" % c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
