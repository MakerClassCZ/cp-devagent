#!/usr/bin/env python3
"""devagent client - drive a board over the devagent HTTP bridge (drive + serial + probe).

    python devagent_client.py --host 192.168.1.150 --port 8101 [--board jam] [--token T] health
    ... list [--dir sub] [--recursive]     get REMOTE [LOCAL]     put LOCAL [REMOTE]
    ... rm REMOTE      mkdir DIR      rmdir DIR [--recursive]     free      ports
    ... read [--ms 3000]      tail [--from N] [--ms 3000]      reboot
    ... run FILE [--as code.py] [--ms 20000]         repl "python one-liner" [--ms 6000]
    ... boards      board [port=COM7 path=M:\\ ...]      forget [ID]     (show / edit / add / drop)
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

Errors are exceptions: DevError(status, message) for anything the agent refused (4xx/5xx,
the message is the agent's own), DevUnreachable when no agent answers. The CLI prints the
message and exits 1 (2 for unreachable).
"""
import argparse
import cmd
import glob
import http.client
import json
import shlex
import os
import sys
import threading
import time
import socket
import urllib.error
import urllib.request
from urllib.parse import quote


class DevError(RuntimeError):
    """The agent answered with an error. `status` is the HTTP status (400 = the request, 404 =
    no such file/board, 409 = refused for the board's sake, 503 = the board or a tool is not
    usable right now, 502 = a tool ran and failed), `message` the agent's own words, `body`
    the whole reply."""
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status, self.message, self.body = status, message, body or {}


class DevUnreachable(DevError):
    """No agent answered at all (host asleep, agent not started, wrong port)."""
    def __init__(self, base, reason):
        super().__init__(0, "devagent unreachable at %s (%s) - is it running there? "
                            "`python devagent.py --port %s` on the host, `health` here to check"
                         % (base, reason, base.rsplit(":", 1)[1]))


def _qs(**params):
    """?k=v&... for the given params, skipping None; True/False become 1/0; values are
    URL-quoted, so a file named 'a b&c.py' or a token with '+' arrives intact."""
    parts = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = int(v)
        parts.append("%s=%s" % (k, quote(str(v), safe="")))
    return ("?" + "&".join(parts)) if parts else ""


