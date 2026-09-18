import json
import os
import struct
import threading
import time

import pytest
from evdev import ecodes as e
from types import SimpleNamespace

from conftest import load
from fakes import FakeDev, FakeProc, ev

km = load("deckkm", "deckkm.py")

KBD = {e.EV_KEY: [e.KEY_A, e.KEY_Z, e.KEY_ESC]}
MOUSE = {e.EV_KEY: [e.BTN_LEFT], e.EV_REL: [e.REL_X, e.REL_Y]}


def test_classify():
    assert km.is_kbd(KBD) and not km.is_mouse(KBD)
    assert km.is_mouse(MOUSE) and not km.is_kbd(MOUSE)
    assert not km.is_kbd({e.EV_KEY: [e.KEY_A]})  # consumer-control nodes with a few keys
    assert not km.is_mouse({e.EV_KEY: [e.BTN_LEFT]})  # touchpad-ish without REL_X


def test_pick_devices_autodetect_and_exclude(monkeypatch):
    devs = {
        "/dev/input/event1": FakeDev("Logitech Keyboard", KBD),
        "/dev/input/event2": FakeDev("Logitech Mouse", MOUSE),
        "/dev/input/event3": FakeDev("Steam Controller Puck Keyboard", KBD),
        "/dev/input/event4": FakeDev("Power Button", {e.EV_KEY: [e.KEY_POWER]}),
    }
    monkeypatch.setattr(km, "list_devices", lambda: list(devs))
    monkeypatch.setattr(km, "InputDevice", lambda p: devs[p])
    picked = km.pick_devices([], km.DEFAULT_EXCLUDE)
    assert [d.name for d in picked] == ["Logitech Keyboard", "Logitech Mouse"]
    assert devs["/dev/input/event3"].closed and devs["/dev/input/event4"].closed
    assert not devs["/dev/input/event1"].closed


def test_pick_devices_explicit_paths_skip_filtering(monkeypatch):
    d = FakeDev("Steam Controller Puck Keyboard", KBD)
    monkeypatch.setattr(km, "InputDevice", lambda p: d)
    assert km.pick_devices(["/dev/input/event3"], km.DEFAULT_EXCLUDE) == [d]


def test_describe_serialises_absinfo():
    from evdev import AbsInfo
    caps = {e.EV_SYN: [0], e.EV_KEY: [e.KEY_A], e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, 100, 1, 2, 3))]}
    spec = km.describe(FakeDev("pad", caps))
    assert spec["name"] == "pad"
    assert spec["caps"][e.EV_ABS] == [[e.ABS_X, [0, 0, 100, 1, 2, 3]]]
    json.dumps(spec)  # must be JSON-clean


def test_wait_all_released_returns_when_idle():
    d = FakeDev("k", KBD)
    d.keys = {e.KEY_ENTER}
    threading.Timer(0.1, d.keys.clear).start()
    km.wait_all_released([d], 2)


def test_wait_all_released_aborts_if_key_stuck():
    d = FakeDev("k", KBD)
    d.keys = {e.KEY_ENTER}
    with pytest.raises(SystemExit):
        km.wait_all_released([d], 0.1)


def test_resolve_host_keeps_resolvable_names(monkeypatch):
    monkeypatch.setattr(km.socket, "getaddrinfo", lambda *a: [()])
    monkeypatch.setattr(km.subprocess, "run", lambda *a, **k: pytest.fail("must not call tailscale"))
    assert km.resolve_host("deck@lan-host") == "deck@lan-host"


def test_resolve_host_falls_back_to_tailscale(monkeypatch):
    import socket as s
    def fail(*a):
        raise s.gaierror()
    monkeypatch.setattr(km.socket, "getaddrinfo", fail)
    calls = []
    def run(cmd, **k):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="100.1.2.3\n")
    monkeypatch.setattr(km.subprocess, "run", run)
    assert km.resolve_host("deck@steamdeck") == "deck@100.1.2.3"
    assert km.resolve_host("steamdeck") == "100.1.2.3"
    assert calls[0] == ["tailscale", "ip", "-4", "steamdeck"]


