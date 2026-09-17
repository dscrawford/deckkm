import io
import json
import struct

import pytest
from evdev import AbsInfo, ecodes as e

from conftest import load
from fakes import FakeDev, FakeUInput

sink = load("deckkm_sink", "deckkm-sink.py")
km = load("deckkm", "deckkm.py")


@pytest.fixture(autouse=True)
def fake_uinput(monkeypatch):
    FakeUInput.instances = []
    monkeypatch.setattr(sink, "UInput", FakeUInput)
    monkeypatch.setattr(sink.time, "sleep", lambda s: None)
    yield


def rec(i, t, c, v):
    return sink.REC.pack(i, t, c, v)


def header(*specs):
    return json.dumps({"devices": list(specs)}).encode() + b"\n"


def run_main(monkeypatch, data, capsys):
    monkeypatch.setattr(sink.sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    sink.main()
    return capsys.readouterr().out


def test_build_maps_caps_and_skips_unreplayable_types():
    spec = {"name": "kbd", "caps": {str(e.EV_SYN): [0], str(e.EV_KEY): [e.KEY_A],
                                    str(e.EV_REP): [0, 1], str(e.EV_FF): [1],
                                    str(e.EV_ABS): [[e.ABS_X, [0, 0, 255, 0, 0, 0]]]}}
    ui = sink.build(spec)
    assert ui.name == "deckkm kbd"
    assert ui.caps[e.EV_KEY] == [e.KEY_A]
    assert ui.caps[e.EV_ABS] == [(e.ABS_X, AbsInfo(0, 0, 255, 0, 0, 0))]
    assert not {e.EV_SYN, e.EV_REP, e.EV_FF} & ui.caps.keys()


def test_build_truncates_long_names():
    ui = sink.build({"name": "x" * 200, "caps": {}})
    assert len(ui.name) == 79


def test_describe_build_roundtrip_through_json():
    caps = {e.EV_SYN: [0], e.EV_KEY: [e.KEY_A, e.KEY_Z], e.EV_REL: [e.REL_X],
            e.EV_ABS: [(e.ABS_X, AbsInfo(1, 2, 3, 4, 5, 6))], e.EV_MSC: [e.MSC_SCAN]}
    spec = json.loads(json.dumps(km.describe(FakeDev("kbd", caps))))
    ui = sink.build(spec)
    assert ui.caps == {e.EV_KEY: [e.KEY_A, e.KEY_Z], e.EV_REL: [e.REL_X],
                       e.EV_ABS: [(e.ABS_X, AbsInfo(1, 2, 3, 4, 5, 6))], e.EV_MSC: [e.MSC_SCAN]}


def test_release_all_releases_only_pressed_keys():
    a, b = FakeUInput({}), FakeUInput({})
    sink.release_all([a, b], [{e.KEY_A, e.KEY_ESC}, set()])
    assert sorted(w for w in a.writes if w != "syn") == sorted([(e.EV_KEY, e.KEY_A, 0), (e.EV_KEY, e.KEY_ESC, 0)])
    assert a.writes[-1] == "syn" and b.writes == ["syn"]
    assert a.closed and b.closed


def test_release_all_isolates_device_failures(capsys):
    class Broken(FakeUInput):
        def write(self, *a):
            raise OSError("uinput gone")

        def close(self):
            raise OSError("already closed")

    bad, good = Broken({}), FakeUInput({})
    sink.release_all([bad, good], [{e.KEY_A}, {e.KEY_B}])
    assert good.writes == [(e.EV_KEY, e.KEY_B, 0), "syn"] and good.closed
    assert "release failed" in capsys.readouterr().err


def test_main_replays_to_indexed_devices_and_reports_ready(monkeypatch, capsys):
    data = header({"name": "k", "caps": {str(e.EV_KEY): [e.KEY_A]}},
                  {"name": "m", "caps": {str(e.EV_REL): [e.REL_X]}})
    data += rec(0, e.EV_KEY, e.KEY_A, 1) + rec(0, e.EV_SYN, 0, 0) + rec(1, e.EV_REL, e.REL_X, 3) + rec(0, e.EV_KEY, e.KEY_A, 0)
    out = run_main(monkeypatch, data, capsys)
    assert out == "READY\n"
    k, m = FakeUInput.instances
    assert k.writes == [(e.EV_KEY, e.KEY_A, 1), (e.EV_SYN, 0, 0), (e.EV_KEY, e.KEY_A, 0), "syn"]
    assert m.writes == [(e.EV_REL, e.REL_X, 3), "syn"]
    assert k.closed and m.closed


def test_main_releases_held_keys_on_eof(monkeypatch, capsys):
    data = header({"name": "k", "caps": {}}) + rec(0, e.EV_KEY, e.KEY_ESC, 1) + rec(0, e.EV_KEY, e.KEY_ESC, 2)
    run_main(monkeypatch, data, capsys)
    k, = FakeUInput.instances
    assert k.writes[-2:] == [(e.EV_KEY, e.KEY_ESC, 0), "syn"]
    assert k.writes.count((e.EV_KEY, e.KEY_ESC, 0)) == 1


def test_main_truncated_record_is_treated_as_eof(monkeypatch, capsys):
    data = header({"name": "k", "caps": {}}) + rec(0, e.EV_KEY, e.KEY_A, 1) + b"\x00\x01"
    run_main(monkeypatch, data, capsys)
    k, = FakeUInput.instances
    assert k.writes == [(e.EV_KEY, e.KEY_A, 1), (e.EV_KEY, e.KEY_A, 0), "syn"]


def test_main_bad_device_index_still_releases(monkeypatch, capsys):
    data = header({"name": "k", "caps": {}}) + rec(0, e.EV_KEY, e.KEY_A, 1) + rec(5, e.EV_KEY, e.KEY_B, 1)
    with pytest.raises(IndexError):
        run_main(monkeypatch, data, capsys)
    k, = FakeUInput.instances
    assert (e.EV_KEY, e.KEY_A, 0) in k.writes and k.closed


def test_main_bad_header_exits_before_creating_devices(monkeypatch, capsys):
    with pytest.raises(json.JSONDecodeError):
        run_main(monkeypatch, b"not json\n", capsys)
    assert FakeUInput.instances == []
    assert "READY" not in capsys.readouterr().out
