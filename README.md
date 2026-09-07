# devagent — your CircuitPython boards over HTTP

One Python file on the machine the boards are plugged into. It serves each board's **drive**,
**serial console**, **debug probe** and — through a capture card — its **screen** over plain
HTTP, so `curl` works, any language can drive it, and a script, a CI job or an AI agent can put
a file on the board, run it, read what it printed and *look at what it drew*. A web panel
(`ui.html`, no CDNs) and a Python client/CLI come with it.

    host with the boards                       anywhere on the network
    ┌────────────────────────────────┐         ┌─────────────────────────────┐
    │ devagent.py  :8100             │◀──HTTP──▶│ browser  → the panel        │
    │   pico   drive + serial        │         │ devagent_client.py / curl   │
    │   jam    drive + serial + SWD  │         │ your script, your agent     │
    │          + HDMI capture        │         └─────────────────────────────┘
    └────────────────────────────────┘

**One agent, every board on the host.** Each board is a tab in the panel and a `?board=<id>`
on the API. Boards are kept in `devagent.json` next to the script, so a restart brings them
back — and a board that comes back on another COM port or drive letter is found again by its
USB serial number, without an edit.

## Quick start

    pip install pyserial
    python devagent.py                       # panel on http://127.0.0.1:8100/  — add boards there

Or define the board on the command line (it is saved too):

    python devagent.py --add 'pico path=/media/you/CIRCUITPY port=/dev/ttyACM0'     # Linux
    python devagent.py --add 'pico path=/Volumes/CIRCUITPY port=/dev/cu.usbmodem1101' # macOS
    python devagent.py --add "pico path=E:\ port=COM4"                               # Windows

To reach it from other machines, bind the network and set a token (the agent refuses to serve
a network without one — `--open` is the deliberate exception for a network you trust):

    python devagent.py --bind 0.0.0.0 --token SECRET        # or DEVAGENT_TOKEN=SECRET

Then, from anywhere:

    python devagent_client.py --host 192.168.1.50 --token SECRET list
    python devagent_client.py --host 192.168.1.50 --token SECRET run game.py --ms 10000
    curl -H 'X-Token: SECRET' 'http://192.168.1.50:8100/snapshot?board=jam' -o screen.jpg

No hardware at hand? `python tests/fakeboard.py` prints a port and a drive to `--add`: a pty
that answers like the REPL and a directory that looks like CIRCUITPY.

### Requirements

* Python 3.7+ and `pyserial` (the console half; without it the file half still works).
* Linux: your user in the `dialout` group and ModemManager told to leave the board alone —
  `udev/99-circuitpython.rules` does both, install steps are in the file.
* Optional: `ffmpeg` for the screen (`--ffmpeg PATH` if it is not on `PATH`), `openocd` for
  SWD (`--openocd PATH`), and `cpfs.py` from
  [MakerClassCZ/circuitpython-filesystem](https://github.com/MakerClassCZ/circuitpython-filesystem)
  next to `devagent.py` for boards that have no USB drive at all.

Windows, Linux and macOS are supported; the capture path is tested on Windows and Linux, reports
from macOS are welcome.

## The panel

Open `http://HOST:PORT/`. With a token set the page asks for it once and keeps it in that
browser.

* **Console** — a real terminal, not a text box. The agent holds the serial port exclusively,
  so this is how you reach the board while it runs. Keys go straight through (REPL history,
  Tab completion and line editing behave; Ctrl-C interrupts, Ctrl-D reboots, Ctrl-] and the
  other control codes exist); a multi-line paste is wrapped in the REPL's paste mode so
  indentation survives. It understands the escape subset CircuitPython emits — colours, `\r`
  overwrite, erase-to-end-of-line, the OSC title. The board you are looking at is long-polled,
  so an echo arrives in ~25 ms and an idle console makes no requests; the other tabs get an
  occasional non-blocking read, so their output is there when you switch back. If the agent
  goes away the console says so once and picks up where it left off when it is back.
