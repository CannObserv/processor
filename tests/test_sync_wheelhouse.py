"""``scripts/sync_wheelhouse.py`` against a fake bucket (#79).

The script runs standalone, before ``uv sync``, so it is loaded from its path, and
``storage.Client`` is replaced: no test touches ``gs://co-gcs-pypi``.
"""

import importlib.util
from pathlib import Path

import pytest
from google.cloud import storage

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sync_wheelhouse.py"

_spec = importlib.util.spec_from_file_location("sync_wheelhouse", SCRIPT)
sync_wheelhouse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_wheelhouse)


class FakeBlob:
    """What the sync reads of a listed object: its name, size and bytes."""

    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    @property
    def size(self) -> int:
        return len(self.bucket.objects[self.name])

    def download_to_filename(self, filename: str) -> None:
        self.bucket.downloads.append(self.name.removeprefix(sync_wheelhouse.PREFIX))
        Path(filename).write_bytes(self.bucket.objects[self.name])


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []

    def put(self, name: str, data: bytes) -> None:
        self.objects[sync_wheelhouse.PREFIX + name] = data


class FakeClient:
    def __init__(self, bucket: FakeBucket) -> None:
        self.bucket = bucket

    def list_blobs(self, bucket: str, prefix: str) -> list[FakeBlob]:
        assert bucket == sync_wheelhouse.BUCKET
        return [FakeBlob(self.bucket, n) for n in sorted(self.bucket.objects) if n.startswith(prefix)]


@pytest.fixture
def bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    fake = FakeBucket()
    monkeypatch.setattr(sync_wheelhouse.storage, "Client", lambda: FakeClient(fake))
    monkeypatch.setattr(sync_wheelhouse, "DEST", tmp_path / ".wheelhouse")
    return fake


def test_the_summary_names_the_storage_client_it_ran_on(bucket, capsys) -> None:
    """#79's done-when: CI's sync step shows the locked version, not a fresh resolve."""
    bucket.put("co_core-9.9.0-py3-none-any.whl", b"the wheel")
    assert sync_wheelhouse.sync() == 0
    out = capsys.readouterr().out
    assert "1 downloaded" in out
    assert f"google-cloud-storage {storage.__version__}" in out
