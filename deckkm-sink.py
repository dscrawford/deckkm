#!/usr/bin/env python3
"""deckkm sink: runs on the Deck. Reads a JSON header line describing source
devices, creates matching uinput devices, then replays binary event records
from stdin. Releases every held key when the stream ends for any reason."""
import json
import signal
import struct
import sys
import time

from evdev import AbsInfo, UInput, ecodes as e

REC = struct.Struct("<BHHi")  # dev index, type, code, value
SKIP_TYPES = {e.EV_SYN, e.EV_FF, e.EV_FF_STATUS, e.EV_REP, e.EV_PWR}


def build(spec):
    caps = {}
    for t, codes in spec["caps"].items():
        t = int(t)
        if t in SKIP_TYPES:
            continue
        if t == e.EV_ABS:
            caps[t] = [(int(c), AbsInfo(*a)) for c, a in codes]
        else:
            caps[t] = [int(c) for c in codes]
    return UInput(caps, name=f"deckkm {spec['name']}"[:79])


def release_all(devs, pressed):
    """One failing device must not keep another's keys held."""
    for ui, keys in zip(devs, pressed):
        try:
            for c in keys:
                ui.write(e.EV_KEY, c, 0)
            ui.syn()
        except OSError as x:
            print(f"release failed on {ui.name}: {x}", file=sys.stderr)
    time.sleep(0.05)  # let readers drain the key-ups before the devices vanish
    for ui in devs:
        try:
            ui.close()
        except OSError:
            pass


def main():
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    stdin = sys.stdin.buffer
    header = json.loads(stdin.readline())
    devs = [build(s) for s in header["devices"]]
    pressed = [set() for _ in devs]
    print("READY", flush=True)
    try:
        while True:
            b = stdin.read(REC.size)
            if len(b) < REC.size:
                break
            i, t, c, v = REC.unpack(b)
            devs[i].write(t, c, v)
            if t == e.EV_KEY:
                (pressed[i].add if v else pressed[i].discard)(c)
    finally:
        release_all(devs, pressed)


if __name__ == "__main__":
    main()
