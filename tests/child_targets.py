"""Transforms that misbehave on purpose, run in the child by ``tests/test_child.py``."""

import gc
import io
import os
import pickle
import signal
import socket
import subprocess
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


def flood_stderr_then_exit(n_bytes: int) -> None:
    chunk = b"w" * 65536
    for _ in range(n_bytes // len(chunk)):
        os.write(2, chunk)
    os.write(2, b"the last words")
    os._exit(3)


def write_fd_1() -> str:
    os.write(1, b"what C code or a subprocess writes to fd 1")
    return "result"


def forge_protocol(kind, payload) -> None:
    """What a compromised child could send: a well-formed pickle off the protocol."""
    (channel,) = [
        o
        for o in gc.get_objects()
        if isinstance(o, io.BufferedWriter) and not o.closed and o.fileno() > 2
    ]
    channel.write(pickle.dumps((kind, payload)))
    channel.flush()
    os._exit(0)


# What a child compromised by a document would try (#2). Each answers ["ok", detail]
# or ["denied", errno], so a test can tell a refusal from a missing target.


def _attempt(action) -> list:
    try:
        return ["ok", action()]
    except OSError as exc:
        return ["denied", exc.errno]


def try_read(path: str) -> list:
    def read() -> int:
        with open(path, "rb") as f:
            return len(f.read(64))

    return _attempt(read)


def try_write(path: str) -> list:
    def write() -> int:
        with open(path, "w") as f:
            return f.write("written by the child")

    return _attempt(write)


def try_connect(family: str, address) -> list:
    def connect() -> str:
        with socket.socket(getattr(socket, family)) as s:
            s.connect(tuple(address) if isinstance(address, list) else address)
        return "connected"

    return _attempt(connect)


def try_signal_parent() -> list:
    return _attempt(lambda: os.kill(os.getppid(), 0))


def try_exec(path: str) -> list:
    def run() -> int:
        return subprocess.run([path, "-c", "exit 0"], check=False).returncode

    return _attempt(run)
