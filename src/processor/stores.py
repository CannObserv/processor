"""The input and output ``BlobStore``s (spec §2, §3).

Input: Replicator's raw blobs, read-only. Output: derived text, write-if-absent,
never deleted. Both are co-core stores behind the same Protocol, so the handler
is tested against ``LocalBlobStore`` and runs against ``GcsBlobStore``.
"""

from dataclasses import dataclass

from co_core.pure.util.blobstore import BlobStore
from co_core_sync.drivers.blobstore.gcs import GcsBlobStore
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from google.cloud import storage

from processor.settings import Settings


@dataclass(frozen=True)
class Stores:
    """Replicator's raw blobs (``input``) and the derived-text store (``output``)."""

    input: BlobStore
    output: BlobStore


def build_stores(settings: Settings, *, client: storage.Client | None = None) -> Stores:
    """Build both stores. GCS resolves credentials here, at boot, not on first use."""
    if settings.store_backend == "local":
        return Stores(
            input=LocalBlobStore(settings.local_input_root),
            output=LocalBlobStore(settings.local_output_root),
        )
    client = client or storage.Client()
    return Stores(
        input=GcsBlobStore(settings.input_bucket, prefix=settings.input_prefix, client=client),
        output=GcsBlobStore(settings.output_bucket, prefix=settings.output_prefix, client=client),
    )
