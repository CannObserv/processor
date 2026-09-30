"""Input and output store builders (spec §2, §3)."""

from pathlib import Path

import pytest
from co_core_sync.drivers.blobstore.gcs import GcsBlobStore
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from processor.settings import Settings
from processor.stores import build_stores

HEX = "ab" * 32


@pytest.fixture
def gcs_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", "redis://localhost:6379/0")
    return Settings()


def test_gcs_stores_address_the_cohort_buckets(gcs_settings: Settings) -> None:
    client = storage.Client(project="test", credentials=AnonymousCredentials())
    stores = build_stores(gcs_settings, client=client)
    assert isinstance(stores.input, GcsBlobStore) and isinstance(stores.output, GcsBlobStore)
    assert stores.input.uri_for(HEX) == f"gs://co-gcs-blobs/blobs/{HEX}.bin"
    assert stores.output.uri_for(HEX) == f"gs://co-gcs-processor/blobs/{HEX}.bin"


def test_local_stores_for_development(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CO_PROCESSOR_STORE_BACKEND", "local")
    monkeypatch.setenv("CO_PROCESSOR_LOCAL_INPUT_ROOT", str(tmp_path / "in"))
    monkeypatch.setenv("CO_PROCESSOR_LOCAL_OUTPUT_ROOT", str(tmp_path / "out"))
    stores = build_stores(Settings())
    assert isinstance(stores.input, LocalBlobStore) and isinstance(stores.output, LocalBlobStore)
    assert stores.input.root == tmp_path / "in"
    assert stores.output.root == tmp_path / "out"
