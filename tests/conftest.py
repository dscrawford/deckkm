import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _cleanup_fake_procs():
    from fakes import FakeProc
    yield
    while FakeProc.instances:
        FakeProc.instances.pop().cleanup()
