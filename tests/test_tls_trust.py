"""The services' TLS trust is certifi's bundle, and it must not trust emSign ECC G3 (#66).

GCS (``google-auth`` / ``google-cloud-storage`` over ``requests``) and the drift check's
GitHub calls verify against ``certifi.where()``, not the system store, so exe.dev's
system-store remediation of the eMudhra compromise doesn't reach them
(https://exe.dev/security/emudhra-compromise). certifi 2026.10.19 drops
``emSign ECC Root CA - G3``, the compromised chains' root. A later lock must not bring
it back: ``[tool.uv] constraint-dependencies`` holds the floor, and these tests check it.
"""

import ssl
import tomllib
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
FLOOR = (2026, 10, 19)
DISTRUSTED = "emSign ECC Root CA - G3"


def _dated(version: str) -> tuple[int, ...]:
    # The lock normalises 2026.07.22 to 2026.7.22; compare parts, not strings.
    return tuple(int(part) for part in version.split("."))


def _bundle_common_names() -> set[str]:
    # The certificates themselves, not cacert.pem's comment lines.
    certs = ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
    return {dict(rdn[0] for rdn in c["subject"]).get("commonName", "") for c in certs}


def test_dated_compares_parts() -> None:
    assert _dated("2026.7.22") == _dated("2026.07.22") < FLOOR <= _dated("2026.10.19")


def test_lock_resolves_certifi_at_the_floor() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (locked,) = [p["version"] for p in lock["package"] if p["name"] == "certifi"]
    assert _dated(locked) >= FLOOR, locked


def test_installed_certifi_is_at_the_floor() -> None:
    assert _dated(certifi.__version__) >= FLOOR, certifi.__version__


def test_the_bundle_does_not_trust_emsign_ecc_g3() -> None:
    names = _bundle_common_names()
    # Control: the matcher sees eMudhra's roots, and the one 2026.10.19 keeps.
    assert "emSign Root CA - G1" in names
    assert DISTRUSTED not in names
