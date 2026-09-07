#!/usr/bin/env python3
"""devagent - every dev board on this host, over HTTP: drives, serial consoles, probes, screens.

Start it with no arguments and add boards from the web panel at http://HOST:8100/ :

    python devagent.py                       # panel on http://127.0.0.1:8100/, boards added in the UI
    python devagent.py --bind 0.0.0.0 --token secret   # serve the network; the token is required
    python devagent.py --add jam:O:\\:COM4:rp2350   # optional: define a board on the CLI

Boards live in devagent.json next to this file, so a restart brings them back. Each board keeps
its own serial reader, rolling console buffer and (optionally) its own OpenOCD instance on its
own ports, so several boards are served at once and the panel just switches tabs.

Needs `pyserial` for the console half. `cpfs.py` from MakerClassCZ/circuitpython-filesystem is
optional and enables file access over the REPL for boards with no USB drive at all. `ffmpeg` is
optional too: give a board a capture device and /snapshot returns what its HDMI output is showing,
which is how a script (or an agent) can SEE the board instead of inferring from serial text.

Design notes (learned the hard way, keep them):
  * Nothing is cached. A drive is re-checked and a port re-opened per request, because a board
    reset re-enumerates USB and invalidates long-lived handles - the classic way bridges wedge.
  * Every write is open -> write -> flush -> fsync -> close, then re-read, so the reply carries
    the sha256 of what is ACTUALLY on disk. Never trust an echo.
  * OpenOCD runs on THIS host and sees THIS host's filesystem, so firmware arrives as a request
    BODY and is staged locally - a remote caller's path means nothing here.
  * The TCL port swallows OpenOCD's log; telnet echoes it. Anything whose failure reason matters
    (flashing) goes through telnet.
  * A UF2 copy "fails" on success: the board reboots mid-write and the volume vanishes.
  * Bootloader entry NEVER goes through a file. See autorun_guard.

Endpoints (all take ?board=<id>; with one board configured it is optional)
    GET    /                            the web panel
    GET    /health /version /discover   status · agent version · serial ports + drives on this host
    GET    /boards                      configured boards        POST /boards   add or update (JSON)
    DELETE /boards?id=                  forget a board
    GET    /list /file /free            files (drive or REPL backend)
    PUT    /file?name= [&force=1]       write (parents auto-created; guard on auto-run files)
    DELETE /file?name= · /dir?name=     delete file · directory (&recursive=1)
    POST   /mkdir?name=                 create directory
    GET    /serial/read?ms=             console output      POST /serial/write · /serial/reboot
    GET    /serial/tail?from=&ms=       console output by CURSOR (nothing is consumed - a script
                                        and the panel can both read the whole stream)
    POST   /run?name=&ms=               upload + reboot + capture
    GET    /info[?refresh=1]            board identity, probed once and cached
    GET    /snippets                    POST /snippet?id=      canned REPL scripts
    POST   /repl?ms=                    body is code; pasted into the REPL, nothing written
    POST   /reset?mode=soft|hard        soft reboot, or a real reset (SWD when a probe is up)
    GET    /video?w=&fps= · /snapshot?w=  HDMI capture: live MJPEG · one JPEG
    GET    /bootloader/status           POST /bootloader/enter?method=repl|touch
    POST   /uf2?enter=&wait=            body is a .uf2
    GET    /ocd/status                  POST /ocd/start · /ocd/stop · /ocd/cmd · /ocd/flash
    POST   /shutdown                    clean exit
"""
import argparse
import ast
import atexit
import collections
import contextlib
import errno
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import posixpath
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

AGENT_VERSION = 8
LOG_KEEP = 512 * 1024              # chars of console history kept for /serial/tail
BUF_KEEP = 256 * 1024              # chars of unread console output kept for /serial/read
RELINK_EVERY = 2.0                 # s between looks for a board's port/drive under a new name
STARTED = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "devagent.json")
ARGS = None
TOKEN = None
TOKEN_FROM = None                             # where the token came from, for the startup line
BOARDS = {}                                   # id -> Board
BOARDS_LOCK = threading.Lock()                # held while BOARDS is read or changed (never nested)
SAVE_LOCK = threading.Lock()                  # one config writer at a time

try:
    import serial                             # pyserial
except ImportError:
    serial = None
try:
    import cpfs                               # MakerClassCZ/circuitpython-filesystem
except ImportError:
    cpfs = None

REPL_MARK = "--devagent--"                            # separates paste-mode echo from real output


def repl_done(text):
    """True once the board PRINTED the marker (on a line of its own - the paste-mode echo of
    print("...") carries the same word) and is back at its prompt: the script has run."""
    at = max(text.rfind(REPL_MARK + "\r"), text.rfind(REPL_MARK + "\n"))
    return at >= 0 and text[at:].rstrip().endswith(">>>")


def code_done(text):
    """True once CircuitPython says the program in code.py finished."""
    return "Code done running." in text
_FFMPEG_OK = None                                    # ffmpeg present? probed once
OCD_CFGS = {
    "rp2350": ("interface/cmsis-dap.cfg", "target/rp2350.cfg", "adapter speed 5000"),
    "rp2040": ("interface/cmsis-dap.cfg", "target/rp2040.cfg", "adapter speed 5000"),
}
AUTORUN = ("code.py", "main.py", "code.txt", "main.txt", "boot.py")
BOOTLOADER_MARKERS = ("RunMode.BOOTLOADER", "on_next_reset", "reset_to_bootloader")


def autorun_guard(name, body):
    """Refuse the boot loop: an auto-run file that asks for the bootloader re-enters it on
    every boot, and only a flash-erase UF2 gets you out. Returns why, or None if safe."""
    # the name the write will actually use: "code.py/" and "x/../code.py" are code.py too
    base = posixpath.basename(posixpath.normpath("/" + (name or "").replace("\\", "/")))
    if base.lower() not in AUTORUN:
        return None
    hits = [m for m in BOOTLOADER_MARKERS if m in body.decode("utf-8", "ignore")]
    if not hits:
        return None
    return ("refusing to write %s: it contains %s. On the next boot this drops the board into "
            "the bootloader, and because the file survives the flash it does so again every "
            "time - recoverable only with a flash-erase UF2. Use POST /bootloader/enter (REPL, "
            "nothing persisted), or pass force=1 if you really mean it." % (name, " + ".join(hits)))


# Small REPL programs kept at hand. They run through the console in paste mode and never
# touch the filesystem, so they are safe on a board mid-experiment. Drop your own *.py into
# a `snippets/` folder next to this file and they appear in the panel automatically.
# Every body is CircuitPython SOURCE, so the strings are raw: a plain "\n" in here would become a
# real newline and reach the board as an unterminated string literal (it did, in "info").
BUILTIN_SNIPPETS = {
    "info": ("Board info", r"""
import sys, os, gc, binascii
try:
    import microcontroller as mc
except ImportError:
    mc = None
import board as _b
d = {}
d["impl"] = "%s %s" % (sys.implementation.name,
                       ".".join(str(x) for x in sys.implementation.version if str(x)))
try: d["board"] = os.uname().machine
except Exception: pass
try: d["uid"] = binascii.hexlify(mc.cpu.uid).decode().upper()
except Exception: pass
try: d["cpu_mhz"] = round(mc.cpu.frequency / 1e6, 1)
except Exception: pass
try: d["temp_c"] = round(mc.cpu.temperature, 1)
except Exception: pass
try: d["reset"] = str(mc.cpu.reset_reason).split(".")[-1]
except Exception: pass
gc.collect()
d["heap_free"] = gc.mem_free()
try:
    st = os.statvfs("/")
    d["fs_free_kb"] = st[0] * st[3] // 1024
    d["fs_total_kb"] = st[0] * st[2] // 1024
except Exception: pass
try:
    with open("/boot_out.txt") as f: d["boot_out"] = f.read().strip().split("\n")[0].strip()
except Exception: pass
d["pins"] = len([p for p in dir(_b) if not p.startswith("_")])
try:
    import supervisor
    disp = supervisor.runtime.display
    d["display"] = "%dx%d" % (disp.width, disp.height) if disp else None
except Exception: pass
print("DEVAGENT_INFO " + repr(d))
"""),
    "i2c": ("I2C scan", r"""
import board, busio, time
tried = []
for name, mk in (("board.I2C()", lambda: board.I2C()),
                 ("STEMMA_I2C", lambda: board.STEMMA_I2C())):
    try:
        i2c = mk()
    except Exception as e:
        tried.append("%s -> %s" % (name, e)); continue
    while not i2c.try_lock():
        time.sleep(0.01)
    try:
        found = i2c.scan()
        print("%s: %d device(s): %s" % (name, len(found), ", ".join(hex(a) for a in found)))
    finally:
        i2c.unlock()
        try: i2c.deinit()
        except Exception: pass
    break
else:
    print("no I2C bus: " + " | ".join(tried))
"""),
    "modules": ("Frozen modules", "help('modules')\n"),
    "pins": ("Board pins", r"""
import board
names = sorted(p for p in dir(board) if not p.startswith("_"))
print("%d entries" % len(names))
print(", ".join(names))
"""),
    "mem": ("Memory", r"""
import gc
gc.collect()
free = gc.mem_free()
try: alloc = gc.mem_alloc()
except Exception: alloc = None
print("free %d B (%.1f KB)%s" % (free, free / 1024.0,
      "" if alloc is None else ", allocated %d B" % alloc))
"""),
    "storage": ("Storage", r"""
import os, storage
st = os.statvfs("/")
print("/ : %d KB free of %d KB, block %d" % (st[0]*st[3]//1024, st[0]*st[2]//1024, st[0]))
try:
    print("writable by CircuitPython:", not storage.getmount("/").readonly)
except Exception as e:
    print("mount info:", e)
print("root:", ", ".join(sorted(os.listdir("/"))))
"""),
}


def snippets():
    """Built-ins plus anything in ./snippets/*.py, so the user can add their own."""
    out = {k: {"id": k, "label": v[0], "code": v[1], "builtin": True}
           for k, v in BUILTIN_SNIPPETS.items()}
    d = os.path.join(HERE, "snippets")
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            if n.endswith(".py"):
                try:
                    with open(os.path.join(d, n)) as f:
                        code = f.read()
                except Exception:
                    continue
                sid = n[:-3]
                out[sid] = {"id": sid, "label": sid.replace("_", " "), "code": code,
                            "builtin": False}
    return out


