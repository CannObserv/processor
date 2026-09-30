"""Child-process entry point: ``python -I -m processor._child <rlimit_as> [sys_path ...]``.

Reads a pickled ``(target, args)`` from stdin, runs ``target`` ("module:function"),
and writes a pickled ``("ok", value)`` or ``("raised", "Type: message")`` to stdout.
A hard exit, a signal, or a garbled payload is the parent's "crashed".

Only the stdlib is imported at module level, and the transform's module is imported
**after** ``RLIMIT_AS`` is set: the limit then covers the extractor libraries too,
and a memory-hungry document raises ``MemoryError`` here instead of drawing the OOM
killer (spec §3). That deferred import is the one deliberate exception to the
imports-at-top convention.
"""

import importlib
import pickle
import resource
import sys


def main(argv: list[str]) -> int:
    """Run one target under the address-space limit; always exit 0 unless killed."""
    limit = int(argv[0])
    if limit > 0:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    sys.path[:0] = argv[1:]

    # The result channel is the real stdout; anything a library prints goes to stderr.
    channel = sys.stdout.buffer
    sys.stdout = sys.stderr

    target, args = pickle.load(sys.stdin.buffer)
    module_name, _, name = target.partition(":")
    try:
        value = getattr(importlib.import_module(module_name), name)(*args)
        payload = pickle.dumps(("ok", value))
    except BaseException as exc:  # MemoryError under RLIMIT_AS included
        payload = pickle.dumps(("raised", f"{type(exc).__name__}: {exc}"))
    channel.write(payload)
    channel.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