@pytest.mark.parametrize("outcome", ["unknown", "missing"])
def test_resolve_host_unchanged_when_tailscale_cannot_help(monkeypatch, outcome):
    import socket as s
    def fail(*a):
        raise s.gaierror()
    monkeypatch.setattr(km.socket, "getaddrinfo", fail)
    def run(cmd, **k):
        if outcome == "missing":
            raise FileNotFoundError("tailscale")
        return SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(km.subprocess, "run", run)
    assert km.resolve_host("deck@nope") == "deck@nope"


def test_wait_ready_ok():
    p = FakeProc()
    p.remote_says(b"READY\n")
    km.wait_ready(p, 1, "deck@x")
    assert not p.killed


@pytest.mark.parametrize("reply", [b"", b"Traceback\n"])
def test_wait_ready_bad_reply_kills_ssh(reply):
    p = FakeProc()
    if reply:
        p.remote_says(reply)
    else:
        os.close(p.stdout_w)
    with pytest.raises(SystemExit) as x:
        km.wait_ready(p, 1, "deck@x")
    assert p.killed
    assert "deck@x" in str(x.value)
    assert ("DECKKM_HOST" in str(x.value)) == (reply == b"")


def test_wait_ready_timeout_kills_ssh():
    p = FakeProc()
    with pytest.raises(SystemExit) as x:
        km.wait_ready(p, 0.1, "deck@x")
    assert p.killed and "deck@x" in str(x.value)


def test_writer_sets_dead_on_broken_pipe():
    p = FakeProc()
    q, t, dead = km.start_writer(p)
    q.put(b"abc")
    assert os.read(p.stdin_r, 3) == b"abc"
    os.close(p.stdin_r)
    q.put(b"more")
    assert dead.wait(2)


def test_writer_survives_stdin_closed_underneath(capsys):
    p = FakeProc()
    q, t, dead = km.start_writer(p)
    p.stdin.close()
    q.put(b"late")
    assert dead.wait(2)
    t.join(1)
    assert "Traceback" not in capsys.readouterr().err


def run_forward(devs, proc, hold, q, dead):
    box = {}
    t = threading.Thread(target=lambda: box.update(why=km.forward(devs, proc, hold, q, dead)))
    t.start()
    return t, box


def recs(proc, n):
    data = b""
    while len(data) < n * km.REC.size:
        data += os.read(proc.stdin_r, 4096)
    return [km.REC.unpack_from(data, i) for i in range(0, len(data), km.REC.size)]


def test_forward_packs_events_with_device_index():
    kbd, mouse, p = FakeDev("k", KBD), FakeDev("m", MOUSE), FakeProc()
    q, wt, dead = km.start_writer(p)
    t, box = run_forward([kbd, mouse], p, 1.0, q, dead)
    kbd.push(ev(e.EV_KEY, e.KEY_A, 1), ev(e.EV_SYN, 0, 0))
    mouse.push(ev(e.EV_REL, e.REL_X, -5), ev(e.EV_SYN, 0, 0))
    assert recs(p, 4) == [(0, e.EV_KEY, e.KEY_A, 1), (0, e.EV_SYN, 0, 0),
                          (1, e.EV_REL, e.REL_X, -5), (1, e.EV_SYN, 0, 0)]
    p.returncode = 0
    t.join(2)
    assert box["why"] == "ssh closed"


def test_forward_esc_tap_is_forwarded_not_release():
    kbd, p = FakeDev("k", KBD), FakeProc()
    q, wt, dead = km.start_writer(p)
    t, box = run_forward([kbd], p, 0.3, q, dead)
    kbd.push(ev(e.EV_KEY, e.KEY_ESC, 1))
    kbd.push(ev(e.EV_KEY, e.KEY_ESC, 0))
    assert recs(p, 2) == [(0, e.EV_KEY, e.KEY_ESC, 1), (0, e.EV_KEY, e.KEY_ESC, 0)]
    time.sleep(0.5)
    assert t.is_alive(), "tap must not release"
    dead.set()
    t.join(2)