class BadRequest(ValueError):
    """A request the caller got wrong (bad or missing parameter) - answered with 400."""


def qstr(q, key, default=None):
    """?key=value; required when no default is given."""
    v = (q.get(key) or [None])[0]
    if v is None or v == "":
        if default is None:
            raise BadRequest("?%s is required" % key)
        return default
    return v


def qint(q, key, default, lo=0, hi=None):
    """?key= as an int within lo..hi (a bad one used to be a traceback and no reply)."""
    raw = (q.get(key) or [None])[0]
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise BadRequest("?%s must be an integer, not %r" % (key, raw))
    if v < lo or (hi is not None and v > hi):
        raise BadRequest("?%s must be %d..%s" % (key, lo, "" if hi is None else hi))
    return v


def qfloat(q, key, default, lo=0.0, hi=None):
    raw = (q.get(key) or [None])[0]
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise BadRequest("?%s must be a number, not %r" % (key, raw))
    if v < lo or (hi is not None and v > hi) or v != v:
        raise BadRequest("?%s must be %g..%s" % (key, lo, "" if hi is None else hi))
    return v


def qflag(q, key, default=False):
    """?key=1 / ?key=0; an absent key is `default`, a present empty one is False."""
    v = (q.get(key) or [None])[0]
    return default if v is None else v not in ("0", "", "false", "no")


MS_MAX = 60000                                       # longest wait any ?ms= may ask for


class ConsoleError(RuntimeError):
    """The board's REPL cannot be reached: no port, port busy, or the write failed."""


class DriveMissing(Exception):
    """The board's drive is not there (unplugged, resetting, not mounted) or was refused."""


