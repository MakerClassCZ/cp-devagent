# devagent — your dev boards over HTTP

The bridge that makes a board testable from anywhere: its **drive**, its **serial console**, its
**debug probe** and — with a capture card — its **screen**, over plain HTTP so `curl` works and
any language can drive it. **One agent serves every board plugged into the host**; each board is
a tab in the panel and a `?board=<id>` on the API.

    host with the boards                    this machine (or CI, or an agent)
    ┌──────────────────────────────┐        ┌──────────────────────────┐
    │ devagent.py                  │◀─HTTP─▶│ devagent_client.py / curl│
    │  jam  drive+serial+probe+HDMI│        └──────────────────────────┘
    │  pad  drive+serial           │
    │  …                           │
    └──────────────────────────────┘

Boards are defined **in the panel**, not on the command line, and kept in `devagent.json` next
to the script, so the agent comes back with the same set after a restart.

## Boards: adding, editing, and why a new COM port needs no edit

**Add** with the `＋ board` tab: the form is filled from what the host can see (ports, drives,
OpenOCD configs, capture devices), each labelled with the board that sits on it — `COM4 — Adafruit
Fruit Jam with rp2350b`, `O:\ ← Adafruit Fruit Jam with rp2350b`, and `· used by <board>` when
another entry already has it. **Edit** with the *Edit board* button, or click the `drive`/`serial`
pill in the header; the same form comes back pre-filled. **Forget** drops the entry (the board
itself is untouched).

**A COM-port or drive-letter change does not need an edit.** The UID CircuitPython prints in
`boot_out.txt` is also the board's USB serial number, so the agent learns it on first contact,
saves it, and when a configured port or drive vanishes — or the OS hands its name to a different
board — it re-points the entry at wherever that UID turned up (a grey `[devagent] serial COM3 ->
COM7` line in the console says so). Edit only to point an entry at *different* hardware; the UID
is then re-learnt.

The header pill says why a configured port is silent: `serial COM3 missing` (not on the host) vs
`serial COM3 busy` (present, but another program — Mu, Thonny, a terminal — holds it).

From a script or the CLI: `board port=COM7` edits one field of the selected board (`POST /boards`
with the id and only the fields to change; keys left out keep their value, `null`/`none` clears
one), `boards` lists them with their UID, `forget [ID]` drops one.

## Web control panel

Open `http://HOST:PORT/` in a browser. One page, no CDNs (it has to work on a bare LAN):

* **Console** — a real terminal, not a text box. The agent holds the serial port exclusively, so
  this is how you reach the board while it runs. Click it and type: keys go straight through, so
  REPL history (arrows), Tab completion and line editing behave normally, and nothing is echoed
  locally — what you see is what the board sent back. Ctrl-any-letter works; a multi-line paste
  is wrapped in the REPL's own paste mode so indentation survives. It understands the escape
  subset CircuitPython actually emits: SGR colours (a traceback is red), `\r` overwrites the line
  instead of stacking copies, erase-to-end-of-line, and the OSC title string is swallowed rather
  than printed as `]0;...` noise. The board you are looking at is **long-polled** — a read
  returns the moment the board says anything — so a keystroke is echoed in about 25 ms and an
  idle console makes no requests at all; the other boards get an occasional non-blocking read.
* **Board tabs** — one per board, each with its own console scrollback. Every board is polled in
  the background, so output that arrives while you are looking at another tab is still there when
  you switch back. `＋ board` opens a form filled from what the host can actually see (drives,
  COM ports, OpenOCD configs, capture devices) — no guessing at names.
* **Screen** — the board's HDMI output through a capture card, live in the page (MJPEG straight
  into an `<img>`, no player, no plugin). Click the picture for a full-size still. The capture
  card is opened only while the stream runs, so it stays free for OBS the rest of the time.
* **Board** — cached identity (CP version, board id, free RAM — probed once, it does not change
  while the board is plugged in), one-click **scripts** (I²C scan, pins, frozen modules, memory,
  storage; drop your own `.py` in `snippets/` and it appears next to them), **soft/hard reset**
  (hard goes over SWD when a probe is running, otherwise `microcontroller.reset()`), enter the
  bootloader (through the REPL, so nothing is persisted) and drop a `.uf2` on it; start/stop
  OpenOCD, run a probe command, flash an `.elf` over SWD.
* **The split is yours** — drag the edge between the console and the right column (it is
  remembered per browser), or double-click it to snap the column to the captured picture shown
  1:1. A 640x480 screen next to a full-height console needs the room.