* **Board** — cached identity (CP version, board id, free RAM), one-click **scripts** (I²C
  scan, pins, frozen modules, memory, storage — drop your own `.py` in `snippets/` and it
  appears next to them), soft/hard reset (hard goes over SWD when a probe is running), enter the
  bootloader through the REPL and drop a `.uf2` on it, start/stop OpenOCD, run a probe command,
  flash an `.elf`.
* **Screen** — the board's HDMI output through a capture card, live in the page (MJPEG into an
  `<img>`); click it for a full-size still. The card is opened only while someone watches.
* **Files** — browse, upload, download, delete, mkdir; a write mismatch and the bootloader-loop
  guard are dialogs, not silent failures.
* **＋ board / Edit / Forget** — the form is filled from what the host can see (ports, drives,
  OpenOCD configs, capture devices), each labelled with the board that sits on it; a port or
  drive the host cannot see right now can be typed in.

## The client

`devagent_client.py` is the CLI, an interactive shell, a live console and an importable class,
all on the same dispatcher.

    devagent_client.py --host H [--port 8100] [--board ID] [--token T] COMMAND ...

    health · version · ports · boards · board [KEY=VALUE ...] · forget [ID] · info [refresh] · reset [soft|hard]
    list [--dir D] [--recursive] · get FILE · put FILE [REMOTE] [--force] · puttree DIR [REMOTE]
    rm FILE · mkdir DIR · rmdir DIR [--recursive] · free
    run FILE [--as code.py] [--ms 8000]      upload + soft reboot + capture until the program ends
    repl "CODE" [--ms 6000]                  paste into the REPL, nothing written to the board
    read [--ms N] · tail [--from N] · write TEXT · reboot
    snippets · snippet ID · snapshot [FILE] [--width W]
    bootloader [enter [repl|touch|auto]] · uf2 FILE · ocd start [CFG]|status|stop · ocd cmd "halt" · flash FILE.elf
    shell                                    the same commands at a prompt (Tab completion, history)
    console                                  a raw terminal on the board's REPL; Ctrl-] leaves

`--board` is optional while one board is configured. `DEVAGENT_TOKEN` stands in for `--token`.
Errors are errors: a failed command prints `error: <what> (HTTP <status>)` and exits 1; an agent
that cannot be reached exits 2 with a hint about where it should be running.

```python
from devagent_client import Dev, DevError
d = Dev("192.168.1.50", 8100, board="jam", token="SECRET")
d.put("game.py")
print(d.repl("import game; game.main()", ms=6000))
d.snapshot("screen.jpg")               # what the board is drawing right now
try:
    d.get("missing.py")
except DevError as e:
    print(e.status, e)                 # 404 no such file: missing.py (HTTP 404)
```

`console` is what `screen`/`tio` are on a local serial port, through the agent: keystrokes go to
`/serial/write`, output comes from `/serial/tail` (which consumes nothing, so the panel keeps
showing the same stream). It is a terminal, not a recorder — to capture a run use `run`/`repl`,
or `tail` from a script.

## The HTTP API

Every board-scoped endpoint takes `?board=<id>` (optional with one board configured). With a
token set, every request except `/`, `/version` carries it as `X-Token: T` or `?token=T`.
Replies are JSON unless noted; `ms` values are milliseconds, capped at 60000.