def drive_check(path):
    """Why `path` may not serve as a board's drive, or None when it may. POST /boards is
    reachable by anything that can reach the agent, so the drive must be a CircuitPython
    volume (boot_out.txt at its root, as circuitpy_drives() requires) and never a directory
    that holds the host's own files - the home directory, this program, Python, a root."""
    real = os.path.normcase(os.path.realpath(path))
    guarded = [os.path.expanduser("~"), HERE, sys.prefix]
    guarded.append(os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/")
    for g in guarded:
        g = os.path.normcase(os.path.realpath(g))
        try:
            if os.path.commonpath([real, g]) == real:
                return "%s contains the host's own files (%s) - not a board drive" % (path, g)
        except ValueError:                           # different Windows drives
            continue
    if not (ARGS and ARGS.any_path) and not os.path.isfile(os.path.join(path, "boot_out.txt")):
        return ("%s has no boot_out.txt, so it is not a CircuitPython drive "
                "(start with --any-path to serve a plain directory)" % path)
    return None


def normalize_drive_path(path):
    """`O:` means "the current directory on O:" to Windows; a drive is `O:\\`."""
    path = (path or "").strip()
    if not path:
        return None
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", path):
        path += "\\"
    return os.path.normpath(path)


class Board:
    """One board: its drive, its console, its probe. All per-board state lives here so the
    agent can serve several at once without them stepping on each other."""

    def __init__(self, id, label=None, path=None, port=None, baud=115200,
                 ocd=None, index=0, fs="auto", video=None, video_size=None, uid=None):
        if not isinstance(id, str) or not id.strip():
            raise ValueError("board id is required")
        self.id = id.strip()
        if not re.fullmatch(r"[\w.-]{1,40}", self.id):    # it lives in URLs and the config
            raise ValueError("board id %r: letters, digits, . _ - only" % self.id)
        self.label = str(label or self.id)
        self.path = str(path) if path else None
        self.port = str(port) if port else None
        try:
            self.baud = int(baud or 115200)
            self.index = int(index or 0)             # decides this board's OpenOCD ports
        except (TypeError, ValueError):
            raise ValueError("baud and index must be integers")
        if not 300 <= self.baud <= 4000000:
            raise ValueError("baud %d is not a serial speed" % self.baud)
        if not 0 <= self.index < 64:
            raise ValueError("index must be 0..63 (it picks the OpenOCD ports)")
        self.ocd = ocd or None                       # None | "rp2040" | "rp2350"
        if self.ocd is not None and self.ocd not in OCD_CFGS:
            raise ValueError("ocd must be one of %s" % sorted(OCD_CFGS))
        self.fs = fs or "auto"
        if self.fs not in ("auto", "msc", "repl"):
            raise ValueError("fs must be auto, msc or repl")
        self.video = str(video) if video else None   # HDMI capture: dshow name / /dev/videoN / "test"
        self.video_size = str(video_size) if video_size else None   # capture mode, e.g. "640x480"
        if self.video_size and not re.fullmatch(r"\d+x\d+", self.video_size):
            raise ValueError("video_size must look like 640x480")
        self.uid = (str(uid).upper() or None) if uid else None   # boot_out.txt UID == USB serial
        self.dead = False                            # replaced or removed: reader exits, no reopen
        self._uid_seen = (None, 0.0, None)           # (path, when, uid on it) - see drive_uid()
        self.port_present = None                     # last relink() verdict: None = not checked yet
        self._relink_at = 0.0
        self._relink_lock = threading.Lock()
        self.buf = deque()                           # unread console output (drain); BUF_KEEP chars
        self.buf_len = 0
        self.buf_lock = threading.Lock()
        self.log = []                                # everything said, for cursor reads (tail)
        self.log_len = 0                             # chars in log
        self.log_base = 0                            # absolute offset of log[0]
        self.taps = []                               # extra readers; see tap()
        self.exclusive = 0                           # >0: a script owns the stream, console pauses
        self.wake = threading.Condition(self.buf_lock)   # "new console output"; shares the buffer
                                                     # lock so a check-then-wait cannot miss a wake-up
        self.repl_lock = threading.RLock()
        self.ser = None
        self.ser_lock = threading.RLock()
        self.ocd_proc = None
        self.info = None                             # cached board identity (see get_info)
        self._reader = None
        # NB: the reader is NOT started here. __init__ runs while `BOARDS[id] = Board(...)` is
        # still evaluating, so the board is not registered yet and the thread would see itself
        # as forgotten and exit. Callers start it after registering - see add_board().

    # ---- serialisation ----
    def to_json(self):
        return {"id": self.id, "label": self.label, "path": self.path, "port": self.port,
                "baud": self.baud, "ocd": self.ocd, "fs": self.fs, "video": self.video,
                "video_size": self.video_size, "index": self.index, "uid": self.uid}

    def status(self):
        self.relink()
        return dict(self.to_json(), drive=self.drive(), serial_open=self.serial_ok(),
                    drive_problem=None if self.drive() else self.drive_problem(),
                    port_present=self.port_present, reader_alive=self.reader_alive(),
                    openocd=self.ocd_running(), ocd_ports=self.ocd_ports(),
                    fs_mode=self.fs_mode(), in_bootloader=uf2_volume() is not None,
                    ffmpeg=have_ffmpeg())

    # ---- drive ----
    def drive(self):
        """The board's drive when it is there, is something a board may be pointed at
        (drive_check - never the host's own directories, whatever a request named), and
        still carries THIS board's UID: after a re-plug the same letter can be another board,
        and a write must not land there."""
        if not self.path or not os.path.isdir(self.path) or drive_check(self.path):
            return None
        if self.uid and self.drive_uid() not in (None, self.uid):
            return None
        return self.path

    def drive_uid(self):
        """UID in boot_out.txt on self.path (None when it has none), re-read at most once a
        second - drive() is asked per file operation."""
        now = time.time()
        if self._uid_seen[0] != self.path or now - self._uid_seen[1] > 1.0:
            self._uid_seen = (self.path, now, (drive_identity(self.path) or {}).get("uid"))
        return self._uid_seen[2]

    def drive_problem(self):
        """Why drive() is None right now: absent, or refused (with the reason)."""
        if not self.path:
            return "no drive configured"
        if not os.path.isdir(self.path):
            return "drive not found: %s" % self.path
        why = drive_check(self.path)
        if why:
            return why
        if self.uid and self.drive_uid() not in (None, self.uid):
            return "%s now belongs to another board (UID %s, this one is %s)" % (
                self.path, self.drive_uid(), self.uid)
        return None

    def fs_mode(self):
        if self.fs in ("msc", "repl"):
            return self.fs
        return "msc" if self.drive() else ("repl" if (cpfs and self.port) else "msc")

    def join(self, name):
        """Host path of a file on the drive. Confined twice: lexically (no `..` out of the
        drive) and through symlinks (the real path must stay under the real drive)."""
        drive = self.drive()
        if drive is None:
            raise DriveMissing(self.drive_problem())
        root = os.path.realpath(drive)
        name = (name or "").replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(root, name))
        for candidate in (full, os.path.realpath(full)):
            try:
                inside = os.path.commonpath([os.path.normcase(candidate),
                                             os.path.normcase(root)]) == os.path.normcase(root)
            except ValueError:                       # different Windows drives
                inside = False
            if not inside:
                raise ValueError("path escapes the drive")
        return full

    # ---- identity ----
    def relink(self):
        """Follow the board across a COM-port / drive-letter change.

        The UID CircuitPython prints in boot_out.txt is also the board's USB serial number, so
        the board is recognised on whatever port and drive the host handed out this time (another
        USB socket, a second CIRCUITPY plugged in first, a reboot). The UID is learnt on first
        contact and saved; a port or drive that later vanishes - or whose name the OS gave to a
        different board - is re-pointed at the one carrying our UID. Runs from status() and the
        console reader, at most every RELINK_EVERY seconds; the port list is asked of the OS only
        while the console is not open (an open port cannot have been renamed under us)."""
        now = time.time()
        if now - self._relink_at < RELINK_EVERY or not self._relink_lock.acquire(False):
            return []
        try:
            self._relink_at = now
            moved, changed = [], False
            self._uid_seen = (None, 0.0, None)                     # look afresh, not at the cache
            drive_uid = self.drive_uid() if self.path and os.path.isdir(self.path) else None
            ports = None
            if self.port and serial is not None and (self.ser is None or self.uid is None):
                ports = {p.device: p for p in comports()}
            if self.uid is None:                                   # first contact: learn who this is
                here = ports.get(self.port) if ports else None
                learnt = drive_uid or (here.serial_number if here else None)
                if learnt:
                    self.uid, changed = learnt.upper(), True
            if self.uid:
                with BOARDS_LOCK:
                    others = [b for b in BOARDS.values() if b is not self]
                if self.path and drive_uid != self.uid:            # gone, or the letter went elsewhere
                    taken = {b.path for b in others}
                    for cand in circuitpy_drives():
                        if cand != self.path and cand not in taken \
                                and (drive_identity(cand) or {}).get("uid") == self.uid:
                            moved.append("drive %s -> %s" % (self.path, cand))
                            self.path = cand
                            break
                if ports is not None:
                    here = ports.get(self.port)
                    here_uid = (here.serial_number or "").upper() if here else None
                    if here_uid != self.uid:                       # gone, or the name went elsewhere
                        taken = {b.port for b in others}
                        for dev, p in ports.items():
                            if dev != self.port and dev not in taken \
                                    and (p.serial_number or "").upper() == self.uid:
                                moved.append("serial %s -> %s" % (self.port, dev))
                                self.close_serial()
                                self.port = dev
                                break
            if ports is not None:                                  # present = there AND ours
                here = ports.get(self.port)
                here_uid = (here.serial_number or "").upper() if here else None
                self.port_present = here is not None and (not self.uid or not here_uid
                                                          or here_uid == self.uid)
            elif self.ser is not None:
                self.port_present = True
            if moved or changed:
                try:
                    save_config()
                except OSError as e:
                    self.note("could not save devagent.json: %s" % e)
            for m in moved:
                self.note("%s (same board, UID %s)" % (m, self.uid))
                print("[%s] %s" % (self.id, m))
            return moved
        finally:
            self._relink_lock.release()

    def note(self, msg):
        """A grey line from the agent in the console history (not into taps: a scripted run
        must not find it in its own output)."""
        self._ingest("\x1b[90m[devagent] %s\x1b[0m\r\n" % msg, to_taps=False)

    # ---- serial ----
    def serial_ok(self):
        with self.ser_lock:
            return self.open_serial() is not None

    def open_serial(self):
        if serial is None or not self.port or self.dead:
            return None
        if self.ser is not None and getattr(self.ser, "is_open", False):
            return self.ser
        try:
            # short timeout on purpose: the read holds the port lock, so a long one makes every
            # keystroke wait for it - that is felt directly as typing lag in the web console
            self.ser = serial.Serial(self.port, self.baud, timeout=0.02, write_timeout=2)
        except Exception:
            self.ser = None
        return self.ser

    def close_serial(self):
        with self.ser_lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def start_reader(self):
        if self._reader is None and self.port and serial is not None:
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()

    def _read_loop(self):
        """A board prints whether or not anyone is listening; keep the tail."""
        while not self.dead:
            try:
                self._read_once()
            except Exception as e:                   # a dead reader would look like a silent
                self.note("console reader: %r" % e)  # board; log it and carry on
                time.sleep(0.5)

    def _read_once(self):
        with self.ser_lock:
            s = self.open_serial()
            data = b""
            if s is not None:
                try:
                    n = s.in_waiting
                    data = s.read(n if n else 1)
                except Exception:
                    self.ser = None
        if data:
            self._ingest(data.decode("utf-8", "replace"))
        elif s is None:
            self.relink()                        # the port may be back under another name
            time.sleep(0.5)                      # no port: don't spin on a failing open()
        else:
            time.sleep(0.005)                    # the blocking read above does the pacing

    def reader_alive(self):
        return self._reader is not None and self._reader.is_alive()

    def _ingest(self, text, to_taps=True):
        with self.buf_lock:
            if not self.exclusive:                   # a script's own echo is noise in the console
                self.buf.append(text)
                self.buf_len += len(text)
                while self.buf_len > BUF_KEEP and len(self.buf) > 1:
                    self.buf_len -= len(self.buf.popleft())
            if to_taps:
                for t in self.taps:                  # every listener gets its own copy
                    t.append(text)
            self.log.append(text)                    # the cursor log keeps it either way
            self.log_len += len(text)
            while self.log_len > LOG_KEEP and len(self.log) > 1:
                old = self.log.pop(0)
                self.log_len -= len(old)
                self.log_base += len(old)
            self.wake.notify_all()                   # a waiting console read returns at once

    @contextlib.contextmanager
    def tap(self, exclusive=False):
        """A private copy of everything the board says from here on.

        The console poll and a scripted REPL run are two consumers of one stream, and drain()
        empties it - so whoever asked first won and the other got nothing. That is why pressing
        'I2C scan' printed only its own header: the panel's 500 ms poll had already taken the
        answer. A tap leaves the console buffer alone, so both see the whole thing."""
        sink = []
        with self.buf_lock:
            self.taps.append(sink)
            if exclusive:
                self.exclusive += 1
        try:
            yield sink
        finally:
            with self.buf_lock:
                if sink in self.taps:
                    self.taps.remove(sink)
                if exclusive:
                    self.exclusive = max(0, self.exclusive - 1)

    def collect(self, sink, ms, done=None):
        """Read a tap for `ms`; return earlier only when `done(text)` says the run is over.
        (It used to stop after 0.4 s of silence, which cut off `print; sleep(1); print` after
        the first line and handed a slow script's output to whoever asked next.)"""
        deadline, seen = time.time() + ms / 1000.0, 0
        while True:
            with self.wake:
                if len(sink) > seen:
                    seen = len(sink)
                    if done and done("".join(sink)):
                        break
                left = deadline - time.time()
                if left <= 0:
                    break
                self.wake.wait(min(0.25, left))
        with self.buf_lock:
            return "".join(sink)

    def _take_locked(self):
        got = "".join(self.buf)
        self.buf.clear()
        self.buf_len = 0
        return got

    def take(self):
        with self.buf_lock:
            return self._take_locked()

    def tail(self, frm, ms, stop=None):
        """Console output from absolute offset `frm` on, without consuming anything: the reply
        carries `next`, the offset to ask for next time. Long-polls up to `ms` for new output.
        frm=-1 = start at the current end (a cursor for "from now on"). Exists because
        /serial/read (drain) is one shared stream - a test runner polling it while the panel
        was open got a tenth of what the board printed, whoever asked first took the burst.
        `stop()` is asked every quarter second; True ends the wait early (the client left)."""
        with self.buf_lock:
            end = self.log_base + self.log_len
        if frm < 0:
            return {"next": end, "text": ""}
        deadline = time.time() + ms / 1000.0
        while True:
            with self.wake:
                end = self.log_base + self.log_len
                if end > frm:
                    start = max(frm, self.log_base)      # older than we keep: skip, flag the gap
                    text = "".join(self.log)[start - self.log_base:]
                    return {"next": end, "text": text, "gap": start != frm}
                left = deadline - time.time()
                if left <= 0:
                    return {"next": end, "text": ""}
                self.wake.wait(min(0.25, left))
            if stop and stop():
                return {"next": end, "text": ""}

    def drain(self, ms, coalesce=0.015, stop=None):
        """Console output, long-poll style: return the moment anything is there (after a blink to
        gather the rest of the burst), otherwise wait up to `ms`. ms=0 = take what is buffered and
        return now. Waiting out the full window even when data arrived immediately is what made
        the browser console feel laggy - the echo sat in the reply for up to a quarter second.
        `stop()` is asked every quarter second; True ends the wait early WITHOUT taking anything -
        the panel aborts its long-poll on every tab switch, and a handler left waiting here would
        otherwise swallow the next 15 s of console output on behalf of a browser that is gone."""
        out = self.take()
        if out or ms <= 0:
            return out
        deadline = time.time() + ms / 1000.0
        while True:
            with self.wake:
                if not self.buf:
                    left = deadline - time.time()
                    if left <= 0:
                        return ""
                    self.wake.wait(min(0.25, left))
                out = self._take_locked()
            if out:
                time.sleep(coalesce)                 # let the rest of the burst land
                return out + self.take()
            if stop and stop():
                return ""

    def send(self, data):
        with self.ser_lock:
            s = self.open_serial()
            if s is None:
                return False
            try:
                s.write(data)
                s.flush()
                return True
            except Exception:
                self.ser = None
                return False

    def send_or_raise(self, data, slice_size=256):
        """send() for scripted runs: a failure is an error, not a silent half-paste. Written in
        slices with the port lock released in between, so the reader can take the echo the
        board sends back - a board whose echo nobody reads stops reading itself."""
        for at in range(0, len(data), slice_size):
            if not self.send(data[at:at + slice_size]):
                raise ConsoleError("console write to %s failed (port gone?)" % self.port)
            if at + slice_size < len(data):
                time.sleep(0.005)

    # ---- running code in the REPL (never touches the filesystem) ----
    def console_error(self):
        """Why the REPL is unreachable, or None. Every REPL-driven action asks first, so a
        board without a console says so instead of silently doing nothing."""
        if serial is None:
            return "pyserial is not installed on the agent host (pip install pyserial)"
        if not self.port:
            return "no serial console configured for board %r" % self.id
        with self.ser_lock:
            if self.open_serial() is None:
                return "cannot open %s (in use by another program?)" % self.port
        return None

    def run_repl(self, code, ms=6000):
        """Paste-mode a snippet into the REPL and return what it printed. Paste mode is
        what keeps indentation intact - sending a block line by line runs half of it.
        Reads through a tap, so the console keeps showing the run as it happens."""
        err = self.console_error()
        if err:
            raise ConsoleError(err)
        with self.repl_lock:                              # two scripts at once would interleave
            with self.tap(exclusive=True) as sink:
                self.send_or_raise(b"\x03")
                time.sleep(0.25)
                self.send_or_raise(b"\x05")               # Ctrl-E: paste mode
                time.sleep(0.15)
                marked = 'print("%s")\n%s' % (REPL_MARK, code)
                self.send_or_raise(marked.replace("\r\n", "\n").replace("\n", "\r").encode())
                time.sleep(0.1)
                self.send_or_raise(b"\x04")               # Ctrl-D: run it
                out = self.collect(sink, ms, done=repl_done)
        # Paste mode echoes every line back, so the raw capture is the script itself followed by
        # its output. The marker is printed AFTER the echo, so the last one splits the two.
        if REPL_MARK in out:
            out = out.split(REPL_MARK)[-1]
        out = out.replace("\r\n", "\n").replace("\r", "\n").strip()
        while out.endswith(">>>"):                    # the prompt the REPL leaves behind
            out = out[:-3].rstrip()
        return out

    def get_info(self, refresh=False):
        """Board identity, read once and kept for the life of the agent - it does not change
        while the board is plugged in, and re-probing would interrupt whatever is running."""
        if self.info is not None and not refresh:
            return self.info
        err = self.console_error()
        if err:
            return {"error": err}
        out = self.run_repl(snippets()["info"]["code"], 6000)
        info = {"raw_tail": out[-400:]}
        for line in out.splitlines():
            if line.startswith("DEVAGENT_INFO "):
                try:
                    info = ast.literal_eval(line[len("DEVAGENT_INFO "):])   # never eval():
                    if not isinstance(info, dict):                              # the board wrote it
                        raise ValueError("not a dict")
                except Exception as e:
                    info = {"error": "the DEVAGENT_INFO line is not plain data (%s)"
                                     % type(e).__name__, "raw_tail": out[-400:]}
                break
        else:
            info["error"] = "no reply - is the board at a REPL prompt? (try a soft reboot)"
        info["probed_at"] = int(time.time())
        self.info = info
        return info

    def reset(self, mode="soft"):
        """soft = Ctrl-D (re-runs code.py). hard = a real reset: over SWD when a probe is
        running, otherwise microcontroller.reset() from the REPL."""
        err = self.console_error() if (mode == "soft" or not self.ocd_running()) else None
        if err:
            return {"mode": mode, "error": err}
        if mode == "soft":
            self.drain(150)
            self.send(b"\x03")
            time.sleep(0.3)
            self.send(b"\x04")
            return {"mode": "soft"}
        if self.ocd_running():
            try:
                return {"mode": "hard", "via": "swd", "output": self.ocd_tcl("reset run")}
            except Exception as e:
                return {"mode": "hard", "via": "swd", "error": repr(e)}
        self.send(b"\x03")
        time.sleep(0.25)
        self.send(b"import microcontroller; microcontroller.reset()\r\n")
        self.info = None                                  # identity is re-probed after a reset
        return {"mode": "hard", "via": "repl"}

    # ---- files: one API, two backends ----
    def _repl_fs(self):
        return ReplFs(self)

    def list(self, sub, recursive):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                out, stack = [], ["/" + (sub or "").strip("/")]
                while stack:
                    d = stack.pop(0)
                    for name in f.listdir(d):
                        p = d.rstrip("/") + "/" + name
                        isdir = f.isdir(p)
                        out.append({"name": p.lstrip("/"), "dir": isdir,
                                    "size": 0 if isdir else f.getsize(p)})
                        if isdir and recursive:
                            stack.append(p)
                return out
        root = self.join(sub)
        if not os.path.isdir(root):
            raise FileNotFoundError("no such directory: %s" % sub)
        out = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, self.drive()).replace("\\", "/")
                rel = "" if rel == "." else rel + "/"
                for d in sorted(dirnames):
                    out.append({"name": rel + d, "size": 0, "dir": True})
                for n in sorted(filenames):
                    out.append({"name": rel + n, "dir": False,
                                "size": os.path.getsize(os.path.join(dirpath, n))})
        else:
            names = sorted(os.listdir(root), key=lambda n: (not os.path.isdir(os.path.join(root, n)), n))
            for n in names:
                full = os.path.join(root, n)
                isdir = os.path.isdir(full)
                out.append({"name": n, "size": 0 if isdir else os.path.getsize(full), "dir": isdir})
        return out

    def read(self, name):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                return f.read_file("/" + name.lstrip("/"))
        p = self.join(name)
        if not os.path.isfile(p):
            raise FileNotFoundError("no such file: %s" % name)
        with open(p, "rb") as fh:
            return fh.read()

    def write(self, name, body):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                parent = "/" + "/".join(name.strip("/").split("/")[:-1])
                if parent != "/":
                    try:
                        f.makedirs(parent)
                    except Exception:
                        pass
                f.write_file("/" + name.lstrip("/"), body)
                back = f.read_file("/" + name.lstrip("/"))
            return {"size": len(back), "sha256": hashlib.sha256(back).hexdigest(),
                    "matches_sent": back == body, "via": "repl"}
        p = self.join(name)
        parent = os.path.dirname(p)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        with open(p, "rb") as f:
            ondisk = f.read()
        return {"size": len(ondisk), "sha256": hashlib.sha256(ondisk).hexdigest(),
                "matches_sent": ondisk == body}

    def delete(self, name):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                f.remove("/" + name.lstrip("/"))
            return
        p = self.join(name)
        if not os.path.isfile(p):
            raise FileNotFoundError("no such file: %s" % name)
        os.remove(p)

    def mkdir(self, name):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                f.makedirs("/" + name.strip("/"))
            return
        os.makedirs(self.join(name), exist_ok=True)

    def rmdir(self, name, recursive):
        if self.fs_mode() == "repl":
            with self._repl_fs() as f:
                (f.rmtree if recursive else f.rmdir)("/" + name.strip("/"))
            return
        p = self.join(name)
        if not os.path.isdir(p):
            raise FileNotFoundError("no such directory: %s" % name)
        shutil.rmtree(p) if recursive else os.rmdir(p)

    # ---- debug probe (own ports, so boards do not collide) ----
    def ocd_ports(self):
        return {"gdb": 3333 + self.index * 10, "telnet": 4444 + self.index * 10,
                "tcl": 6666 + self.index * 10}

    def ocd_running(self):
        return self.ocd_proc is not None and self.ocd_proc.poll() is None

    def ocd_start(self, cfg=None):
        if self.ocd_running():
            return {"running": True, "pid": self.ocd_proc.pid, "already": True,
                    "ports": self.ocd_ports()}
        name = cfg or self.ocd
        spec = OCD_CFGS.get(name or "")
        if spec is None:
            return {"error": "no OpenOCD config for this board (set one: %s)" % sorted(OCD_CFGS)}
        if shutil.which(ARGS.openocd) is None:
            return {"error": "openocd not found on PATH (%s)" % ARGS.openocd}
        iface, target, speed = spec
        p = self.ocd_ports()
        cmd = [ARGS.openocd, "-f", iface, "-f", target, "-c", speed, "-c", "bindto %s" % ARGS.ocd_bind,
               "-c", "gdb_port %d" % p["gdb"], "-c", "telnet_port %d" % p["telnet"],
               "-c", "tcl_port %d" % p["tcl"]]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return {"error": repr(e)}
        time.sleep(1.2)
        if proc.poll() is not None:
            return {"error": "openocd exited immediately (rc=%s) - probe attached? target powered?"
                    % proc.returncode}
        self.ocd_proc = proc
        return {"running": True, "pid": proc.pid, "cfg": name, "ports": p}

    def ocd_stop(self):
        p, self.ocd_proc = self.ocd_proc, None
        if p is None or p.poll() is not None:
            return {"running": False}
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        return {"stopped": True}

    def ocd_tcl(self, cmd, timeout=120):
        s = socket.create_connection(("127.0.0.1", self.ocd_ports()["tcl"]), timeout=timeout)
        s.settimeout(timeout)
        try:
            s.sendall(cmd.encode() + b"\x1a")
            buf = b""
            while not buf.endswith(b"\x1a"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return buf[:-1].decode("utf-8", "replace")
        finally:
            s.close()

    def ocd_telnet(self, cmds, timeout=300):
        s = socket.create_connection(("127.0.0.1", self.ocd_ports()["telnet"]), timeout=timeout)
        s.settimeout(timeout)
        out = b""
        time.sleep(1.0)
        try:
            try:
                out += s.recv(65536)
            except Exception:
                pass
            for c in cmds:
                s.sendall(c.encode() + b"\n")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    out += chunk
                    if out.rstrip().endswith(b">"):
                        break
        finally:
            s.close()
        return out.decode("utf-8", "replace")

    def ocd_flash(self, body, verify=True, reset=True):
        """Firmware arrives over HTTP because OpenOCD cannot see the caller's filesystem."""
        suffix = ".elf" if body[:4] == b"\x7fELF" else ".bin"
        fd, path = tempfile.mkstemp(prefix="devagent-fw-", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        try:
            verb = "program %s%s%s" % (path.replace("\\", "/"), " verify" if verify else "",
                                       " reset" if reset else "")
            if self.ocd_running():
                out = self.ocd_telnet(["halt", verb])
            else:
                spec = OCD_CFGS.get(self.ocd or "")
                if spec is None:
                    return {"error": "no OpenOCD config for this board, and none running"}
                if shutil.which(ARGS.openocd) is None:
                    return {"error": "openocd not found on PATH (%s)" % ARGS.openocd}
                iface, target, speed = spec
                r = subprocess.run([ARGS.openocd, "-f", iface, "-f", target, "-c", speed,
                                    "-c", "%s exit" % verb], capture_output=True, timeout=600)
                out = (r.stdout + r.stderr).decode("utf-8", "replace")
            return {"ok": ("Verified OK" in out) or ("** Programming Finished **" in out),
                    "bytes": len(body), "output": out[-4000:]}
        except Exception as e:
            return {"error": repr(e)}
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    # ---- bootloader ----
    def bootloader_enter(self, method="repl", timeout=20.0):
        vol = uf2_volume()
        if vol:
            return {"already": True, "volume": vol, "info": uf2_info(vol)}
        if method in ("repl", "auto"):
            self.send(b"\x03")
            time.sleep(0.3)
            self.send(b"\r\n")
            time.sleep(0.2)
            self.send(b"import microcontroller\r\n")
            time.sleep(0.2)
            self.send(b"microcontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\r\n")
            time.sleep(0.2)
            self.send(b"microcontroller.reset()\r\n")
        touch_error = None
        if method in ("touch", "auto") and serial is not None and self.port:
            with self.ser_lock:                       # keeps the reader from reopening the port
                self.close_serial()                   # in between: ours must be the LAST close,
                try:                                  # or DTR never drops and nothing happens
                    s = serial.Serial(self.port, 1200)
                    s.dtr = False
                    time.sleep(0.2)
                    s.close()
                except Exception as e:
                    touch_error = repr(e)
        vol = wait_for(uf2_volume, timeout)
        return {"volume": vol, "info": uf2_info(vol) if vol else [],
                "touch_error": touch_error,
                "error": None if vol else "no UF2 volume appeared within %.0fs" % timeout}


class ReplFs:
    """cpfs over a board's console port, borrowed for one operation and handed back."""

    def __init__(self, board):
        self.board = board

    def __enter__(self):
        if cpfs is None:
            raise RuntimeError("cpfs.py not found - needed for REPL file access "
                               "(MakerClassCZ/circuitpython-filesystem)")
        self.board.ser_lock.acquire()
        try:
            self.board.close_serial()
            self.repl = cpfs.SerialREPL(self.board.port, self.board.baud)
            self.repl.connect()
            return cpfs.CircuitPythonFS(self.repl)
        except BaseException:                         # a lock left held here froze every later
            self.board.ser_lock.release()             # console read and write
            raise

    def __exit__(self, *exc):
        try:
            self.repl.disconnect()
        except Exception:
            pass
        finally:
            self.board.ser_lock.release()
        return False


# ------------------------------------------------------------- bootloader ----
def host_volumes():
    """Where a board's drive can show up: drive letters on Windows; /Volumes/X on macOS;
    /media/X, /run/media/X and one level below them (/media/<user>/CIRCUITPY) on Linux."""
    if os.name == "nt":
        import string
        return [r for r in ("%s:\\" % c for c in string.ascii_uppercase) if os.path.isdir(r)]
    vols = []
    for base in ("/media", "/run/media", "/Volumes", os.path.expanduser("~/media")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            vols.append(p)
            try:
                vols.extend(os.path.join(p, sub) for sub in sorted(os.listdir(p))
                            if os.path.isdir(os.path.join(p, sub)))
            except OSError:
                pass
    return vols


def drive_identity(root):
    """What boot_out.txt says about the board on a drive - {"name", "board", "board_id", "uid"} -
    or None when it is not a CircuitPython drive. The UID is the string CircuitPython also uses
    as its USB serial number, which is what lets a board be recognised after a port or drive
    letter change (Board.relink)."""
    try:
        with open(os.path.join(root, "boot_out.txt"), "r", errors="replace") as f:
            lines = [ln.strip() for ln in f.read(1024).splitlines() if ln.strip()]
    except OSError:
        return None
    name = lines[0] if lines else ""
    ident = {"name": name, "board": name.split(";", 1)[1].strip() if ";" in name else name,
             "board_id": None, "uid": None}
    for ln in lines[1:]:
        key, _, val = ln.partition(":")
        if key == "Board ID":
            ident["board_id"] = val.strip()
        elif key == "UID":
            ident["uid"] = val.strip().upper() or None
    return ident


def circuitpy_drives():
    return [v for v in host_volumes() if os.path.isfile(os.path.join(v, "boot_out.txt"))]


def comports():
    """pyserial's port list, or nothing when pyserial is missing - never an exception."""
    try:
        from serial.tools import list_ports
        return list(list_ports.comports())
    except Exception:
        return []


def uf2_volume():
    """The mounted UF2 bootloader volume, identified by INFO_UF2.TXT (host-wide, not per board:
    only one board can sit in the bootloader at a time anyway)."""
    for root in host_volumes():
        try:
            if os.path.isfile(os.path.join(root, "INFO_UF2.TXT")):
                return root
        except Exception:
            continue
    return None


def uf2_info(vol):
    try:
        with open(os.path.join(vol, "INFO_UF2.TXT"), "r", errors="replace") as f:
            return [ln.strip() for ln in f if ln.strip()][:6]
    except Exception:
        return []


def wait_for(fn, timeout=20.0, step=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def uf2_write(body, vol):
    """The board reboots the moment the last block lands, so the volume disappears under us:
    a write error after a full write is the normal ending, not a failure."""
    dest = os.path.join(vol, "firmware.uf2")
    written = 0
    try:
        with open(dest, "wb") as f:
            f.write(body)
            written = len(body)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception as e:
        if written:
            return {"ok": True, "bytes": written,
                    "note": "volume vanished mid-copy (%s) - that is what a successful UF2 "
                            "flash looks like" % type(e).__name__}
        return {"ok": False, "error": repr(e)}
    return {"ok": True, "bytes": written}


# --------------------------------------------------------------- registry ----
def load_config():
    global TOKEN, TOKEN_FROM
    cfg = {}
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG) as f:
                cfg = json.load(f)
        except Exception as e:
            print("config unreadable (%r) - starting empty" % e)
    # --token wins, and an explicit empty one (--token "") clears a persisted token
    TOKEN = ARGS.token if ARGS.token is not None else cfg.get("token")
    TOKEN = TOKEN or None
    TOKEN_FROM = "--token / DEVAGENT_TOKEN" if ARGS.token else "devagent.json"
    if ARGS.token is not None and TOKEN != (cfg.get("token") or None):
        save_config()                            # a new or cleared token replaces the remembered one
    for i, b in enumerate(cfg.get("boards", [])):
        b.setdefault("index", i)
        try:
            BOARDS[b["id"]] = Board(**b)
            BOARDS[b["id"]].start_reader()
        except Exception as e:
            print("skipping board %r: %r" % (b.get("id"), e))
    return cfg


def save_config():
    """Atomic rewrite of devagent.json. Serialised: every board's first relink() saves, and
    two writers sharing one temp name lost the file (FileNotFoundError on the replace)."""
    with SAVE_LOCK:
        with BOARDS_LOCK:
            data = {"token": TOKEN, "boards": [b.to_json() for b in BOARDS.values()]}
        fd, tmp = tempfile.mkstemp(prefix="devagent.json.", dir=HERE)   # mode 600: the token
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def add_board(spec):
    """spec: dict from the UI/CLI. Re-adding an id edits it in place: keys left out keep their
    value, an explicit null clears one - so {"id": "jam", "port": "COM7"} is a complete edit."""
    bid = (spec.get("id") or "").strip()
    if not bid:
        raise ValueError("board id is required")
    if spec.get("path") is not None:
        spec["path"] = normalize_drive_path(spec["path"])
        if spec["path"] and os.path.isdir(spec["path"]):
            why = drive_check(spec["path"])
            if why:
                raise ValueError(why)
    with BOARDS_LOCK:
        old = BOARDS.get(bid)
        if old:
            spec = dict(old.to_json(), **spec)
            if spec.get("port") != old.port or spec.get("path") != old.path:
                spec["uid"] = None                   # pointed at other hardware: learn its UID afresh
        idx = spec.get("index")
        if idx is None:
            used = {b.index for b in BOARDS.values() if b is not old}
            idx = next(i for i in range(64) if i not in used)
        new = Board(id=bid, label=spec.get("label"), path=spec.get("path"),
                    port=spec.get("port"), baud=spec.get("baud", 115200),
                    ocd=spec.get("ocd"), fs=spec.get("fs", "auto"),
                    video=spec.get("video"), video_size=spec.get("video_size"),
                    index=idx, uid=spec.get("uid"))   # raises on a bad value: old stays as it was
        BOARDS[bid] = new
    if old:
        retire(old)
    new.start_reader()                               # only now: the board is registered
    save_config()
    return new


def shutdown_all():
    """Leave nothing behind: every openocd, every console, every ffmpeg. Runs on /shutdown,
    SIGTERM, Ctrl-C and at exit - a killed agent used to leave openocd holding the probe
    and ffmpeg holding the capture card until someone found them in the process list."""
    with BOARDS_LOCK:
        boards = list(BOARDS.values())
    for b in boards:
        try:
            b.ocd_stop()
            b.close_serial()
        except Exception:
            pass
    with CAPTURES_LOCK:
        caps = list(CAPTURES.values())
    for cap in caps:
        cap.kill()


def retire(board):
    """A board taken out of BOARDS: its reader must stop and never reopen the port."""
    board.dead = True
    board.close_serial()
    board.ocd_stop()


def drop_board(bid):
    with BOARDS_LOCK:
        b = BOARDS.pop(bid, None)
    if b:
        retire(b)
    save_config()
    return b is not None


def boards_ordered():
    with BOARDS_LOCK:
        return sorted(BOARDS.values(), key=lambda b: (b.index, b.id))


def pick_board(q):
    """?board=id, or the only one configured. Never guesses between several."""
    bid = (q.get("board") or [None])[0]
    with BOARDS_LOCK:
        if bid:
            return BOARDS.get(bid)
        if len(BOARDS) == 1:
            return next(iter(BOARDS.values()))
    return None


def have_ffmpeg():
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        try:
            subprocess.run([ARGS.ffmpeg, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
            _FFMPEG_OK = True
        except Exception:
            _FFMPEG_OK = False
    return _FFMPEG_OK


def parse_dshow(text):
    """Device names out of `ffmpeg -list_devices`. TWO formats have to work: up to ffmpeg 6 the
    list is grouped under 'DirectShow video devices' headers, from ffmpeg 7 each line carries its
    own '(video)' / '(audio)' tag and the headers are gone. Parsing only the old one is why a
    perfectly good capture card showed up as 'no devices'.

    Windows can also give two cards the SAME friendly name; the alternative name is unique, so it
    becomes the value whenever a name is not."""
    devs, video_section, last = [], False, None
    for line in (text or "").splitlines():
        m = re.search(r'Alternative name\s+"([^"]+)"', line)
        if m:
            if last is not None:
                last["alt"] = m.group(1)
            continue
        m = re.search(r'"([^"]+)"\s*\((video|audio)\)', line)          # ffmpeg >= 7
        if m:
            last = {"name": m.group(1), "alt": None} if m.group(2) == "video" else None
            if last:
                devs.append(last)
            continue
        if "video devices" in line:
            video_section, last = True, None
            continue
        if "audio devices" in line:
            video_section, last = False, None
            continue
        m = re.search(r'"([^"]+)"', line)                                # ffmpeg <= 6, in a section
        if m and video_section:
            last = {"name": m.group(1), "alt": None}
            devs.append(last)
        elif not m:
            last = None
    seen = [d["name"] for d in devs]
    out = []
    for d in devs:
        dup = seen.count(d["name"]) > 1
        out.append(d["alt"] if (dup and d["alt"]) else d["name"])
    return out


def video_probe():
    """Capture devices plus WHY the list is empty - 'nothing offered' with no reason is the
    single most confusing thing this panel can do."""
    global _FFMPEG_OK
    _FFMPEG_OK = None                                # re-probe: ffmpeg may have been installed
    if not have_ffmpeg():
        return {"devices": [], "ffmpeg": False,
                "error": "ffmpeg not found (looked for %r) - install it, or start the agent with "
                         "--ffmpeg <full path>" % ARGS.ffmpeg}
    if os.name != "nt":
        devs = sorted(glob.glob("/dev/video*"))
        return {"devices": devs, "ffmpeg": True,
                "error": None if devs else "no /dev/video* devices on this host"}
    try:
        r = subprocess.run([ARGS.ffmpeg, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
    except Exception as e:
        return {"devices": [], "ffmpeg": True, "error": "ffmpeg failed: %r" % (e,)}
    text = (r.stderr or b"").decode("utf-8", "replace")
    devs = parse_dshow(text)
    return {"devices": devs, "ffmpeg": True,
            "error": None if devs else "ffmpeg listed no DirectShow video device",
            "raw": text[-1200:] if not devs else None}


def video_devices():
    return video_probe()["devices"]


def video_input(dev, size=None):
    """ffmpeg input arguments for a capture device. `size` picks the capture MODE (e.g. 640x480):
    an HDMI grabber will happily hand you a 16:9 frame with a 4:3 source stretched into it, so
    asking for the mode the board actually outputs is what keeps pixels square and 1:1.
    'test' is a synthetic source - it checks the plumbing without a capture card plugged in."""
    if dev == "test":
        # -re: a synthetic source runs as fast as the pipe drains it, which is nothing like a
        # capture card and turns a test into a CPU burner
        return ["-re", "-f", "lavfi", "-i", "testsrc=size=%s:rate=15" % (size or "640x480")]
    if os.name == "nt":
        return ["-f", "dshow", "-rtbufsize", "64M"] + \
               (["-video_size", size] if size else []) + ["-i", "video=%s" % dev]
    return ["-f", "v4l2"] + (["-video_size", size] if size else []) + ["-i", dev]


def video_proc(dev, width=0, fps=0, frames=0, size=None):
    """An ffmpeg writing MJPEG to stdout: one frame for a snapshot, a stream otherwise.
    width=0 means native - no scaling at all, which is what you want for pixel art."""
    cmd = [ARGS.ffmpeg, "-hide_banner", "-loglevel", "error"] + video_input(dev, size)
    if frames:
        cmd += ["-frames:v", str(frames)]
    vf = []
    if width and not (size and size.split("x")[0] == str(width)):
        vf.append("scale=%d:-2" % width)
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if fps:
        cmd += ["-r", str(fps)]
    cmd += ["-f", "mjpeg", "-q:v", "5", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class Capture:
    """One ffmpeg per capture device, many consumers.

    A capture card can only be opened once, so a snapshot taken while the panel is streaming
    used to fail with 'device already in use' - which is exactly when you want one. The device
    is opened once here and every viewer (and every snapshot) reads the latest frame."""

    def __init__(self, dev, width, fps, size=None):
        self.dev, self.width, self.fps, self.size = dev, width, fps, size
        self.proc = None
        self.frame = None
        self.seq = 0
        self.err = None
        self.users = 0
        self.cond = threading.Condition()

    def _pump(self, proc):
        # ffmpeg's stderr is read by its own thread: a chatty ffmpeg (a v4l2 warning per
        # frame) used to fill the pipe, block, and the stream froze after a few seconds.
        stderr_tail = collections.deque(maxlen=20)

        def drain():
            for line in iter(proc.stderr.readline, b""):
                stderr_tail.append(line)
        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        try:
            for jpg in jpeg_frames(proc):
                with self.cond:
                    self.frame, self.seq = jpg, self.seq + 1
                    self.cond.notify_all()
        finally:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
            drainer.join(timeout=2)
            tail = b"".join(stderr_tail).decode("utf-8", "replace").strip()[-400:]
            with self.cond:
                if proc is self.proc:
                    self.proc = None
                    if self.frame is None:
                        self.err = tail or "ffmpeg stopped"
                    self.cond.notify_all()

    def acquire(self):
        with self.cond:
            if self.proc is None:
                self.err = None
                self.frame = None          # a frame from the last run must not pass as live
                try:
                    self.proc = video_proc(self.dev, self.width, self.fps, size=self.size)
                except OSError as e:       # no ffmpeg binary after all, or it cannot spawn
                    self.err = "cannot start ffmpeg: %s" % e
                    raise
                threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            self.users += 1

    def kill(self):
        """Stop ffmpeg regardless of viewers (agent shutdown)."""
        with self.cond:
            proc, self.proc = self.proc, None
            self.cond.notify_all()
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    def release(self):
        with self.cond:
            self.users = max(0, self.users - 1)
            if self.users == 0 and self.proc is not None:
                proc, self.proc = self.proc, None
            else:
                proc = None
        if proc is not None:
            try:
                proc.kill()                              # frees the card for OBS and friends
                proc.wait(timeout=3)
            except Exception:
                pass

    def wait_frame(self, after_seq, timeout):
        """The next frame newer than after_seq, or (None, seq) on timeout/failure."""
        deadline = time.time() + timeout
        with self.cond:
            # after_seq = -1 means "whatever you have, even a frame already in hand" - so the
            # test is "is there a frame NEWER than after_seq", not "is seq past after_seq"
            while (self.frame is None or self.seq <= after_seq) and self.err is None:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            fresh = self.frame is not None and self.seq > after_seq
            return (self.frame if fresh else None), self.seq


CAPTURES = {}
CAPTURES_LOCK = threading.Lock()


def capture_for(dev, width, fps, size=None):
    """The shared capture for a device. Width/fps come from whoever opened it first - a second
    viewer joins the running stream instead of fighting for the card."""
    with CAPTURES_LOCK:
        cap = CAPTURES.get(dev)
        if cap is None or (cap.proc is None and (cap.size, cap.width) != (size, width)):
            cap = CAPTURES[dev] = Capture(dev, width, fps, size)
        return cap


def jpeg_frames(proc):
    """Split ffmpeg's MJPEG stdout into whole JPEGs (SOI ffd8 .. EOI ffd9)."""
    buf = b""
    while True:
        chunk = proc.stdout.read1(16384)   # read(): blocks until 16 KB - a frame's worth of lag
        if not chunk:
            if buf.startswith(b"\xff\xd8"):
                yield buf
            return
        buf += chunk
        while True:
            end = buf.find(b"\xff\xd9", 2)
            if end < 0:
                break
            yield buf[:end + 2]
            buf = buf[end + 2:]
            start = buf.find(b"\xff\xd8")
            buf = buf[start:] if start > 0 else buf


def discover():
    """What the host can see. Ports and drives carry the board's UID (USB serial number /
    boot_out.txt) so the panel can say WHICH board sits on COM7, not just that one does."""
    ports = [{"device": p.device, "description": p.description, "hwid": p.hwid,
              "serial_number": (p.serial_number or "").upper() or None,
              "vid": p.vid, "pid": p.pid} for p in comports()]
    drives = []
    for root in host_volumes():
        ident = drive_identity(root)
        drives.append(dict({"path": root, "circuitpy": ident is not None}, **(ident or {})))
    vid = video_probe()
    return {"serial": ports, "drives": drives, "openocd_cfgs": sorted(OCD_CFGS),
            "video": vid["devices"], "ffmpeg": vid["ffmpeg"],
            "video_error": vid.get("error"), "video_raw": vid.get("raw")}


# ---------------------------------------------------------------- server ----
BODY_REQUIRED = ("/boards", "/serial/write", "/repl", "/uf2", "/ocd/cmd", "/ocd/flash", "/run")
ALLOWED_HOSTS = set()                         # lower-case names this agent answers to (host_allowed)
ALLOWED_ORIGINS = set()                       # lower-case foreign origins allowed to call the API


def host_name(host):
    """`Host: name:port` / `[v6]:port` -> the name."""
    host = (host or "").strip()
    if host.startswith("["):
        return host[1:host.find("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host):
    """A web page cannot pick the Host it sends unless its own name resolves to this machine
    (DNS rebinding), so only names that are ours are answered: IP literals, localhost, this
    machine's hostname / .local name and --allow-host extras. No Host at all is an HTTP/1.0
    client, not a browser."""
    name = host_name(host).lower().rstrip(".")
    if not name or name in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def local_names():
    names = {"localhost"}
    try:
        h = socket.gethostname().lower()
        names.update((h, h + ".local", h.split(".")[0], h.split(".")[0] + ".local"))
        names.add(socket.getfqdn().lower())
    except Exception:
        pass
    return names


def is_loopback(bind):
    try:
        return bind in ("localhost", "") or ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


SERVER = None


def stop_server():
    """Let the reply go out, stop accepting, clean up; and if serve_forever does not return
    in time (a streaming /video holding on), leave the hard way."""
    time.sleep(0.2)
    if SERVER is not None:
        SERVER.shutdown()
    time.sleep(3)
    shutdown_all()
    os._exit(0)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # The browser aborts long-polls (tab switch), closes the snapshot tab mid-write, stops the
        # video stream: the handler's write then fails with a ConnectionError - BrokenPipe /
        # ConnectionReset, or ConnectionAborted (WinError 10053) on Windows. Not worth the
        # traceback socketserver prints by default; everything else still gets one.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    cors_origin = None                            # set by _gate() when a foreign origin is allowed

    replied = False                               # a reply has started: an error can only be logged

    def _dispatch(self, handler):
        """Every verb goes through here: the gate, then the handler, and whatever it raises
        becomes a status - 400 for a caller's mistake, 404 for a missing file, 503 when the
        board's drive or console is not there, 500 for the rest. Handlers used to wrap
        each branch in its own try/except, and the ones they forgot (PUT to an unplugged
        board, a bad ?ms=) ended as a traceback with no reply at all."""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if self._gate(u, q):
                handler(u, q)
        except (ConnectionError, socket.timeout):
            return                                # the client left; nothing to tell it
        except Exception as e:
            if self.replied:                      # mid-stream (video): only the log can know
                print("[%s] %s: %r" % (self.path.split("?")[0], type(e).__name__, e))
                return
            if isinstance(e, BadRequest):
                code, msg = 400, str(e)
            elif isinstance(e, DriveMissing):
                code, msg = 503, str(e)
            elif isinstance(e, ConsoleError):
                code, msg = 503, str(e)
            elif isinstance(e, FileNotFoundError):
                code, msg = 404, str(e) or "not found"
            elif isinstance(e, PermissionError):
                code, msg = 403, "permission denied: %s" % (e.filename or e)
            elif isinstance(e, (FileExistsError, IsADirectoryError, NotADirectoryError)):
                code, msg = 409, str(e)
            elif isinstance(e, (ValueError, UnicodeDecodeError, json.JSONDecodeError)):
                code, msg = 400, str(e) or type(e).__name__
            else:
                code, msg = 500, "%s: %s" % (type(e).__name__, e)
            self._json(code, {"error": msg})

    def _start(self, code, ctype, length=None, cache=None):
        """Status + headers for every reply, so the CORS headers never get forgotten."""
        self.replied = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if cache:
            self.send_header("Cache-Control", cache)
        if self.cors_origin:
            self.send_header("Access-Control-Allow-Origin", self.cors_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self._start(code, "application/json", len(body))
        self.wfile.write(body)

    def _text(self, code, s):
        body = s.encode("utf-8", "replace")
        self._start(code, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def _client_gone(self):
        """True once the browser has dropped this connection. Long-polls ask it while they wait,
        so a request the panel aborted (every tab switch does that) ends within a quarter second
        instead of holding a thread - and the console text - until its 15 s window runs out.
        The request line is fully read by now, so anything readable is EOF or a reset."""
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def log_message(self, *a):
        pass

    def _body(self, need=False):
        """The request body, or None after an error reply has been sent. Only a body whose
        length is declared is accepted: a chunked one would be read as empty and, for PUT
        /file, written as an empty file and reported as verified."""
        if self.headers.get("Transfer-Encoding"):
            self._json(411, {"error": "chunked request bodies are not supported - "
                                      "send a Content-Length"})
            return None
        length = self.headers.get("Content-Length")
        if length is None:
            if need:
                self._json(411, {"error": "Content-Length required"})
                return None
            return b""
        try:
            n = int(length)
            if n < 0:
                raise ValueError
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return None
        if n > ARGS.max_body * 1024 * 1024:
            self._json(413, {"error": "body larger than --max-body (%d MB)" % ARGS.max_body})
            return None
        data = self.rfile.read(n)
        if len(data) != n:
            self._json(400, {"error": "body shorter than Content-Length"})
            return None
        return data

    def _gate(self, u, q):
        """Every request passes here first: is the Host one of ours (DNS rebinding), is the
        Origin ours or allowed (a web page in the developer's browser must not be able to
        write code.py or flash), and does it carry the token when one is set. The panel
        page and /version are served without the token so a browser can ask for it."""
        host = (self.headers.get("Host") or "").strip()
        if not host_allowed(host):
            self._json(403, {"error": "Host %r is not this agent - reach it by IP, localhost "
                                      "or its hostname, or start with --allow-host %s"
                                      % (host, host_name(host))})
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            o = origin.rstrip("/").lower()
            if o in ALLOWED_ORIGINS:
                self.cors_origin = origin
            elif o != "http://" + host.lower():
                self._json(403, {"error": "cross-origin request from %s refused; start with "
                                          "--allow-origin %s to permit it" % (origin, origin)})
                return False
        if not TOKEN or u.path in ("/", "/ui.html", "/version"):
            return True
        given = self.headers.get("X-Token") or (q.get("token") or [""])[0] or ""
        if hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8")):
            return True
        self._json(401, {"error": "token required"})
        return False

    def do_OPTIONS(self):
        """CORS preflight for an --allow-origin page (X-Token / PUT trigger one)."""
        u = urlparse(self.path)
        origin = (self.headers.get("Origin") or "").rstrip("/").lower()
        if origin not in ALLOWED_ORIGINS:
            return self._json(403, {"error": "origin not allowed"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Token, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def _board(self, q):
        b = pick_board(q)
        if b is None:
            with BOARDS_LOCK:
                known = sorted(BOARDS)
            self._json(404, {"error": "no such board; pass ?board=<id> (configured: %s)" % known})
        return b

    def do_GET(self):
        self._dispatch(self._get)

    def do_PUT(self):
        self._dispatch(self._put)

    def do_DELETE(self):
        self._dispatch(self._delete)

    def do_POST(self):
        self._dispatch(self._post)

    def _get(self, u, q):
        if u.path in ("/", "/ui.html"):
            try:
                with open(os.path.join(HERE, "ui.html"), "rb") as f:
                    page = f.read()
            except Exception as e:
                return self._text(500, "ui.html not found next to devagent.py (%r)" % e)
            self._start(200, "text/html; charset=utf-8", len(page))
            self.wfile.write(page)
            return
        if u.path == "/version":
            return self._json(200, {"agent": AGENT_VERSION, "auth": bool(TOKEN)})
        if u.path == "/health":
            return self._json(200, {
                "agent": AGENT_VERSION, "uptime_s": int(time.time() - STARTED),
                "pyserial": serial is not None, "cpfs": cpfs is not None,
                "auth": bool(TOKEN), "boards": [b.status() for b in boards_ordered()],
            })
        if u.path == "/discover":
            return self._json(200, discover())
        if u.path == "/snippets":
            return self._json(200, [{"id": v["id"], "label": v["label"], "builtin": v["builtin"]}
                                    for v in snippets().values()])
        if u.path == "/boards":
            return self._json(200, [b.status() for b in boards_ordered()])
        if u.path == "/bootloader/status":
            vol = uf2_volume()
            return self._json(200, {"volume": vol, "in_bootloader": vol is not None,
                                    "info": uf2_info(vol) if vol else []})
        b = self._board(q)
        if b is None:
            return
        if u.path == "/serial/read":
            return self._text(200, b.drain(qint(q, "ms", 1500, 0, MS_MAX),   # ms=0: non-blocking
                                           stop=self._client_gone))
        if u.path == "/serial/tail":
            return self._json(200, b.tail(qint(q, "from", -1, -1), qint(q, "ms", 1500, 0, MS_MAX),
                                          stop=self._client_gone))
        if u.path == "/ocd/status":
            return self._json(200, {"running": b.ocd_running(), "ports": b.ocd_ports(),
                                    "cfg": b.ocd})
        if u.path in ("/video", "/snapshot"):
            dev = (q.get("device") or [b.video])[0]
            if not dev:
                return self._json(404, {"error": "no capture device set for board %r" % b.id})
            if not have_ffmpeg():
                return self._json(503, {"error": "ffmpeg not found on the agent host "
                                                 "(install it, or pass --ffmpeg <path>)"})
            size = qstr(q, "size", b.video_size or "")
            if size and not re.fullmatch(r"\d+x\d+", size):
                raise BadRequest("?size must look like 640x480")
            cap = capture_for(dev, qint(q, "w", 0, 0, 7680), qint(q, "fps", 12, 1, 60),
                              size or None)
            cap.acquire()
            try:
                if u.path == "/snapshot":
                    jpg, _ = cap.wait_frame(-1, 12)      # -1: the frame already in hand will do
                    if not jpg:
                        return self._json(502, {"error": "no frame from %r: %s%s"
                                                % (dev, cap.err or "timed out",
                                                   " (capture size %s - clear it if the card "
                                                   "cannot do that mode)" % cap.size
                                                   if cap.size else "")})
                    self._start(200, "image/jpeg", len(jpg), cache="no-store")
                    self.wfile.write(jpg)
                    return
                # MJPEG: the browser shows this straight in an <img>, no player needed
                self._start(200, "multipart/x-mixed-replace; boundary=frame", cache="no-store")
                seq = -1
                while True:
                    jpg, seq = cap.wait_frame(seq, 15)
                    if jpg is None:
                        return
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except ConnectionError:
                return                    # the tab closed (WinError 10053 on Windows) - just stop
            finally:
                cap.release()
        if u.path == "/info":
            return self._json(200, b.get_info(qflag(q, "refresh")))
        if u.path == "/free":
            st = shutil.disk_usage(b.join(""))    # join() says 503 when the drive is gone
            return self._json(200, {"free": st.free, "total": st.total, "used": st.used})
        if u.path == "/list":
            return self._json(200, b.list(qstr(q, "dir", ""), qflag(q, "recursive")))
        if u.path == "/file":
            data = b.read(qstr(q, "name"))
            self._start(200, "application/octet-stream", len(data))
            self.wfile.write(data)
            return
        return self._json(404, {"error": "unknown path"})

    def _put(self, u, q):
        if u.path != "/file":
            return self._json(404, {"error": "unknown path"})
        b = self._board(q)
        if b is None:
            return
        body = self._body(need=True)
        if body is None:
            return
        name = qstr(q, "name")
        if not qflag(q, "force"):
            why = autorun_guard(name, body)
            if why:
                return self._json(409, {"error": why})
        return self._json(200, b.write(name, body))

    def _delete(self, u, q):
        if u.path == "/boards":
            bid = (q.get("id") or [None])[0]
            return self._json(200, {"removed": bool(bid and drop_board(bid))})
        b = self._board(q)
        if b is None:
            return
        if u.path == "/file":
            name = qstr(q, "name")
            b.delete(name)
            return self._json(200, {"deleted": name})
        if u.path == "/dir":
            name = qstr(q, "name")
            b.rmdir(name, qflag(q, "recursive"))
            return self._json(200, {"removed": name})
        return self._json(404, {"error": "unknown path"})

    def _post(self, u, q):
        body = self._body(need=u.path in BODY_REQUIRED)
        if body is None:
            return
        if u.path == "/shutdown":
            threading.Thread(target=stop_server, daemon=True).start()
            return self._json(200, {"shutdown": True})
        if u.path == "/boards":
            spec = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(spec, dict):
                raise BadRequest("the body must be a JSON object")
            return self._json(200, add_board(spec).status())
        b = self._board(q)
        if b is None:
            return
        if u.path == "/serial/write":
            return self._json(200, {"sent": b.send(body)})
        if u.path == "/reset":
            mode = qstr(q, "mode", "soft")
            if mode not in ("soft", "hard"):
                raise BadRequest("?mode must be soft or hard")
            return self._json(200, b.reset(mode))
        if u.path == "/repl":
            out = b.run_repl(body.decode("utf-8", "replace"), qint(q, "ms", 6000, 0, MS_MAX))
            return self._json(200, {"output": out})
        if u.path == "/snippet":
            sid = qstr(q, "id")
            snip = snippets().get(sid)
            if snip is None:
                return self._json(404, {"error": "no snippet %r" % sid})
            ms = qint(q, "ms", 6000, 0, MS_MAX)
            out = b.run_repl(snip["code"], ms)
            note = None
            if not out.strip():
                note = ("the board sent nothing back in %d ms - is %s really this board's REPL, "
                        "and is the board past its boot (a program in code.py that never yields "
                        "still answers Ctrl-C)?" % (ms, b.port))
            return self._json(200, {"id": sid, "label": snip["label"], "output": out, "note": note})
        if u.path == "/serial/reboot":
            b.drain(200)
            b.send(b"\x03")
            time.sleep(0.3)
            b.send(b"\x04")
            return self._json(200, {"rebooted": True})
        if u.path == "/bootloader/enter":
            return self._json(200, b.bootloader_enter(bl_method(q), qfloat(q, "timeout", 20, 0, 300)))
        if u.path == "/uf2":
            if body[:4] != b"UF2\n":
                return self._json(400, {"error": "body is not a UF2 image (bad magic)"})
            vol = uf2_volume()
            if vol is None and qflag(q, "enter", True):
                r = b.bootloader_enter(bl_method(q), qfloat(q, "timeout", 20, 0, 300))
                vol = r.get("volume")
                if vol is None:
                    return self._json(503, {"error": "could not enter the bootloader", "detail": r})
            if vol is None:
                return self._json(503, {"error": "no UF2 volume; enter the bootloader first"})
            res = uf2_write(body, vol)
            if res.get("ok") and qflag(q, "wait", True):
                res["drive_back"] = wait_for(b.drive, qfloat(q, "wait_s", 25, 0, 300))
            return self._json(200, res)
        if u.path == "/ocd/start":
            return self._json(200, b.ocd_start(qstr(q, "cfg", "") or None))
        if u.path == "/ocd/stop":
            return self._json(200, b.ocd_stop())
        if u.path == "/ocd/cmd":
            if not b.ocd_running():
                return self._json(503, {"error": "openocd is not running for this board"})
            return self._json(200, {"output": b.ocd_tcl(body.decode("utf-8", "replace"))})
        if u.path == "/ocd/flash":
            if not body:
                return self._json(400, {"error": "body must be the firmware image"})
            return self._json(200, b.ocd_flash(body, qflag(q, "verify", True),
                                               qflag(q, "reset", True)))
        if u.path == "/mkdir":
            name = qstr(q, "name")
            b.mkdir(name)
            return self._json(200, {"created": name})
        if u.path == "/run":
            name = qstr(q, "name", "code.py")
            if not qflag(q, "force"):
                why = autorun_guard(name, body)
                if why:
                    return self._json(409, {"error": why})
            with b.tap() as sink:                     # a private copy: the panel's console poll
                res = b.write(name, body)             # would otherwise take the output first
                time.sleep(0.4)
                b.send(b"\x03")
                time.sleep(0.3)
                b.send(b"\x04")
                res["output"] = b.collect(sink, qint(q, "ms", 8000, 0, MS_MAX), done=code_done)
            return self._json(200, res)
        return self._json(404, {"error": "unknown path"})


def parse_add(spec):
    """'ID key=value key=value' (whitespace- or comma-separated) -> a board spec dict."""
    words = [w for w in re.split(r"[\s,]+", spec.strip()) if w]
    if not words:
        raise ValueError("empty")
    if "=" not in words[0]:
        words[0] = "id=" + words[0]
    keys = ("id", "label", "path", "port", "baud", "ocd", "fs", "index", "video", "video_size")
    d = {}
    for w in words:
        key, eq, val = w.partition("=")
        if not eq or key not in keys:
            raise ValueError("expected key=value with a key from %s, got %r" % (keys, w))
        d[key] = val
    return d


def bl_method(q):
    m = qstr(q, "method", "repl")
    if m not in ("repl", "touch", "auto"):
        raise BadRequest("?method must be repl, touch or auto")
    return m


def probe_existing(port):
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/version" % port, timeout=2) as r:
            v = json.loads(r.read().decode())
        return "another devagent (v%s)" % v.get("agent")
    except Exception:
        pass
    s = socket.socket()
    try:
        s.settimeout(1.0)
        s.connect(("127.0.0.1", port))
        return "something that is not a devagent"
    except Exception:
        return None
    finally:
        s.close()


def main():
    global ARGS
    try:
        sys.stdout.reconfigure(line_buffering=True)   # so a redirected log is live, not buffered
    except Exception:
        pass
    if sys.version_info < (3, 7):
        print("devagent needs Python 3.7 or newer (this is %s)" % sys.version.split()[0])
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (default: this machine only; 0.0.0.0 = the "
                         "network, which needs --token)")
    ap.add_argument("--token", default=os.environ.get("DEVAGENT_TOKEN"),
                    help="require this token on every data request (also $DEVAGENT_TOKEN; "
                         "remembered in devagent.json, --token '' forgets it)")
    ap.add_argument("--open", action="store_true",
                    help="serve the network WITHOUT a token - only on a network you trust")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="URL",
                    help="a web origin (https://site.example) allowed to call the API from "
                         "the browser, besides the panel itself; repeatable")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="an extra hostname this agent answers to (a DNS alias); repeatable")
    ap.add_argument("--any-path", action="store_true",
                    help="let a board's drive be a plain directory (no boot_out.txt), for "
                         "testing without hardware")
    ap.add_argument("--max-body", type=int, default=64, metavar="MB",
                    help="largest request body accepted (files, firmware); default 64")
    ap.add_argument("--ocd-bind", default="127.0.0.1", metavar="ADDR",
                    help="address OpenOCD's gdb/telnet/tcl ports listen on (default: local)")
    ap.add_argument("--openocd", default="openocd")
    ap.add_argument("--ffmpeg", default="ffmpeg",
                    help="ffmpeg used for HDMI-capture video (a board's 'video' device)")
    ap.add_argument("--add", action="append", default=[], metavar="SPEC",
                    help="add or edit a board from the command line: 'ID key=value ...' with "
                         "the keys of the panel form (path, port, baud, ocd, fs, index, video, "
                         "video_size, label), e.g. 'jam path=O:\\ port=COM4 ocd=rp2350'; "
                         "repeatable")
    ap.add_argument("--replace", action="store_true",
                    help="if another devagent holds the port, ask it to exit and take over")
    ARGS = ap.parse_args()
    ALLOWED_HOSTS.update(local_names())
    ALLOWED_HOSTS.update(h.lower() for h in ARGS.allow_host)
    ALLOWED_ORIGINS.update(o.rstrip("/").lower() for o in ARGS.allow_origin)

    who = probe_existing(ARGS.port)
    if who:
        if ARGS.replace and who.startswith("another devagent"):
            print("port %d held by %s - asking it to shut down" % (ARGS.port, who))
            try:
                import urllib.request
                urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:%d/shutdown" % ARGS.port, data=b"", method="POST",
                    headers={"X-Token": ARGS.token or ""}), timeout=3)
            except Exception:
                pass
            time.sleep(1.0)
            if probe_existing(ARGS.port):
                print("ERROR: it is still there; stop it by hand")
                return 1
        else:
            print("ERROR: port %d is already in use by %s." % (ARGS.port, who))
            print("       Use a different --port, or --replace to take it over.")
            return 1

    load_config()
    if not is_loopback(ARGS.bind) and not TOKEN and not ARGS.open:
        print("ERROR: --bind %s serves the network, and there is no token: anyone who can reach "
              "this machine could write files and flash firmware." % ARGS.bind)
        print("       Add --token SECRET (or DEVAGENT_TOKEN), or --open on a network you trust.")
        return 1
    for spec in ARGS.add:
        try:
            add_board(parse_add(spec))
        except ValueError as e:
            print("ERROR: --add %r: %s" % (spec, e))
            return 1

    print("devagent v%d on %s:%d  (%d board%s configured)"
          % (AGENT_VERSION, ARGS.bind, ARGS.port, len(BOARDS), "" if len(BOARDS) == 1 else "s"))
    for b in boards_ordered():
        print("  %-10s drive=%-12s serial=%-8s ocd=%-7s ports=%s"
              % (b.id, b.drive() or "-", b.port or "-", b.ocd or "-", b.ocd_ports()["tcl"]))
    if not BOARDS:
        print("  none yet - open http://localhost:%d/ and add one" % ARGS.port)
    if serial is None:
        print("  (pyserial missing - consoles disabled; pip install pyserial)")
    if cpfs is None:
        print("  (cpfs.py missing - REPL file access unavailable for boards without a drive)")
    if TOKEN:
        print("  auth: token required (%s)" % TOKEN_FROM)
    elif is_loopback(ARGS.bind):
        print("  auth: none - reachable from this machine only")
    else:
        print("  auth: OPEN (--open) - anyone on this network can write files and flash firmware")
    global SERVER
    try:
        SERVER = Server((ARGS.bind, ARGS.port), Handler)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
            print("ERROR: %s:%d is already in use." % (ARGS.bind, ARGS.port))
            return 1
        raise
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    atexit.register(shutdown_all)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        shutdown_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
