"""Child-process entry point.

``python -I -m processor._child <containment> <rlimit_as> [sys_path ...]``

Reads a pickled ``(target, args)`` from stdin, runs ``target`` ("module:function"),
and writes a pickled ``("ok", value)`` or ``("raised", "Type: message")`` to stdout.
A hard exit, a signal, a garbled payload, or a transform that fails to import is the
parent's "crashed": a strike, never a terminal verdict on the document.

Only the stdlib is imported at module level (``processor._contain`` is stdlib only),
and the transform's module is imported **after** ``RLIMIT_AS`` is set: the limit then
covers the extractor libraries too, and a memory-hungry document raises
``MemoryError`` here instead of drawing the OOM killer (spec §3). That deferred
import is the one deliberate exception to the imports-at-top convention.

The child also raises its own ``oom_score_adj`` to the maximum (always permitted): if
the host still reaches the OOM killer, the child dies rather than the consumer, and
the unit's ``OOMPolicy=continue`` turns the loss into a counted crash.

Then, under ``required``, it contains itself (``processor._contain``, #2) before it
reads its request: from there it reads only its allowlist, writes nothing, opens no
socket and signals nothing outside itself. A child that cannot contain itself exits
70 without reading the request, which the parent counts as a crash.

It ignores ``SIGINT``: a terminal Ctrl+C reaches the whole foreground process group,
the parent's answer to it is "finish the in-flight command", and a
``KeyboardInterrupt`` caught below would come back as ``raised`` — a terminal
``extraction_error`` for a healthy document. The parent's timeout kill is ``SIGKILL``.
"""

import importlib
import os
import pickle
import resource
import signal
import sys

from processor._contain import ContainmentUnavailable, contain

# EX_SOFTWARE: the parent reports it as "crashed", with the reason from stderr.
EXIT_UNCONTAINED = 70


def main(argv: list[str]) -> int:
    """Run one target, limited and contained; exit 0 unless killed or uncontained."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass  # not Linux, or /proc unavailable: the RLIMIT_AS is the real guard
    mode, limit = argv[0], int(argv[1])
    if limit > 0:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    sys.path[:0] = argv[2:]

    # The result channel is the real stdout, moved to a private descriptor; fd 1 and
    # sys.stdout then point at stderr, so nothing a library prints — from Python, from
    # C, or from a process it spawns — can land in the channel.
    channel = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    try:
        contain(mode)
    except ContainmentUnavailable as exc:
        print(f"child containment failed: {exc}", file=sys.stderr)
        return EXIT_UNCONTAINED

    target, args = pickle.load(sys.stdin.buffer)
    module_name, _, name = target.partition(":")
    # Outside the try: a module that will not import (a venv mid-`uv sync`, source
    # changed under a running unit) is the host's failure, not the document's. It
    # exits non-zero, and the parent counts a crash rather than publishing a terminal
    # extraction_error for a healthy document.
    transform = getattr(importlib.import_module(module_name), name)
    try:
        payload = pickle.dumps(("ok", transform(*args)))
    except BaseException as exc:  # MemoryError under RLIMIT_AS included
        payload = pickle.dumps(("raised", f"{type(exc).__name__}: {exc}"))
    channel.write(payload)
    channel.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
