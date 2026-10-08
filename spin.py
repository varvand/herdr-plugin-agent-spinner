#!/usr/bin/python3
"""Animate a one-cell icon on Agents-sidebar rows while that agent is working.

Herdr draws state_icon as a static glyph. This process writes a display-only
$spin token for panes whose agent_status is working, and clears it otherwise.
"""

import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time

SOURCE = "local.agent-spinner"
TOKEN = "spin"
FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
INTERVAL_S = 0.14
POLL_EVERY = 3
TTL_MS = 1600
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
    "~/.config/herdr/herdr.sock"
)

STOP = False


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not path:
        path = os.path.expanduser("~/.local/state/herdr/plugins/local.agent-spinner")
    os.makedirs(path, exist_ok=True)
    return path


def log(message):
    try:
        path = os.path.join(state_dir(), "spin.log")
        if os.path.exists(path) and os.path.getsize(path) > 200_000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S ") + message + "\n")
    except OSError:
        pass


class Herdr:
    def __init__(self):
        self.sock = None
        self.seq = time.time_ns() // 1_000_000

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def connect(self):
        self.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        self.sock = sock

    def call(self, method, params):
        payload = json.dumps({"id": "spin", "method": method, "params": params}).encode() + b"\n"
        last_error = None
        for _attempt in (1, 2):
            try:
                if self.sock is None:
                    self.connect()
                self.sock.sendall(payload)
                buf = b""
                while b"\n" not in buf:
                    chunk = self.sock.recv(1 << 20)
                    if not chunk:
                        raise ConnectionError("socket closed")
                    buf += chunk
                message = json.loads(buf.split(b"\n", 1)[0])
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"])[:300])
                return message.get("result")
            except (OSError, ConnectionError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                self.close()
        raise last_error

    def next_seq(self):
        self.seq += 1
        return self.seq


def working_panes(result):
    found = {}

    def walk(node):
        if isinstance(node, dict):
            pane_id = node.get("pane_id")
            status = node.get("agent_status")
            if isinstance(pane_id, str) and isinstance(status, str) and "workspace_id" in node:
                found[pane_id] = status
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result)
    return {pane_id for pane_id, status in found.items() if status == "working"}


def report(client, pane_id, frame):
    params = {
        "pane_id": pane_id,
        "source": SOURCE,
        "tokens": {TOKEN: frame},
        "seq": client.next_seq(),
    }
    if frame:
        params["ttl_ms"] = TTL_MS
    client.call("pane.report_metadata", params)


def clear_panes(client, pane_ids):
    for pane_id in list(pane_ids):
        try:
            report(client, pane_id, None)
        except (OSError, ConnectionError, RuntimeError) as exc:
            log("clear %s failed: %s" % (pane_id, exc))


def request_stop(_signum, _frame):
    global STOP
    STOP = True


def run_worker():
    directory = state_dir()
    lock_path = os.path.join(directory, "daemon.lock")
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return 0
        raise

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    client = Herdr()
    shown = set()
    frame_index = 0
    tick = 0
    current = set()
    log("started pid %s" % os.getpid())

    while not STOP:
        if not current or tick % POLL_EVERY == 0:
            try:
                result = client.call("session.snapshot", {})
                current = working_panes(result)
            except (OSError, ConnectionError, RuntimeError) as exc:
                log("snapshot failed: %s" % exc)
            else:
                gone = shown - current
                if gone:
                    clear_panes(client, gone)
                    shown -= gone
                if shown != current or tick == 0:
                    log("working %s" % " ".join(sorted(current)))

        frame = FRAMES[frame_index % len(FRAMES)]
        frame_index += 1
        for pane_id in sorted(current):
            if STOP:
                break
            try:
                report(client, pane_id, frame)
                shown.add(pane_id)
            except (OSError, ConnectionError, RuntimeError) as exc:
                log("frame %s failed: %s" % (pane_id, exc))
                shown.discard(pane_id)

        tick += 1
        deadline = time.monotonic() + (INTERVAL_S if current else 0.4)
        while time.monotonic() < deadline and not STOP:
            time.sleep(0.05)

    clear_panes(client, shown)
    client.close()
    log("stopped")
    return 0


def main():
    if "--worker" not in sys.argv:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        return 0
    return run_worker()


if __name__ == "__main__":
    sys.exit(main())