* **Files** — browse (including subdirectories), upload, download, delete, mkdir. Uploads report
  a write mismatch loudly, and the bootloader-loop guard surfaces as a dialog rather than a
  silent failure.

Set `--token` and the panel asks for it once and keeps it in that browser; every data request
carries it. Without a token anyone on the LAN can write files and flash firmware.

## Seeing the screen

A USB HDMI capture card plugged into the host turns into two endpoints: `/video` (a live MJPEG
stream) and `/snapshot` (one JPEG). Both share **one ffmpeg per device** — a capture card can be
opened only once, so a snapshot taken while the panel is streaming reads the stream's latest
frame instead of failing with "device already in use"; it comes back in about a millisecond.
The card is released as soon as the last viewer leaves.

Set the board's capture **mode** to the resolution it actually outputs (`640x480` for a Fruit
Jam over DVI). Grabbers happily report 16:9 and stretch a 4:3 source into it — asking for the
native mode is what keeps the pixels square and 1:1, which matters when you are looking at a
320x240 game. With a mode set, nothing is rescaled.

The still is the useful half for automation —

    devagent_client.py --host 192.168.1.150 --port 8101 --board jam snapshot screen.jpg

— because it lets a script, a CI job or an agent **look at what the board is actually drawing**
instead of inferring it from serial output. Needs `ffmpeg` on the host (`--ffmpeg PATH` if it is
not on `PATH`). Set a board's capture device to `test` to get a colour-bar generator and check
the plumbing without a card.

## Boards without USB mass storage

`--fs repl` (or `auto`, which prefers a mounted drive) serves files over the serial REPL using
[MakerClassCZ/circuitpython-filesystem](https://github.com/MakerClassCZ/circuitpython-filesystem)
— put its `cpfs.py` next to `devagent.py` or on `PYTHONPATH`. The HTTP API does not change, so
every client and the web panel work identically on a board with no drive at all; only `/free`
stays drive-only. Each operation borrows the port from the console reader and hands it back.

## Run it (on the host the boards are plugged into)

    pip install pyserial
    python devagent.py --port 8100 --token secret       # then add boards in the panel
    python devagent.py --port 8100 --add jam:O:\:COM4:rp2350 --add pad:N:\:COM6

`--add id:drive:serial[:ocdcfg]` is only a shortcut for the first run; boards added either way
are saved to `devagent.json` and come back on the next start.

If the port is taken it says **who** has it (another devagent reports its drive and serial)
instead of dying on a traceback; `--replace` asks the old one to exit and takes over.

## Use it

    python devagent_client.py --host 192.168.1.150 --port 8101 [--board jam] [--token T] health
    ... boards                 ... board port=COM7           ... forget [ID]
    ... info                   ... reset [soft|hard]
    ... snippets               ... snippet i2c                ... snapshot screen.jpg
    ... put game.py            ... puttree assets/ assets     ... rm old.py
    ... mkdir scenes           ... rmdir scenes --recursive   ... list --recursive
    ... free                   ... ports
    ... run bench.py --ms 20000            upload + soft-reboot + capture output
    ... repl "import game; game.main()"    paste into the REPL, no filesystem write
    ... ocd start | status | stop          ... ocd cmd "halt"
    ... flash build/firmware.elf           program over SWD
    ... shell                              interactive: the same commands at a prompt
    ... console                            live terminal on the board's REPL

### Interactive: `shell` and `console`

`shell` keeps host/port/token and the current board between commands, so a session is
`use jam` · `put game.py` · `repl "import game"` · `list` instead of the full command line each
time. Tab completes command names, board ids (`use`), snippet ids, local paths (`put`, `run`,
`uf2`, `flash`) and remote paths (`get`, `rm`, `mkdir`, the target of `put`); history and line
editing come from readline where the platform has it. Every line goes through the same
dispatcher as the CLI, so nothing can behave differently at the prompt.

    $ python devagent_client.py --host 192.168.1.150 shell
    devagent http://192.168.1.150:8100 · boards: jam, picopad · `use ID` switches, `console` = live REPL, Ctrl-D quits
    192.168.1.150:8100> use picopad
    picopad> put game.py
    picopad> repl "import game"
    picopad> console
    --- console on picopad: Ctrl-] leaves, Ctrl-C / Ctrl-D go to the board ---
    >>> import gc; gc.mem_free()
    98432
    >>> ^]
    --- console closed ---
    picopad> quit