| | |
|---|---|
| `GET /health` `/version` | agent version, uptime, pyserial/cpfs present, every board's status |
| `GET /discover` | what the host can see: serial ports (with USB serial number), drives (with the `boot_out.txt` board name / UID), OpenOCD configs, capture devices |
| `GET /boards` · `POST /boards` · `DELETE /boards?id=` | list · add-or-edit (JSON body: `id` + only the fields to change; `null` clears one) · forget |
| `GET /list?dir=&recursive=` | `[{name, dir, size}]` |
| `GET /file?name=` · `PUT /file?name=[&force=1]` · `DELETE /file?name=` | read (bytes) · write, verified — the reply carries `matches_sent` and the `sha256` of what is on disk · delete |
| `POST /mkdir?name=` · `DELETE /dir?name=[&recursive=1]` · `GET /free` | directories, free space |
| `GET /serial/read?ms=` | console output (text). **Consumes**: one reader at a time. Returns as soon as the board says anything, or empty after `ms` (`ms=0` = non-blocking) |
| `GET /serial/tail?from=&ms=` | console output by cursor, **consumes nothing**: `{next, text, gap}`; `from=-1` starts at the current end |
| `POST /serial/write` · `POST /serial/reboot` | raw bytes to the board · Ctrl-D |
| `POST /repl?ms=` | body is code, pasted into the REPL; `{output}` |
| `POST /run?name=&ms=` | to the REPL prompt, upload, Ctrl-D, capture until the program ends or prints `~~END~~` (`ms` at most); `{output, stopped, matches_sent, ...}` — `stopped` is `~~END~~`, `Code done running.` or `timeout` |
| `GET /info[?refresh=1]` | board identity, probed once over the REPL and cached |
| `GET /snippets` · `POST /snippet?id=&ms=` | canned REPL scripts and their output |
| `POST /reset?mode=soft\|hard` | soft reboot, or a real reset (SWD when a probe is up) |
| `GET /snapshot?w=` · `GET /video?w=&fps=` | one JPEG · live MJPEG, from the board's capture device (`device=test` is a colour-bar generator) |
| `GET /bootloader/status` · `POST /bootloader/enter?method=repl\|touch\|auto` | is a UF2 volume mounted · enter it (REPL, or the 1200-baud touch) |
| `POST /uf2?enter=&wait=` | body is a `.uf2`; copied to the bootloader volume |
| `GET /ocd/status` · `POST /ocd/start?cfg=` `/ocd/stop` `/ocd/cmd` · `POST /ocd/flash?verify=&reset=` | OpenOCD per board on its own ports; body of `/flash` is the image |
| `POST /shutdown` | clean exit (also what `--replace` asks a previous instance for) |

**Status codes mean what they say.** `400` a bad parameter (the message names it), `401` no or
wrong token, `403` a Host or Origin that is not this agent, `404` no such board / file / path,
`409` refused on purpose (the bootloader-loop guard, a non-empty directory), `503` the board is
not available for that (drive unplugged, port busy, no console, OpenOCD not running — the
message says which), `502` a flash or capture that ran and failed, with ffmpeg's/OpenOCD's own
words. A board operation that reports an error never comes back as `200`.

## Boards

`devagent.json` holds the list; the panel, `POST /boards`, `board key=value` in the client and
`--add 'ID key=value ...'` all edit the same entries. Fields: `label`, `path` (the drive),
`port`, `baud`, `fs` (`auto` / `msc` / `repl`), `ocd` (an OpenOCD target config such as `rp2040`
or `rp2350`, giving that board its own OpenOCD instance on its own ports), `video` (a capture
device) and `video_size` (its native mode, `640x480` — ask for the mode the board outputs, or
the grabber stretches 4:3 into 16:9), `index` (tab order).

**A port or drive change needs no edit.** The UID CircuitPython prints in `boot_out.txt` is the
board's USB serial number too, so the agent learns it on first contact (from either) and, when the configured
port or drive vanishes or the OS hands its name to a different board, re-points the entry at
wherever that UID turned up (a grey `[devagent] serial COM3 -> COM7` line in the console says
so). A drive that carries *another* board's UID is not written to — the write answers 503
instead of landing on the wrong board. The header pill says why a port is silent: `missing`
(not on the host) or `busy` (another program — Mu, Thonny, a terminal — holds it).

**Boards without a drive** — `fs=repl` (or `auto`, which prefers a mounted drive) serves the
same file API over the REPL with `cpfs.py`; only `/free` stays drive-only.

**Two ways to flash.** With a probe, `flash firmware.elf` programs over SWD — no bootloader, no
volume juggling, and it works on a board wedged in a hard fault. Without one, `bootloader enter`
drives the REPL into BOOTSEL (nothing is persisted) and `uf2 firmware.uf2` copies the image and
waits for the board to come back. Asking for the bootloader **from `code.py`** is the trap: the
flash lands, `code.py` runs, it requests BOOTSEL again, and only a flash-erase UF2 gets you out.
So an auto-run file (`code.py`, `main.py`, `boot.py`, `.txt` variants) that mentions
`RunMode.BOOTLOADER`, `on_next_reset` or `reset_to_bootloader` is refused with an explanation;
`?force=1` (`--force`) overrides it when you mean it.

