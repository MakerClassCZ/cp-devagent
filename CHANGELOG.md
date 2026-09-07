# Changelog

Versions are the `AGENT_VERSION` the agent reports in `/version` and `/health` (the panel
checks it matches its own). Older versions were internal.

## 9 — first public release

The hardening pass before publishing. Behaviour that changed:

* **Security.** Listens on `127.0.0.1` by default; a network bind without `--token` is refused
  unless `--open`. Host and Origin are checked on every request (`--allow-host`,
  `--allow-origin` with CORS preflight); the token is compared in constant time; `?token=` works
  next to `X-Token`. File names are confined to the board's drive; request bodies are capped
  (`--max-body`); OpenOCD's ports bind locally (`--ocd-bind`). `devagent.json` is written with
  mode 600.
* **Status codes mean something.** A board operation that fails answers 503 (502 for a flash or
  capture that ran and failed), a bad parameter 400, a missing file or board 404, a refusal 409.
  Every request is answered, including the ones that used to leak a traceback or hang.
* **Console/REPL.** Scripted runs (`/run`, `/repl`, `/snippet`) read through their own tap and
  fail loudly when the console is unusable, instead of returning an empty string. A long-poll
  returns on the first byte and notices a client that went away. Pastes go out in slices so the
  board's echo can be read while it is sent. A scripted run waits for the REPL prompt after
  Ctrl-C (and answers "Press any key to enter the REPL" when `code.py` was running) before it
  enters paste mode — sent blind, the script landed in the plain prompt.
* **Board registry.** Adds and edits from the panel, the client and `--add` are serialised and
  written atomically; `--add` takes `'ID key=value ...'` (the old `id:drive:port[:ocd]` form is
  refused with a hint) and validates the id. An edit that changes nothing leaves the board alone
  (its console and OpenOCD used to be restarted); the client's bare `board` shows the board.
* **Linux and macOS.** Drives are found under `/media/<user>`, `/run/media/<user>` and
  `/Volumes`; `/dev/ttyACM*`, `/dev/cu.*`; capture through v4l2/avfoundation; serial ports are
  opened exclusively. `udev/99-circuitpython.rules` for the ModemManager and permission problems.
* **Processes.** ffmpeg and OpenOCD children are stopped with the agent and on `/shutdown`; a
  snapshot never returns a frame from a previous device.
* **Client.** `DevError(status, message)` / `DevUnreachable` instead of `SystemExit` from
  library code; every value is URL-quoted; request deadlines follow `ms`; exit codes 1 (refused)
  and 2 (unreachable); `--board` optional with one board configured.
* **Panel.** Survives an agent restart (backoff, one dim note, then picks up), file names with
  spaces/quotes/markup, a 20000-character line, a 2000-line burst; AltGr and Ctrl-punctuation
  reach the board; the add form validates the id, accepts a typed port or path, and says when an
  id is taken; the header pill says why a port is `missing` or `busy` and when the panel and
  agent versions differ.
* **Tests.** `tests/test_agent.py` runs the agent against a fake board (pty REPL + temp drive);
  `tests/test_panel.mjs` drives the panel headlessly.

## 8

* A client that drops mid-request (a tab switch aborts the panel's long-poll every time) is no
  longer an error and no longer keeps consuming the console for the rest of its window.
* Scripted runs read through a *tap* — their own copy of the stream — so the panel's poll cannot
  take their output first.
* ffmpeg 7's `-list_devices` output is parsed next to ffmpeg 6's; an empty device list always
  carries the reason.

## 7

* A board is followed by its UID: the `UID:` line of `boot_out.txt` is the USB serial number,
  learnt on first contact and saved, so a port or drive that vanishes or gets its name handed to
  a different board is re-pointed without an edit. A drive with another board's UID is never
  written to.
* `POST /boards` merges: send only the fields to change, `null` clears one.
* `/health` and `/boards` report `uid` and `port_present`; `/discover` says which board sits on
  each port and drive.
* Client: `board key=value`, `forget`.
* Linux: drives under `/media/<user>/…` are found.

## 6

* `/serial/tail?from=&ms=` — console output by cursor, consuming nothing, so a script and the
  panel can both read the whole stream.
* The console is long-polled: a read returns when the board says something (plus 15 ms to
  gather the burst), which puts a keystroke's echo at ~25 ms; an idle console makes no requests.
* Keystrokes from the panel go out on one serialised chain (they used to race).
* REPL-driven actions say why the console is unreachable (no pyserial, no port, port busy)
  instead of returning nothing.

## 5

* One agent for every board on the host: `?board=<id>` on every endpoint, boards defined in the
  panel from what `/discover` sees, saved in `devagent.json`, each with its own reader thread,
  scrollback and OpenOCD ports.
* `/info` (identity, probed once and cached), `/snippets` + `/snippet` (with user `.py` files in
  `snippets/`), `/reset?mode=soft|hard`.
* HDMI capture: `/video` (MJPEG) and `/snapshot` (JPEG) through ffmpeg, one process per device
  shared by every viewer, opened only while someone watches; `video_size` for cards that report
  16:9 for a 4:3 board.
* Token auth (`--token`, `X-Token`).
