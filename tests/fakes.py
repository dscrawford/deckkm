"""Hardware-free stand-ins for evdev devices and the ssh subprocess."""
import io
import os
import subprocess
from types import SimpleNamespace


def ev(type_, code, value):
    return SimpleNamespace(type=type_, code=code, value=value)


class FakeDev:
    """Looks like evdev.InputDevice; readable fd is a pipe we poke on push()."""

    def __init__(self, name, caps, path="/dev/input/eventX"):
        self.name, self._caps, self.path = name, caps, path
        self.fd, self._w = os.pipe()
        self._events, self.keys = [], set()
        self.closed = self.grabbed = False

    def capabilities(self, absinfo=False):
        return self._caps

    def active_keys(self):
        return list(self.keys)

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        self.closed = True

    def push(self, *events):
        self._events.extend(events)
        os.write(self._w, b"x")

    def read(self):
        os.read(self.fd, 4096)
        evs, self._events = self._events, []
        return iter(evs)


class FakeProc:
    """Looks like subprocess.Popen(ssh ...): stdin/stdout are real pipes."""
    instances = []

    def __init__(self):
        FakeProc.instances.append(self)
        self.stdin_r, w = os.pipe()
        self.stdin = os.fdopen(w, "wb")
        r, self.stdout_w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        try:
            os.close(self.stdin_r)  # reader gone -> writer gets EPIPE
        except OSError:
            pass

    terminate = kill

    def remote_says(self, data):
        os.write(self.stdout_w, data)

    def cleanup(self):
        for f in (self.stdin, self.stdout):
            try:
                f.close()
            except OSError:
                pass
        for fd in (self.stdin_r, self.stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass


class FakeUInput:
    """Records writes instead of touching /dev/uinput."""
    instances = []

    def __init__(self, caps, name="py-evdev-uinput", **kw):
        self.caps, self.name, self.writes, self.closed = caps, name, [], False
        FakeUInput.instances.append(self)

    def write(self, t, c, v):
        self.writes.append((t, c, v))

    def syn(self):
        self.writes.append("syn")

    def close(self):
        self.closed = True