class Dev:
    def __init__(self, host, port=8100, timeout=120, board=None, token=None):
        self.base = "http://%s:%d" % (host, port)
        self.timeout = timeout      # seconds, for requests without a natural bound of their own
        self.board = board          # which board on this agent (only needed if it has several)
        self.token = token

    # ---- plumbing ----
    def _url(self, path, **params):
        if self.board:
            params["board"] = self.board
        return self.base + path + _qs(**params)

    def _req(self, verb, path, data=None, deadline=None, raw=False, **params):
        req = urllib.request.Request(self._url(path, **params), data=data, method=verb,
                                     headers={"X-Token": self.token} if self.token else {})
        try:
            with urllib.request.urlopen(req, timeout=deadline or self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            msg = parsed.get("error") or body.decode("utf-8", "replace").strip() or e.reason
            raise DevError(e.code, "%s (HTTP %d)" % (msg, e.code), parsed)
        except urllib.error.URLError as e:
            raise DevUnreachable(self.base, e.reason)
        except socket.timeout:
            raise DevError(0, "no answer from %s within %ss" % (self.base, deadline or self.timeout))
        except (OSError, http.client.HTTPException) as e:    # the agent died mid-request
            raise DevUnreachable(self.base, e)
        if raw:
            return body
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return body.decode("utf-8", "replace")

    def _board_id(self, id=None):
        """The board an edit applies to: the given id, the selected one, else the agent's only
        board - never a guess between several."""
        if id or self.board:
            return id or self.board
        ids = [b["id"] for b in self.boards()]
        if len(ids) == 1:
            return ids[0]
        raise DevError(400, "which board? pass --board ID (configured: %s)" % (ids or "none"))

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
        return self._req("POST", "/boards", json.dumps(dict(fields, id=self._board_id(id))).encode())

    def forget_board(self, id=None):
        return self._req("DELETE", "/boards", id=self._board_id(id))

    def info(self, refresh=False):
        """Board identity (CP version, board id, free RAM). Probed once, then cached by the agent."""
        return self._req("GET", "/info", refresh=refresh)

    def snippets(self):
        return self._req("GET", "/snippets")

    def snippet(self, sid, ms=6000):
        """Run one of the agent's canned scripts (i2c, pins, mem, ...) and return its output."""
        return self._req("POST", "/snippet", data=b"", deadline=ms / 1000 + 30, id=sid, ms=ms)

    def reset(self, mode="soft"):
        return self._req("POST", "/reset", data=b"", mode=mode)

    def snapshot(self, path=None, width=640):
        """One frame from the board's HDMI capture. Returns the JPEG, or writes it to `path`.
        This is how a session SEES the board's screen - use it to check what a game renders."""
        data = self._req("GET", "/snapshot", raw=True, w=width)
        if data[:2] != b"\xff\xd8":
            raise DevError(0, "not a JPEG: %s" % data[:300].decode("utf-8", "replace"))
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    def free(self):
        return self._req("GET", "/free")

    # ---- files ----
    def list(self, dir="", recursive=False):
        return self._req("GET", "/list", dir=dir, recursive=recursive)

    def get(self, remote):
        return self._req("GET", "/file", raw=True, name=remote)

    def put(self, local, remote=None, force=False):
        """Copy a local file to the board. The agent re-reads what landed on the drive; a
        mismatch (a full card, a write the board's own filesystem cache ate) is an error, not
        a reply to inspect. `force` overrides the auto-run guard (see the agent's PUT /file)."""
        with open(local, "rb") as f:
            data = f.read()
        r = self._req("PUT", "/file", data=data, name=remote or os.path.basename(local),
                      force=force or None)
        if isinstance(r, dict) and not r.get("matches_sent", True):
            raise DevError(0, "%s: the drive holds %d B (sha256 %s), not what was sent"
                           % (remote or local, r.get("size", -1), r.get("sha256")), r)
        return r

    def rm(self, remote):
        return self._req("DELETE", "/file", name=remote)

    def mkdir(self, name):
        return self._req("POST", "/mkdir", data=b"", name=name)

    def rmdir(self, name, recursive=False):
        return self._req("DELETE", "/dir", name=name, recursive=recursive)

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
        """Console output collected over the next `ms` - CONSUMED: the panel's console will not
        show it. Prefer tail() for anything that runs beside the panel."""
        return self._req("GET", "/serial/read", raw=True, deadline=ms / 1000 + 30,
                         ms=ms).decode("utf-8", "replace")

    def tail(self, frm=-1, ms=1500):
        """Non-consuming console read: returns {'next', 'text'[, 'gap']}; pass the previous
        reply's 'next' as `frm`. frm=-1 = just take a cursor at the current end."""
        return self._req("GET", "/serial/tail", deadline=ms / 1000 + 30, **{"from": frm, "ms": ms})

    def write(self, text):
        return self._req("POST", "/serial/write", data=text.encode())

    def reboot(self):
        return self._req("POST", "/serial/reboot", data=b"")

    def run(self, local, as_name="code.py", ms=8000, force=False):
        """Upload `local` as `as_name`, soft-reboot, and return what the board printed in the
        next `ms` (the reply's 'output'; the run is cut short when the program ends)."""
        with open(local, "rb") as f:
            data = f.read()
        return self._req("POST", "/run", data=data, deadline=ms / 1000 + 30,
                         name=as_name, ms=ms, force=force or None)

    def repl(self, code, ms=6000):
        """Paste code into the REPL and return what it printed - no filesystem write.

        Goes through the agent (POST /repl), which reads via a tap: doing it here with
        write-then-read raced the web panel's console poll, and whoever asked first got
        the output."""
        r = self._req("POST", "/repl", data=code.encode("utf-8"), deadline=ms / 1000 + 30, ms=ms)
        return r["output"] if isinstance(r, dict) else r

    # ---- bootloader / UF2 ----
    def bootloader_status(self):
        return self._req("GET", "/bootloader/status")

    def bootloader_enter(self, method="repl", timeout=20):
        return self._req("POST", "/bootloader/enter", data=b"", deadline=timeout + 20,
                         method=method, timeout=timeout)

    def uf2(self, path, enter=True, wait=True):
        """Flash a .uf2 the bootloader way: enter BOOTSEL, copy, wait for the board back."""
        with open(path, "rb") as f:
            data = f.read()
        return self._req("POST", "/uf2", data=data, deadline=300, enter=enter, wait=wait)

    # ---- debug probe ----
    def ocd(self, action, cfg=None):
        if action == "status":
            return self._req("GET", "/ocd/status")
        if action not in ("start", "stop"):
            raise DevError(400, "ocd action must be start, stop, status or cmd, not %r" % action)
        return self._req("POST", "/ocd/" + action, data=b"", cfg=cfg if action == "start" else None)

    def ocd_cmd(self, cmd):
        return self._req("POST", "/ocd/cmd", data=cmd.encode())

    def flash(self, path, verify=True, reset=True):
        with open(path, "rb") as f:
            data = f.read()
        return self._req("POST", "/ocd/flash", data=data, deadline=900, verify=verify, reset=reset)


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
        raise DevError(0, "console needs a terminal (stdin is not a tty)")
    first = d.tail(-1, 0)
    if not isinstance(first, dict) or "next" not in first:
        raise DevError(0, "unexpected reply to /serial/tail: %r" % (first,))
    try:
        label = label or d._board_id()
    except DevError:
        label = d.base
    sys.stdout.write("--- console on %s: Ctrl-] leaves, Ctrl-C / Ctrl-D go to the board ---\r\n" % label)
    sys.stdout.flush()
    stop = threading.Event()

    def note(text):
        sys.stdout.write("\r\n[devagent] %s\r\n" % text)
        sys.stdout.flush()

    def pump():
        cursor, away = first["next"], False
        while not stop.is_set():
            try:
                r = d.tail(cursor, 1500)
            except DevError as e:               # agent away or the board gone: say so once,
                if not away:                    # keep trying, and say when it is back
                    note("%s - retrying" % e.message)
                    away = True
                time.sleep(1.0)
                continue
            if away:
                note("agent is back")
                away = False
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
                try:
                    sent = d.write(keys).get("sent")
                    why = None if sent else "serial port not open - board missing or busy?"
                except DevError as e:
                    why = e.message
                if why and not warned:
                    note("not sent: %s" % why)
                    warned = True
                elif not why:
                    warned = False
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
                       "mkdir", "rmdir", "read", "tail", "reboot", "run", "repl", "ocd", "bootloader",
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
        except DevError as e:
            print(e.message)

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
        except SystemExit as e:                # argparse errors: stay in the shell
            if e.code not in (0, None):
                print("(exit %s)" % e.code)
        except KeyboardInterrupt:
            print("^C")
        except Exception as e:                 # a bug must not take the shell down with it
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
    ap.add_argument("--ms", type=int, default=None, help="how long to collect output (read/tail/"
                    "run/repl/snippet; each has its own default)")
    ap.add_argument("--from", dest="frm", type=int, default=-1, help="tail: cursor to continue from")
    ap.add_argument("--dir", default="")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--as", dest="as_name", default="code.py")
    ap.add_argument("--force", action="store_true", help="put/run: override the auto-run guard")
    ap.add_argument("--board", default=None, help="board id (only needed if the agent has several)")
    ap.add_argument("--token", default=os.environ.get("DEVAGENT_TOKEN"),
                    help="the agent's token (or the DEVAGENT_TOKEN environment variable)")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    d = Dev(a.host, a.port, board=a.board, token=a.token)
    try:
        return _run(ap, a, d)
    except DevUnreachable as e:
        print("error: %s" % e.message, file=sys.stderr)
        return 2
    except DevError as e:
        print("error: %s" % e.message, file=sys.stderr)
        return 1
    except IndexError:
        print("error: %s needs an argument - `--help` shows the syntax" % a.cmd, file=sys.stderr)
        return 2
    except OSError as e:                       # a local file that is not there, not writable
        print("error: %s" % e, file=sys.stderr)
        return 1


def _run(ap, a, d):
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
        show(d.put(rest[0], rest[1] if len(rest) > 1 else None, force=a.force))
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
    elif c == "tail":                                     # what the board says next, nothing consumed
        r = d.tail(a.frm, a.ms or 1500)
        emit(r.get("text", ""))
        print("-- next cursor: %s%s" % (r.get("next"), " (gap: output lost)" if r.get("gap") else ""),
              file=sys.stderr)
    elif c == "reboot":
        show(d.reboot())
    elif c == "run":
        r = d.run(rest[0], a.as_name, a.ms or 8000, force=a.force)
        sys.stdout.write(r.pop("output", "") if isinstance(r, dict) else "")
        show(r)
    elif c == "repl":
        emit(d.repl(rest[0], a.ms or 6000))
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
        if fields:
            show(d.set_board(**fields))
        else:                                            # no fields: show the board, change nothing
            want = d._board_id()
            show(next(b for b in d.boards() if b["id"] == want))
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
        if isinstance(r, dict) and r.get("note"):
            print("note: %s" % r["note"], file=sys.stderr)
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
        return 0 if r.get("ok") else 1
    else:
        ap.error("unknown command %r" % c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
