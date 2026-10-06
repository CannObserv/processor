"""Configuration: ``CO_PROCESSOR_*`` environment variables via pydantic-settings.

Production values live in ``/etc/processor/.env``, loaded by the systemd unit.
Never ``os.getenv``.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from processor._contain import Containment

GiB = 1024**3

# What a command may spend after its extraction before it counts as idle: the
# output store write and the publish, each bounded by its client's timeout.
_POST_EXTRACTION_ALLOWANCE_MS = 60_000


class Settings(BaseSettings):
    """Every knob, with the spec's defaults."""

    model_config = SettingsConfigDict(env_prefix="CO_PROCESSOR_", extra="ignore")

    # The bus: redis://processor:<pw>@broker:6379/0 — the MagicDNS name, never the
    # address, which a broker rebuild changes (#8).
    bus_url: SecretStr
    consumer_name: str = "co-processor"
    # > 0: XREADGROUP reads BLOCK 0 as "block forever", past the socket timeout.
    read_block_ms: int = Field(default=5_000, gt=0)

    # Stores (spec §2): Replicator's raw blobs in, derived text out.
    store_backend: Literal["gcs", "local"] = "gcs"
    input_bucket: str = "co-gcs-blobs"
    input_prefix: str = "blobs"
    output_bucket: str = "co-gcs-processor"
    output_prefix: str = "blobs"
    local_input_root: Path | None = None
    local_output_root: Path | None = None

    # The child (spec §3) and the retry cap (spec §4).
    extraction_timeout_s: float = Field(default=120, gt=0)
    rlimit_as_bytes: int = Field(default=3 * GiB, ge=0)  # 0: no limit
    # required: `processor run` refuses to start where the child cannot contain
    # itself (Landlock ABI >= 6, a known seccomp arch). off: dev and CI only (#2).
    child_containment: Containment = "required"
    max_attempts: int = Field(default=3, ge=1)
    reclaim_min_idle_ms: int = 600_000
    reclaim_interval_s: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        floor = int(self.extraction_timeout_s * 1000) + _POST_EXTRACTION_ALLOWANCE_MS
        if self.reclaim_min_idle_ms <= floor:
            raise ValueError(
                f"reclaim_min_idle_ms ({self.reclaim_min_idle_ms}) must exceed the extraction "
                f"timeout plus {_POST_EXTRACTION_ALLOWANCE_MS} ms ({floor}), or a slow command "
                "is reclaimed from under itself"
            )
        if self.store_backend == "local" and not (self.local_input_root and self.local_output_root):
            raise ValueError("store_backend=local needs local_input_root and local_output_root")
        return self