`console` is what `screen`/`tio` are on a local serial port — a raw terminal on the board's
REPL, through the agent. Keystrokes go to `/serial/write` as you type them; output comes from
`/serial/tail`, which consumes nothing, so the web panel's console keeps showing the same
stream. `Ctrl-C` interrupts the running program and `Ctrl-D` soft-reboots it, as on a serial
terminal; `Ctrl-]` (as in telnet) closes the console, leaving the board as it is. Arrow keys
reach the REPL's history on Windows as well. Needs agent v6+ (`/serial/tail`) and a real
terminal (stdin must be a tty). It is a terminal, not a recorder — for capturing a run's output
use `run`/`repl`, or `tail` from a script.

Or import it, which is the point — no session should re-derive this:

```python
from devagent_client import Dev
d = Dev("192.168.1.150", 8101, board="jam", token="secret")
d.put("doomlite.py")
print(d.repl("import doomlite; doomlite.main()", ms=6000))
d.snapshot("screen.jpg")          # and look at what it drew
```

## Two ways to flash, and the trap between them

**With a probe (preferred):** `flash build/firmware.elf` programs over SWD. No bootloader, no
volume juggling, and it works even when the board is wedged in a hard fault.

**Without a probe (UF2):**

    devagent_client.py ... bootloader          # is a UF2 volume mounted?
    devagent_client.py ... bootloader enter    # BOOTSEL via the REPL - nothing persisted
    devagent_client.py ... uf2 firmware.uf2    # enters if needed, copies, waits for the board back

### The trap this guards against

Asking for the bootloader **from `code.py`** cycles the board: the flash lands, `code.py` runs,
it requests BOOTSEL again, and you are back where you started — recoverable only by flashing a
flash-erase UF2. So an auto-run file (`code.py`, `main.py`, `boot.py`, `code.txt`, `main.txt`)
whose contents mention `RunMode.BOOTLOADER`, `on_next_reset` or `reset_to_bootloader` is
**refused** with an explanation:

    $ devagent_client.py ... put trap.py code.py
    {"error": "refusing to write code.py: it contains RunMode.BOOTLOADER + on_next_reset. ..."}

The same file under any other name is written normally, and `?force=1` overrides the guard when
you genuinely mean it. `bootloader enter` is the safe route: it drives the REPL, so nothing
survives the reset.

## Things it knows that cost us time to learn

* **Nothing is cached.** A reset re-enumerates USB and invalidates handles; the drive is
  re-resolved and the port re-opened per request. That is what a normal FTP/file server gets
  wrong — it caches a handle and then returns errors forever.
* **Writes are verified.** Every write is open → write → flush → fsync → close, then re-read,
  so the reply carries the sha256 of what is *actually* on disk. A stale-cache "success" is
  how a FAT got corrupted once.
* **Firmware is uploaded, not path-referenced.** OpenOCD runs on the agent's host and cannot
  see your filesystem — `flash` sends the image as the request body and stages it there.
* **The TCL port (6666) swallows OpenOCD's log; telnet (4444) echoes it.** Anything whose
  failure reason matters goes through telnet, which is why `flash` reports a real error.
* **A dropped client is not an error.** The panel aborts its `/serial/read` long-poll on every
  tab switch and closes snapshot/video mid-write; the handler's write then fails with a
  `ConnectionError` (`ConnectionAbortedError` / WinError 10053 on Windows). The server swallows
  those instead of printing the traceback `socketserver` would, and a long-poll checks every
  quarter second whether its client is still connected — an abandoned `read` used to keep
  consuming the console for the rest of its 15 s window, stealing output from the panel's new
  poll (v8).
* **`repl` beats `run` for anything that resets the board.** A reset left in `code.py` re-runs
  on every boot and traps the board in a loop; `repl` never touches the drive.
* **The REPL eats the first character after Ctrl-C**, so the client sends a bare newline first.
* **A UF2 copy "fails" on success.** The board reboots the instant the last block lands, so the
  volume disappears mid-write; `uf2` treats a vanished volume after a full write as the normal
  ending. Code that calls that an error reports a false failure on every good flash.
* **Start a board's reader AFTER the board is registered.** It used to start inside `Board()`,
  i.e. while `BOARDS[id] = Board(...)` was still evaluating - so the thread's first "am I still
  registered?" check said no and it exited. Every board came up with a dead console and every
  scripted run returned nothing.