## Security

The agent writes files and flashes firmware, so it treats the network as hostile:

* It listens on `127.0.0.1` by default. `--bind 0.0.0.0` without `--token` is refused; `--open`
  is the explicit override for a trusted network.
* The token is compared in constant time and accepted as `X-Token` or `?token=`; the panel
  stores it in the browser only. Rotate it with `--token NEW` (`--token ''` forgets it).
* A browser tab on some other site cannot use your agent: requests must carry a Host that is
  this machine (IP, `localhost`, its hostname or `--allow-host NAME`) and an Origin that is the
  panel itself or an `--allow-origin URL` you listed (CORS with preflight for those).
* File names are confined to the board's drive (`..` and absolute paths answer 400/404), the
  drive must carry the board's own `boot_out.txt` (`--any-path` relaxes that for a plain
  directory), request bodies are capped (`--max-body MB`, default 64), OpenOCD's ports bind to
  `127.0.0.1` (`--ocd-bind`).

It is still an HTTP service with write access to hardware — put it behind a VPN or an SSH tunnel
rather than on the open internet.

## Troubleshooting

* **`serial /dev/ttyACM0 busy` right after plugging in (Linux)** — ModemManager is probing the
  port; the udev rule in `udev/` stops it. **Permission denied** — add yourself to `dialout`
  (or the rule's group) and log in again.
* **Console empty, REPL commands time out** — the board is running `code.py` and not at a
  prompt: Ctrl-C in the console, or `reset`. A board that prints nothing at all after Ctrl-C may
  be in the bootloader (`bootloader` says).
* **A big `repl` paste comes back with a `SyntaxError`** — the friendly REPL on an RP2040 drops
  characters somewhere past ~2 kB of paste. The paste goes out in 256-byte slices, which helps
  but is not a cure; for anything that size use `run` (a file) instead.
* **`/info` or a script answers 503 "no reply"** — same cause; the agent never returns an empty
  string that looks like "the board printed nothing".
* **Nothing appears in the panel from another machine** — `403` in the browser console means
  the Host or Origin check: reach the agent by IP or hostname, or add `--allow-host`.
* **`pip install pyserial` says it is installed but the agent says it is missing** — a different
  Python; start the agent with the interpreter you installed into.
* **A board that came back on another port is still `missing`** — the follow works from the
  UID the agent learnt on first contact (`boards` shows it); a board it never saw is only the
  name you typed. Edit the port once, and from then on it is found by UID.

## Tests

    pip install pyserial
    python tests/test_agent.py                      # ~30 s: the agent end to end on a fake board

Needs no hardware: `tests/fakeboard.py` is a pty that answers like the REPL plus a temp drive.
The suite starts the agent in a temp directory, so your `devagent.json` is untouched. The
capture tests run when `ffmpeg` is installed. `tests/test_panel.mjs` drives the panel in a
headless browser (Playwright) against a running agent — see its header.

    python tests/authprobe.py 192.168.1.50:8100      # your deployment, from another machine

is the black-box check of the gate on an agent that has a token: ~2000 requests — every
endpoint with every method, a wrong token in every place it could go, path tricks, a foreign
Host and Origin, a body announced before the token — and it prints only what got through.

## Adafruit LLM-Recipes hardware tests

[adafruit/LLM-Recipes](https://github.com/adafruit/LLM-Recipes) runs CircuitPython hardware
tests from pytest: a script per test that prints `PASS: …` / `FAIL: …` lines and `~~END~~` when it
is done, a `circuitpython_runner.py` that copies it to CIRCUITPY, soft-reboots and reads the
serial port, and `test_hw_circuitpython.py` that parses the output. devagent speaks the same
conventions — `run` stops at `~~END~~`, and the output reaches you line for line — and
`tools/circuitpython_runner.py` is that runner with the board behind an agent:

    cp tools/circuitpython_runner.py <driver-repo>/circuitpython_runner.py
    DEVAGENT_URL=http://192.168.1.50:8100 DEVAGENT_TOKEN=SECRET DEVAGENT_BOARD=jam \
        pytest test_hw_circuitpython.py -v

Same command line (`--port`/`--path` are accepted and ignored), same stdout, so the pytest
file and the skills are used unchanged; the difference is that the drive and the serial port
live where the agent runs. One `run` waits at most 300 s (`--duration 300`).

## Things it knows that cost time to learn

* **Nothing is cached.** A reset re-enumerates USB and invalidates handles; the drive is
  re-resolved and the port re-opened per request. That is what a normal file server gets wrong —
  it caches a handle and then returns errors forever.
* **Writes are verified.** Every write is open → write → flush → fsync → close, then re-read, so
  the reply carries the sha256 of what is *actually* on disk. A stale-cache "success" is how a
  FAT got corrupted once.
* **Firmware is uploaded, not path-referenced.** OpenOCD runs on the agent's host and cannot see
  your filesystem — `flash` sends the image as the request body and stages it there.
* **The TCL port swallows OpenOCD's log; telnet echoes it.** Anything whose failure reason
  matters goes through telnet, which is why `flash` reports a real error.
* **A dropped client is not an error.** The panel aborts its long-poll on every tab switch and
  closes a stream mid-write; the server swallows the resulting `ConnectionError` instead of a
  traceback, and a long-poll checks every quarter second whether its client is still there — an
  abandoned read used to keep consuming the console for the rest of its window.
* **`repl` beats `run` for anything that resets the board.** A reset left in `code.py` re-runs
  on every boot; `repl` never touches the drive.
* **`run` goes to the REPL prompt *before* it writes the file.** Written into a running board,
  `code.py` starts by auto-reload before the agent sends Ctrl-D — and interrupting the program
  that was running prints "Code done running." too, which ended the capture before the new
  program had said a word. At the prompt auto-reload is off; the capture starts at the Ctrl-D.
* **A board running `code.py` answers Ctrl-C with "Press any key to enter the REPL"**, and the
  next byte only enters the REPL — it is not typed. Sent blind, Ctrl-E was that byte, and the
  script went line by line into the plain prompt (a SyntaxError at the first indented line). A
  scripted run now waits for the prompt, answers the question with a bare Enter, and waits for
  the paste-mode prompt before it sends anything.
* **An edit that changes nothing must change nothing.** Re-posting a board's own spec used to
  rebuild it — console closed and reopened, OpenOCD stopped — for a no-op.
* **A UF2 copy "fails" on success.** The board reboots the instant the last block lands, so the
  volume disappears mid-write; a vanished volume after a full write is the normal ending.
* **One stream, two readers.** The panel polls the console while a script runs over the same
  port, and a read that empties the buffer means whoever asked first wins. Scripted runs read
  through a *tap* (their own copy) and pause the console feed.
* **A console read must return when data arrives, not when its window expires.** Waiting out the
  window put a quarter second between a keypress and its echo; returning on the first byte plus
  15 ms to gather the burst puts it at ~25 ms.
* **The console buffer has exactly one consumer.** Two panels, or a panel and a CLI `read`, on
  the same board split the output. `tail` is the non-consuming way to watch.
* **Keystrokes must go out on one chain.** Sending each key as its own request lets them race:
  `print(6*7)` arrived as `print(6)*7`. The panel queues and batches.
* **Snippet bodies are raw strings.** A `"\n"` inside one is CircuitPython source, not a
  newline — unescaped it reached the board as an unterminated string literal.
* **ffmpeg 7 changed `-list_devices` output**, so a parser written for ffmpeg 6 reports "no
  capture devices" next to a working card. Both formats are parsed, and an empty list always
  comes with a reason.
* **In the bootloader the CIRCUITPY drive is gone**, so `/bootloader/status` is answered before
  the drive check — otherwise the one endpoint you need during recovery would 503.
* **Serial ports are opened exclusively.** Two agents (or an agent and Thonny) on one port
  silently split the bytes; an exclusive open makes the second one fail with a name instead.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
