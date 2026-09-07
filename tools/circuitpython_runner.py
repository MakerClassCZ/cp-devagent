#!/usr/bin/env python3
"""Adafruit's circuitpython_runner.py, with the board behind a devagent.

The LLM-Recipes hw-tests-runner skill (github.com/adafruit/LLM-Recipes) drives a board from
pytest: `test_hw_circuitpython.py` runs `circuitpython_runner.py <script> --port --path
--duration` once per test script, and reads PASS:/FAIL: lines and the ~~END~~ sentinel from
its stdout. Drop this file over that `circuitpython_runner.py`: same command line, same
output - the program's console text from "code.py output:" on, cut short at ~~END~~ - but the
board is one a devagent serves, on this machine or across the network, so pytest runs where
the drive is not mounted and the serial port does not exist.

    circuitpython_runner.py test.py [--port ...] [--path ...] [--duration 60]

`--port` and `--path` are accepted and ignored (pytest passes them). The board comes from
    DEVAGENT_URL     http://host:8100      (default http://127.0.0.1:8100)
    DEVAGENT_BOARD   the board id          (only when the agent serves several)
    DEVAGENT_TOKEN   the agent's token
or from --agent / --board / --token. Needs nothing but Python."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

RUN_MS_MAX = 300000                # the agent's cap on one scripted run
OUTPUT_MARK = "code.py output:"    # CircuitPython prints it right before the program's first line


def main():
    ap = argparse.ArgumentParser(description="Run a script as code.py on a board a devagent "
                                             "serves and print what it says over the console.")
    ap.add_argument("filename", help="the file to run; it is written to the board as code.py")
    ap.add_argument("--port", help="ignored - the agent owns the serial port")
    ap.add_argument("--path", help="ignored - the agent owns the drive")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="seconds to listen; the run ends earlier at ~~END~~ or when the program ends")
    ap.add_argument("--agent", default=os.environ.get("DEVAGENT_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--board", default=os.environ.get("DEVAGENT_BOARD"))
    ap.add_argument("--token", default=os.environ.get("DEVAGENT_TOKEN"))
    args = ap.parse_args()

    with open(args.filename, "rb") as f:
        code = f.read()
    ms = min(int(args.duration * 1000), RUN_MS_MAX)
    params = {"name": "code.py", "ms": ms}
    if args.board:
        params["board"] = args.board
    url = args.agent.rstrip("/") + "/run?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=code, method="POST",
                                 headers={"X-Token": args.token} if args.token else {})
    try:
        with urllib.request.urlopen(req, timeout=ms / 1000 + 30) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        sys.exit("devagent: %s (HTTP %d)" % (body, e.code))
    except (urllib.error.URLError, OSError) as e:
        sys.exit("devagent unreachable at %s: %s" % (args.agent, e))

    output = reply.get("output", "")
    at = output.find(OUTPUT_MARK)
    if at >= 0:                    # as the original: the program's own words only
        output = output[at + len(OUTPUT_MARK):].lstrip("\r\n")
    sys.stdout.write(output)
    sys.stdout.flush()
    if reply.get("stopped") == "timeout":
        print("devagent: no ~~END~~ and the program still ran after %.0f s" % args.duration,
              file=sys.stderr)


if __name__ == "__main__":
    main()