* **One stream, two readers.** The panel polls the console while a script runs over the same
  port, and a read that empties the buffer means whoever asked first wins. Scripted runs read
  through a *tap* (their own copy) and pause the console feed, so the button prints the answer
  instead of the script's own echo.
* **A console read must return when data arrives, not when its window expires.** Waiting out the
  full poll window put up to a quarter second between a keypress and its echo; returning on the
  first byte (plus 15 ms to gather the burst) puts it at ~25 ms. The port read timeout matters
  too: the reader holds the port lock, so a long one makes every write queue behind it.
* **The console buffer has exactly one consumer.** A read takes the bytes, so two panels (or a
  panel and a `read` from the CLI) on the same board split the output between them. Scripted
  runs are safe - they use a tap - but do not point two consoles at one board and expect both to
  show everything.
* **Long-polling breaks `networkidle`.** Any automation that waits for an idle network against
  this panel hangs forever - wait for `domcontentloaded`.
* **Keystrokes must go out on one chain.** Sending each key as its own fetch lets them race;
  `print(6*7)` arrived as `print(6)*7`. The panel queues and batches instead.
* **Snippet bodies are raw strings.** A `"\n"` inside one is CircuitPython source, not a newline
  - unescaped it reached the board as an unterminated string literal.
* **ffmpeg 7 changed `-list_devices` output** (per-line `(video)` tags instead of section
  headers), so a parser written for ffmpeg 6 reports "no capture devices" next to a working
  card. Both formats are parsed, and an empty list always comes with a reason.
* **A board with no console must say so.** Every REPL-driven action (info, snippets, soft reset)
  checks the port first and returns the reason; the earlier version silently returned an empty
  string, which reads exactly like "the board printed nothing".
* **In the bootloader the CIRCUITPY drive is gone**, so `/bootloader/status` is answered before
  the drive check — otherwise the one endpoint you need during recovery would 503.

## Endpoints

| | |
|---|---|
| `GET /health` `/version` `/free` | status, feature detection |
| `GET /discover` | what the host can see: ports (with USB serial number), drives (with the `boot_out.txt` name / board id / UID), OpenOCD configs, capture devices |
| `GET/POST/DELETE /boards` | list / add-or-edit / forget a board (saved to `devagent.json`). POST merges: send the `id` and only the fields to change; `null` clears one. Status carries `uid` and `port_present` (v7) |
| `GET /info?refresh=` | cached board identity |
| `GET /snippets` · `POST /snippet?id=&ms=` | canned scripts and their output |
| `POST /reset?mode=soft\|hard` | reboot (hard = SWD if a probe is up) |
| `GET /video?w=&fps=&size=` · `GET /snapshot` | HDMI capture: MJPEG stream / one JPEG (shared) |
| `POST /repl?ms=` | body is code, pasted into the REPL; nothing written to the board |
| `GET /list?dir=&recursive=` | listing, directories marked |
| `GET|PUT|DELETE /file?name=` | read / write (parents auto-created, `force=1` to override the guard) / delete |
| `POST /mkdir?name=` · `DELETE /dir?name=&recursive=` | directories |
| `GET /serial/read?ms=` · `POST /serial/write` · `POST /serial/reboot` | console (`read` consumes — one reader at a time) |
| `GET /serial/tail?from=&ms=` | console without consuming: `{next, text}` from offset `from` on (`from=-1` = cursor at the current end); safe next to the panel's `read` loop (v6) |
| `POST /run?name=&ms=` | upload + reboot + capture |
| `GET /ocd/status` · `POST /ocd/start|stop|cmd` | probe control |
| `POST /ocd/flash?verify=&reset=` | body is the firmware image (SWD) |
| `GET /bootloader/status` · `POST /bootloader/enter?method=` | BOOTSEL, via REPL or 1200-baud touch |
| `POST /uf2?enter=&wait=` | body is a .uf2; copied to the bootloader volume |
| `POST /shutdown` | clean exit (used by `--replace`) |
| `GET /` | the web control panel (`ui.html`, edit it in place) |

Every board-specific endpoint takes `?board=<id>` (optional when only one board is configured)
and, if the agent was started with `--token`, `?token=` or an `X-Token:` header.

## Not done yet

Physical button injection and per-board power measurement — the remaining pieces of the
autonomous test-cluster plan. See `review/hw-cluster/`. (Display capture is done, but through a
capture card on the host; the planned SPI sniffer would cover boards whose panel never leaves
the PCB.)
