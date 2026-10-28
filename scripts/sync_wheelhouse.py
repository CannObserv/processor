"""Mirror the private cannobserv package index into the local wheelhouse (from Watcher).

Downloads every object under ``gs://co-gcs-pypi/wheels/`` into ``./.wheelhouse``
(repo root), skipping any file already present with a matching size, except a
file ``uv.lock`` names, which a release installs. That one is held to the sha256
pyproject.toml's ``[tool.processor.wheelhouse-sha256]`` pins (#87, as
broker#125): fetched again when the copy here misses it, and refused when the
download misses it too. A size, or GCS's own checksum, comes over the same TLS as
the bytes, so neither tells a spoofed bucket from the real one. ``uv`` then
resolves ``co-core`` / ``co-core-aio`` from that directory via the ``[tool.uv]
find-links`` entry in ``pyproject.toml`` (watcher#220).

Runs standalone, *before* ``uv sync`` — it must not import the project (whose
deps are what the wheelhouse provides). Its own deps are uv.lock's ``wheelhouse``
group, which ``--only-group`` installs without the project (#79, as broker#113).
``--no-config`` matters: without it uv reads ``[tool.uv] find-links``, which names
the ``./.wheelhouse`` this script is about to create:

    uv run --frozen --no-config --only-group wheelhouse python scripts/sync_wheelhouse.py

Authentication is Application Default Credentials. On the VM/deploy that is the
service-account key at ``GOOGLE_APPLICATION_CREDENTIALS`` (set in
``/etc/processor/.env``); in CI it is the ADC file written by
``google-github-actions/auth`` (keyless Workload Identity Federation). Either
way the identity needs only ``roles/storage.objectViewer`` on the bucket.

Exit codes: ``0`` success (including a no-op re-run) · ``1`` failure (auth,
network, a missing bucket, a locked file with no pinned digest or one the bucket
does not list, or a download that is not the pinned file). Run by hand before
``uv sync``; the ``processor`` unit does not run it, and neither does
``scripts/deploy.sh``, which checks what it finds.
"""

import argparse
import hashlib
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from google.cloud import storage

BUCKET = "co-gcs-pypi"
PREFIX = "wheels/"
PROJECT = Path(__file__).resolve().parent.parent
DEST = PROJECT / ".wheelhouse"
# pyproject.toml's table of digests, under [tool.processor]: one per locked file.
TABLE = "wheelhouse-sha256"
# How uv.lock names the wheelhouse as a source; deploy.sh's locked_wheels() takes both.
WHEELHOUSE_SOURCES = (".wheelhouse", "./.wheelhouse")


def locked_files(lock: dict) -> list[str]:
    """The wheelhouse files a parsed uv.lock names: the sdist and wheel paths of each
    package whose source is the wheelhouse, as deploy.sh copies them."""
    names = []
    for package in lock.get("package", []):
        if package.get("source", {}).get("registry") not in WHEELHOUSE_SOURCES:
            continue
        for dist in [package.get("sdist", {}), *package.get("wheels", [])]:
            if "path" in dist:
                names.append(dist["path"])
    return names


def pins() -> dict[str, str]:
    """``{file: sha256}`` for each file the checkout's uv.lock names; raises for one
    pyproject.toml pins no digest for, or pins with something not a sha256."""
    lock = tomllib.loads((PROJECT / "uv.lock").read_text())
    pyproject = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    table = pyproject.get("tool", {}).get("processor", {}).get(TABLE, {})
    names = locked_files(lock)
    missing = [name for name in names if name not in table]
    if missing:
        raise ValueError(
            f"uv.lock names {', '.join(missing)}, which pyproject.toml's"
            f" [tool.processor.{TABLE}] pins no digest for: pin each from"
            " `sha256sum .wheelhouse/<file>` (docs/DEPLOYMENT.md § Bumping co-core)"
        )
    # As deploy.sh holds them: a blank one would read as no pin, and the size skip.
    malformed = [name for name in names if not re.fullmatch(r"[0-9a-f]{64}", str(table[name]))]
    if malformed:
        raise ValueError(
            f"pyproject.toml's [tool.processor.{TABLE}] pins {', '.join(malformed)}"
            " with a value that is not a sha256 (64 lowercase hex)"
        )
    return {name: table[name] for name in names}


def sha256(path: str | Path) -> str:
    """The hex sha256 of a file's bytes."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def sync(bucket: str = BUCKET, prefix: str = PREFIX) -> int:
    """Mirror ``gs://{bucket}/{prefix}`` into ``DEST``; return an exit code."""
    DEST.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = 0
    try:
        pinned = pins()
        unlisted = set(pinned)
        client = storage.Client()
        for blob in client.list_blobs(bucket, prefix=prefix):
            name = blob.name.removeprefix(prefix)
            if not name:  # the prefix "directory" placeholder object, if any
                continue
            target = DEST / name
            digest = pinned.get(name)
            unlisted.discard(name)
            # A locked file is skipped only when it is the pinned one. Any other is
            # skipped when a same-size file is present: published artifacts are
            # server-side immutable (cannobserv#215), nothing installs it, and it
            # meets its pin when a lock names it.
            if target.exists() and (
                sha256(target) == digest
                if digest is not None
                else target.stat().st_size == blob.size
            ):
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Download to a sibling temp file then atomically rename, so an
            # interrupted run never leaves a partial wheel in place (which a
            # concurrent reader, or a same-size coincidence, could mistake for
            # a complete one). os.replace is atomic within the same directory.
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
            os.close(fd)
            try:
                blob.download_to_filename(tmp)
                # Refused before it replaces anything: a planted copy stays as
                # found, for whoever reads why, and deploy.sh refuses it too.
                if digest is not None and (got := sha256(tmp)) != digest:
                    raise ValueError(
                        f"{name} downloads with sha256 {got}, not the {digest}"
                        f" pyproject.toml's [tool.processor.{TABLE}] pins"
                    )
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)
            downloaded += 1
        # A locked file the loop never reached was held to nothing: a copy here
        # under that name would pass to uv sync unchecked.
        if unlisted:
            raise ValueError(
                f"uv.lock names {', '.join(sorted(unlisted))}, which the bucket does not list"
            )
    except Exception as exc:  # broad by design: auth/network/bucket failures degrade identically
        print(f"error: could not sync gs://{bucket}/{prefix}: {exc}", file=sys.stderr)
        return 1

    # The client's version too: CI's log then shows the locked one (#79).
    print(
        f"wheelhouse in sync: {downloaded} downloaded, {skipped} already present -> {DEST}"
        f" (google-cloud-storage {storage.__version__})"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mirror the cannobserv index into .wheelhouse.")
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--prefix", default=PREFIX)
    cli = parser.parse_args()
    sys.exit(sync(cli.bucket, cli.prefix))
