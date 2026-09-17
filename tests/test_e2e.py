#!/usr/bin/env python3
"""End-to-end tests for deckkm. Creates a virtual keyboard+mouse as the
source so no real device is grabbed. Set DECKKM_HOST=deck@host to run
against a real Deck instead of a local sink."""
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from evdev import InputDevice, UInput, ecodes as e, list_devices

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("DECKKM_HOST", "local")
SINK = os.path.join(HERE, "deckkm-sink.py") if HOST == "local" else "~/.local/bin/deckkm-sink.py"
HOLD = 0.5


def virtual_source():
    caps = {e.EV_KEY: [e.KEY_A, e.KEY_ESC, e.KEY_ENTER, e.BTN_LEFT, e.KEY_Z],
            e.EV_REL: [e.REL_X, e.REL_Y]}
    return UInput(caps, name="deckkm-test-src")


def find_device(name, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in list_devices():
            d = InputDevice(p)
            if d.name == name:
                return d
            d.close()
        time.sleep(0.05)
    return None


def start(src_path):
    return subprocess.Popen([sys.executable, os.path.join(HERE, "deckkm.py"), "--host", HOST,
                             "--sink", SINK, "--dev", src_path, "--hold", str(HOLD)],
                            stderr=subprocess.PIPE, text=True)


def wait_grabbed(src_path, timeout=15):
    """Return once deckkm holds the grab (our own grab() then fails with EBUSY)."""
    probe = InputDevice(src_path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            probe.grab(); probe.ungrab()
        except OSError:
            probe.close(); return True
        time.sleep(0.05)
    probe.close(); return False


def assert_ungrabbed(src_path):
    probe = InputDevice(src_path)
    probe.grab(); probe.ungrab(); probe.close()


class Recorder(threading.Thread):
    """Drain a device in the background until it disappears (ENODEV)."""
    def __init__(self, dev):
        super().__init__(daemon=True); self.dev, self.got = dev, []; self.start()

    def run(self):
        try:
            for ev in self.dev.read_loop():
                if ev.type != e.EV_SYN:
                    self.got.append((ev.type, ev.code, ev.value))
        except OSError:
            pass


def press(src, code, value):
    src.write(e.EV_KEY, code, value); src.syn()


def remote_deckkm_devices():
    out = subprocess.run(["ssh", HOST, "cat /sys/class/input/event*/device/name"],
                         capture_output=True, text=True).stdout
    return [n for n in out.splitlines() if n.startswith("deckkm ")]


def test_forward_and_esc_hold(src, src_path):
    proc = start(src_path)
    assert wait_grabbed(src_path), proc.stderr.read()
    if HOST != "local":
        assert remote_deckkm_devices() == ["deckkm deckkm-test-src"], remote_deckkm_devices()
    sink_dev = find_device("deckkm deckkm-test-src") if HOST == "local" else None
    rec = Recorder(sink_dev) if sink_dev else None

    press(src, e.KEY_A, 1); press(src, e.KEY_A, 0)
    src.write(e.EV_REL, e.REL_X, 7); src.syn()
    if rec:
        time.sleep(0.3); got = list(rec.got)
        assert (e.EV_KEY, e.KEY_A, 1) in got and (e.EV_KEY, e.KEY_A, 0) in got, got
        assert (e.EV_REL, e.REL_X, 7) in got, got

    press(src, e.KEY_ESC, 1); press(src, e.KEY_ESC, 0)  # tap: must NOT release
    time.sleep(HOLD / 2)
    assert proc.poll() is None, "tap released control"

    press(src, e.KEY_ESC, 1)  # hold
    proc.wait(HOLD + 5)
    err = proc.stderr.read()
    assert "released (esc held)" in err, err
    if rec:  # sink must have released the held Esc before closing
        rec.join(2)
        assert rec.got[-2:] == [(e.EV_KEY, e.KEY_ESC, 1), (e.EV_KEY, e.KEY_ESC, 0)], rec.got
    press(src, e.KEY_ESC, 0)
    assert_ungrabbed(src_path)
    if sink_dev:
        assert find_device("deckkm deckkm-test-src", 1) is None, "sink device lingered"
    if HOST != "local":
        assert remote_deckkm_devices() == [], "sink device lingered on Deck"
    print("ok: forward, esc tap ignored, esc hold releases, sink releases keys")


def test_sink_death_releases(src, src_path):
    proc = start(src_path)
    assert wait_grabbed(src_path)
    kids = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
    for k in kids:
        os.kill(int(k), signal.SIGKILL)
    proc.wait(5)
    assert "released (ssh closed)" in proc.stderr.read()
    assert_ungrabbed(src_path)
    print("ok: sink/ssh death releases")


def test_sigterm_releases(src, src_path):
    proc = start(src_path)
    assert wait_grabbed(src_path)
    proc.send_signal(signal.SIGTERM)
    proc.wait(5)
    assert_ungrabbed(src_path)
    print("ok: SIGTERM releases")


def test_sigkill_releases(src, src_path):
    proc = start(src_path)
    assert wait_grabbed(src_path)
    proc.kill(); proc.wait(5)
    assert_ungrabbed(src_path)
    print("ok: SIGKILL releases (kernel drops grab)")


@pytest.fixture
def src():
    dev = virtual_source()
    time.sleep(0.3)
    yield dev
    dev.close()


@pytest.fixture
def src_path(src):
    return find_device("deckkm-test-src").path