def test_forward_esc_hold_releases_and_repeat_keeps_timer():
    kbd, p = FakeDev("k", KBD), FakeProc()
    q, wt, dead = km.start_writer(p)
    t, box = run_forward([kbd], p, 0.3, q, dead)
    start = time.monotonic()
    kbd.push(ev(e.EV_KEY, e.KEY_ESC, 1))
    time.sleep(0.15)
    kbd.push(ev(e.EV_KEY, e.KEY_ESC, 2))  # autorepeat must not reset the hold timer
    t.join(2)
    assert box["why"] == "esc held"
    assert 0.25 < time.monotonic() - start < 0.6


def test_forward_returns_when_writer_dies():
    kbd, p = FakeDev("k", KBD), FakeProc()
    q, wt, dead = km.start_writer(p)
    t, box = run_forward([kbd], p, 1.0, q, dead)
    os.close(p.stdin_r)
    kbd.push(ev(e.EV_KEY, e.KEY_A, 1))
    t.join(2)
    assert box["why"] == "ssh closed"


def test_shutdown_clean():
    p = FakeProc()
    q, t, dead = km.start_writer(p)
    p.returncode = 0
    km.shutdown(p, q, t)
    assert not t.is_alive() and not p.killed


def test_shutdown_does_not_hang_on_stalled_link():
    """Nobody drains ssh's stdin: the writer blocks on a full pipe. shutdown
    must still return promptly (by killing ssh) instead of hanging forever."""
    p = FakeProc()
    q, t, dead = km.start_writer(p)
    for _ in range(64):
        q.put(b"\0" * 65536)
    time.sleep(0.2)
    assert t.is_alive()
    start = time.monotonic()
    km.shutdown(p, q, t)
    assert time.monotonic() - start < 3
    assert p.killed
    t.join(2)
    assert not t.is_alive()


def test_main_list_does_not_connect(monkeypatch, capsys):
    monkeypatch.setattr(km, "pick_devices", lambda paths, ex: [FakeDev("k", KBD, "/dev/input/event9")])
    monkeypatch.setattr(km, "start_sink", lambda *a: pytest.fail("must not connect"))
    monkeypatch.setattr("sys.argv", ["deckkm", "--list"])
    km.main()
    assert "/dev/input/event9  k" in capsys.readouterr().err


def test_main_host_from_env(monkeypatch):
    seen = {}

    def fake_start(host, sink, opts):
        seen["host"] = host
        raise SystemExit(0)
    monkeypatch.setattr(km, "pick_devices", lambda paths, ex: [FakeDev("k", KBD)])
    monkeypatch.setattr(km, "start_sink", fake_start)
    monkeypatch.setattr(km, "resolve_host", lambda h: h)
    monkeypatch.setenv("DECKKM_HOST", "deck@example")
    monkeypatch.setattr("sys.argv", ["deckkm"])
    with pytest.raises(SystemExit):
        km.main()
    assert seen["host"] == "deck@example"


def test_main_no_devices_exits(monkeypatch):
    monkeypatch.setattr(km, "pick_devices", lambda paths, ex: [])
    monkeypatch.setattr("sys.argv", ["deckkm"])
    with pytest.raises(SystemExit):
        km.main()


def test_main_end_to_end_with_fakes(monkeypatch, capsys):
    """Full main(): READY handshake, grab after idle, esc-hold release, ungrab."""
    kbd = FakeDev("k", KBD, "/dev/input/event9")
    p = FakeProc()
    p.remote_says(b"READY\n")
    monkeypatch.setattr(km, "pick_devices", lambda paths, ex: [kbd])
    monkeypatch.setattr(km, "start_sink", lambda *a: p)
    monkeypatch.setattr("sys.argv", ["deckkm", "--hold", "0.2"])

    def drive():
        while not kbd.grabbed:
            time.sleep(0.01)
        kbd.push(ev(e.EV_KEY, e.KEY_ESC, 1))
    threading.Thread(target=drive).start()
    threading.Thread(target=lambda: [os.read(p.stdin_r, 4096)], daemon=True).start()
    km.main()
    assert not kbd.grabbed
    assert "released (esc held)" in capsys.readouterr().err
    p.returncode = 0
