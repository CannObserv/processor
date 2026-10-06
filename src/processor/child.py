"""Run a transform in a fresh, killable child process (spec §3, D6).

One interpreter per command (``python -I -m processor._child``): killed on timeout,
its exit status reported on a crash, ``RLIMIT_AS`` set before the extractors load.
A thread cannot be killed, and one PDF that wedges pypdf would stall the consumer
forever. At ≤ ~100 commands/day the start-up cost is noise.

The child parses untrusted documents, so it is treated as untrusted too:

- a scrubbed environment (never the broker credential or the GCS key);
- under ``containment="required"`` (production's default), it contains itself
  before it reads its request: Landlock and seccomp (``processor._contain``, #2). It
  can then read only the interpreter, its libraries and the code, write nothing,
  open no socket, and signal nothing outside itself;
- its result is decoded by an unpickler that resolves no global but
  ``ExtractOutcome``, so a child compromised by a document cannot make the parent
  run code.

A child that cannot contain itself exits 70 before it runs anything: a crash (a
strike), never a terminal verdict. ``processor run`` refuses to start where that
would happen, so in production it is a backstop.
"""

import asyncio
import io
import os
import pickle
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from processor._contain import Containment

ChildKind = Literal["ok", "raised", "timeout", "crashed"]

_STDERR_TAIL = 2000

# The only globals a child's result may reference. Builtin containers, bytes, str,
# numbers, bool and None decode through opcodes and need no entry.
_ALLOWED_GLOBALS = {("processor.processors.extract", "ExtractOutcome")}


@dataclass(frozen=True)
class ChildResult:
    """``ok`` carries ``value``; ``raised`` / ``timeout`` / ``crashed`` carry ``detail``."""

    kind: ChildKind
    value: Any = None
    detail: str = ""
    returncode: int | None = None


def transform_target(fn: Callable) -> str:
    """The ``module:function`` reference the child imports ``fn`` by."""
    return f"{fn.__module__}:{fn.__qualname__}"


class _ResultUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in _ALLOWED_GLOBALS:
            raise pickle.UnpicklingError(f"global {module}.{name} is not allowed")
        return super().find_class(module, name)


def _decode(out: bytes) -> tuple[str, Any]:
    return _ResultUnpickler(io.BytesIO(out)).load()


def _child_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}


async def run_in_child(
    target: str,
    args: tuple,
    *,
    timeout_s: float,
    rlimit_as_bytes: int,
    containment: Containment,
    sys_path: Sequence[str] = (),
) -> ChildResult:
    """Run ``target(*args)`` in a child; never raises for the child's own failures.

    ``containment`` has no default: every caller says which mode it runs.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "processor._child",
        containment,
        str(rlimit_as_bytes),
        *sys_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
        cwd="/",
    )
    try:
        out, err = await asyncio.wait_for(
            _exchange(proc, pickle.dumps((target, args))), timeout=timeout_s
        )
    except TimeoutError:
        return ChildResult(kind="timeout", detail=f"no result after {timeout_s:g}s")
    finally:
        if proc.returncode is None:  # timed out, or the caller was cancelled
            proc.kill()
            await proc.wait()

    stderr_tail = err.decode("utf-8", "replace")
    if proc.returncode != 0:
        return ChildResult(
            kind="crashed",
            detail=f"exit status {proc.returncode}: {stderr_tail}".rstrip(),
            returncode=proc.returncode,
        )
    try:
        kind, payload = _decode(out)
    except Exception as exc:
        return ChildResult(
            kind="crashed", detail=f"unreadable result ({exc}): {stderr_tail}", returncode=0
        )
    if kind == "ok":
        return ChildResult(kind="ok", value=payload, returncode=0)
    if kind == "raised" and isinstance(payload, str):
        return ChildResult(kind="raised", detail=payload, returncode=0)
    # Anything else is not the child's protocol: a crash (a strike), never a terminal
    # extraction_error carrying whatever the child chose to send as its "detail".
    return ChildResult(kind="crashed", detail=f"malformed result: {stderr_tail}", returncode=0)


async def _exchange(proc: asyncio.subprocess.Process, request: bytes) -> tuple[bytes, bytes]:
    """``communicate``, keeping only the tail of stderr.

    The child is untrusted: a document that makes a library warn in a loop must not
    grow the parent by the whole stream. stdout, the result, is read in full.
    """

    async def feed() -> None:
        try:
            proc.stdin.write(request)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child exited without reading it all; its exit status says why
        finally:
            proc.stdin.close()

    async def tail() -> bytes:
        kept = b""
        while chunk := await proc.stderr.read(64 * 1024):
            kept = (kept + chunk)[-_STDERR_TAIL:]
        return kept

    _, out, err = await asyncio.gather(feed(), proc.stdout.read(), tail())
    await proc.wait()
    return out, err
