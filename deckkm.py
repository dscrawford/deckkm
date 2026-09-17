#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p "python3.withPackages (p: [p.evdev])"
"""deckkm: forward this PC's keyboard + mouse to a Steam Deck over ssh.

Devices are grabbed exclusively (the local desktop stops seeing them) only
after the Deck-side sink reports READY. Kill switches, all of which return
the devices to the PC:
  * hold Esc for --hold seconds (default 1s)
  * ssh dies / Deck unreachable
  * SIGINT/SIGTERM, any exception, or the process dying — the kernel drops an
    EVIOCGRAB when its file descriptor closes, so even kill -9 restores input.
On the Deck the sink releases held keys when the stream ends; if the sink
itself is killed, the kernel emits key-ups for held keys when the uinput
device is unregistered (input_dev_release_keys).
"""
import argparse
import json
import queue
import re
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time

from evdev import InputDevice, ecodes as e, list_devices

REC = struct.Struct("<BHHi")
DEFAULT_HOST = "deck@192.168.0.80"
DEFAULT_SINK = "~/.local/bin/deckkm-sink.py"
DEFAULT_EXCLUDE = r"Steam Controller|Webcam|deckkm"
READY_TIMEOUT_S = 15
IDLE_WAIT_S = 5


def is_kbd(caps):
    keys = caps.get(e.EV_KEY, [])
    return e.KEY_A in keys and e.KEY_Z in keys


def is_mouse(caps):
    return e.BTN_LEFT in caps.get(e.EV_KEY, []) and e.REL_X in caps.get(e.EV_REL, [])


def pick_devices(paths, exclude):
    if paths:
        return [InputDevice(p) for p in paths]
    out = []
    for p in sorted(list_devices()):
        d = InputDevice(p)
        caps = d.capabilities()
        if re.search(exclude, d.name) or not (is_kbd(caps) or is_mouse(caps)):
            d.close()
            continue
        out.append(d)
    return out


def describe(dev):
    caps = {}
    for t, codes in dev.capabilities(absinfo=True).items():
        if t == e.EV_ABS:
            caps[t] = [[c, list(ai)] for c, ai in codes]
        else:
            caps[t] = list(codes)
    return {"name": dev.name, "caps": caps}


def wait_all_released(devs, timeout):
    """Never grab while a key is held: the desktop would miss the key-up."""
    deadline = time.monotonic() + timeout
    while any(d.active_keys() for d in devs):
        if time.monotonic() > deadline:
            sys.exit("keys still held after %ss; aborting" % timeout)
        time.sleep(0.02)


def start_sink(host, sink, ssh_opts):
    if host == "local":  # run the sink on this machine (tests)
        return subprocess.Popen([sys.executable, "-u", sink], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=2",
           "-o", "ServerAliveCountMax=3", *ssh_opts, host, f"python3 -u {sink}"]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def wait_ready(proc, timeout):
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    if not sel.select(timeout):
        proc.kill()
        sys.exit("sink did not report READY within %ss" % timeout)
    line = proc.stdout.readline()
    if line.strip() != b"READY":
        proc.kill()
        sys.exit("sink failed: %r" % line)


def writer(proc, q, dead):
    try:
        while (chunk := q.get()) is not None:
            proc.stdin.write(chunk)
            proc.stdin.flush()
    except (OSError, ValueError):  # EPIPE, or stdin already closed by shutdown()
        pass
    dead.set()


def start_writer(proc):
    """Writes happen off the main thread so a stalled link never blocks the
    Esc-hold check. Returns (queue, thread, dead-event)."""
    q, dead = queue.Queue(), threading.Event()
    t = threading.Thread(target=writer, args=(proc, q, dead), daemon=True)
    t.start()
    return q, t, dead


def forward(devs, proc, hold, q, dead):
    """Pump events until Esc is held for `hold` seconds or the link dies."""
    sel = selectors.DefaultSelector()
    for i, d in enumerate(devs):
        sel.register(d.fd, selectors.EVENT_READ, (i, d))
    esc_down = None
    while not dead.is_set() and proc.poll() is None:
        for key, _ in sel.select(0.05):
            i, d = key.data
            chunk = bytearray()
            for ev in d.read():
                if ev.type == e.EV_KEY and ev.code == e.KEY_ESC:
                    if ev.value == 1:
                        esc_down = time.monotonic()
                    elif ev.value == 0:
                        esc_down = None
                chunk += REC.pack(i, ev.type, ev.code, ev.value)
            q.put(bytes(chunk))
        if esc_down and time.monotonic() - esc_down >= hold:
            return "esc held"
    return "ssh closed"


def shutdown(proc, q, thread):
    """End the stream so the sink releases its keys. Devices must already be
    ungrabbed: if the link is stalled the writer is stuck in write(), and only
    killing ssh (EPIPE) will unstick it."""
    q.put(None)
    thread.join(1.0)
    if thread.is_alive():
        proc.kill()
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--sink", default=DEFAULT_SINK, help="sink path on the Deck")
    ap.add_argument("--hold", type=float, default=1.0, help="Esc hold seconds")
    ap.add_argument("--dev", action="append", default=[], help="/dev/input/eventN (repeatable); default: autodetect")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE, help="regex of device names to skip")
    ap.add_argument("--ssh-opt", action="append", default=[], help="extra ssh option")
    ap.add_argument("--list", action="store_true", help="show devices that would be grabbed")
    a = ap.parse_args()

    devs = pick_devices(a.dev, a.exclude)
    if not devs:
        sys.exit("no keyboard/mouse found")
    for d in devs:
        print(f"  {d.path}  {d.name}", file=sys.stderr)
    if a.list:
        return

    proc = start_sink(a.host, a.sink, a.ssh_opt)
    proc.stdin.write(json.dumps({"devices": [describe(d) for d in devs]}).encode() + b"\n")
    proc.stdin.flush()
    wait_ready(proc, READY_TIMEOUT_S)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(1))
    wait_all_released(devs, IDLE_WAIT_S)
    for d in devs:
        d.grab()
    print(f"forwarding to {a.host}; hold Esc {a.hold}s to release", file=sys.stderr)
    q, thread, dead = start_writer(proc)
    why = "stopped"
    try:
        why = forward(devs, proc, a.hold, q, dead)
    finally:
        for d in devs:
            try:
                d.ungrab()
            except OSError:
                pass
        shutdown(proc, q, thread)
    print(f"released ({why})", file=sys.stderr)


if __name__ == "__main__":
    main()
