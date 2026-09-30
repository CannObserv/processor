"""Transforms that misbehave on purpose, run in the child by ``tests/test_child.py``."""

import os
import signal
import sys
import time
from collections import OrderedDict


def echo(*args):
    return args


def raise_value_error():
    raise ValueError("bad document")


def allocate(n_bytes: int) -> int:
    return len(bytearray(n_bytes))


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def exit_hard(code: int) -> None:
    os._exit(code)


def kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def noisy_stdout() -> str:
    print("chatter a library wrote to stdout")
    sys.stdout.write("more chatter\n")
    return "result"


def environment() -> dict[str, str]:
    return dict(os.environ)


def pid_then_sleep(path: str, seconds: float) -> None:
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    time.sleep(seconds)


class _Evil:
    """What a compromised child could send: a pickle that calls a global on load."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self):
        return (os.system, (f"touch {self.marker}",))


def evil(marker: str) -> _Evil:
    return _Evil(marker)


def ordered_dict() -> OrderedDict:
    return OrderedDict(a=1)


def oom_score_adj() -> str:
    with open("/proc/self/oom_score_adj") as f:
        return f.read().strip()
