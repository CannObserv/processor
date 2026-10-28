"""``scripts/sync_wheelhouse.py`` against a fake bucket (#79), and the digests it
holds a locked file to (#87).

The script runs standalone, before ``uv sync``, so it is loaded from its path, and
``storage.Client`` is replaced: no test touches ``gs://co-gcs-pypi``.

A file ``uv.lock`` names from the wheelhouse is what a release installs, and uv
records no digest for one: a find-links path locks by file name alone, and a
hand-added ``hash`` goes at the next ``uv lock``. So pyproject.toml's
``[tool.processor.wheelhouse-sha256]`` carries one per file, and the sync holds a
locked file to it (as broker#125). The real table is checked here too: every
locked file has a digest, in the one-line form ``scripts/deploy.sh`` reads.
"""

import hashlib
import importlib.util
import re
import tomllib
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
        names = sorted(n for n in self.bucket.objects if n.startswith(prefix))
        return [FakeBlob(self.bucket, n) for n in names]


TABLE = "wheelhouse-sha256"
WHEEL = "co_core-9.9.0-py3-none-any.whl"
AIO = "co_core_aio-9.9.0-py3-none-any.whl"
OTHER = "co_core-0.1.0-py3-none-any.whl"
GOOD = {WHEEL: b"the wheel", AIO: b"the aio", OTHER: b"other"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock(*, source: str = ".wheelhouse") -> str:
    return f"""version = 1

[[package]]
name = "co-core"
version = "9.9.0"
source = {{ registry = "{source}" }}
wheels = [
    {{ path = "{WHEEL}" }},
]

[[package]]
name = "co-core-aio"
version = "9.9.0"
source = {{ registry = "{source}" }}
wheels = [
    {{ path = "{AIO}" }},
]

[[package]]
name = "pydantic"
version = "2.12.0"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.example/pydantic-2.12.0.tar.gz", hash = "sha256:00" }}
"""


def pyproject(digests: dict[str, str]) -> str:
    rows = "".join(f'"{name}" = "{value}"\n' for name, value in digests.items())
    return f'[project]\nname = "t"\n\n[tool.processor.{TABLE}]\n{rows}'


class Checkout:
    """A checkout's root, its wheelhouse, and the bucket the sync mirrors."""

    def __init__(self, tmp: Path, bucket: FakeBucket) -> None:
        self.root = tmp
        self.wheelhouse = tmp / ".wheelhouse"
        self.bucket = bucket
        for name, data in GOOD.items():
            bucket.put(name, data)
        (tmp / "uv.lock").write_text(lock())
        self.pin({WHEEL: sha(GOOD[WHEEL]), AIO: sha(GOOD[AIO])})

    def pin(self, digests: dict[str, str]) -> None:
        (self.root / "pyproject.toml").write_text(pyproject(digests))

    def plant(self, name: str, data: bytes) -> None:
        self.wheelhouse.mkdir(exist_ok=True)
        (self.wheelhouse / name).write_bytes(data)

    def sync(self, capsys) -> tuple[int, str]:
        rc = sync_wheelhouse.sync()
        return rc, capsys.readouterr().err


@pytest.fixture
def bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    fake = FakeBucket()
    monkeypatch.setattr(sync_wheelhouse.storage, "Client", lambda: FakeClient(fake))
    monkeypatch.setattr(sync_wheelhouse, "PROJECT", tmp_path, raising=False)
    monkeypatch.setattr(sync_wheelhouse, "DEST", tmp_path / ".wheelhouse")
    return fake


@pytest.fixture
def co(tmp_path: Path, bucket: FakeBucket) -> Checkout:
    return Checkout(tmp_path, bucket)


def test_the_summary_names_the_storage_client_it_ran_on(co, capsys) -> None:
    """#79's done-when: CI's sync step shows the locked version, not a fresh resolve."""
    assert sync_wheelhouse.sync() == 0
    out = capsys.readouterr().out
    assert "3 downloaded" in out
    assert f"google-cloud-storage {storage.__version__}" in out


class TestALockedFile:
    def test_a_same_size_wheel_with_other_bytes_is_fetched_again(self, co, capsys) -> None:
        """#87's red: the swap a size-only skip kept forever. Same name, same size."""
        planted = b"the WHEEL"
        assert len(planted) == len(GOOD[WHEEL])
        co.plant(WHEEL, planted)
        rc, err = co.sync(capsys)
        assert rc == 0, err
        assert (co.wheelhouse / WHEEL).read_bytes() == GOOD[WHEEL]
        assert WHEEL in co.bucket.downloads

    def test_a_download_that_is_not_the_pinned_file_is_refused(self, co, capsys) -> None:
        """What a spoofed channel serves: its own wheel, and a size to match."""
        co.bucket.put(WHEEL, b"the WHEEL")
        rc, err = co.sync(capsys)
        assert rc == 1
        assert WHEEL in err
        assert sha(b"the WHEEL") in err and sha(GOOD[WHEEL]) in err
        assert not (co.wheelhouse / WHEEL).exists()
        assert not list(co.wheelhouse.glob(".*.part"))

    def test_a_planted_file_is_left_as_found_when_the_refetch_misses_too(self, co, capsys) -> None:
        """Evidence for whoever reads why; deploy.sh refuses it either way."""
        co.plant(WHEEL, b"the wheeL")
        co.bucket.put(WHEEL, b"the WHEEL")
        rc, err = co.sync(capsys)
        assert rc == 1
        assert WHEEL in err
        assert (co.wheelhouse / WHEEL).read_bytes() == b"the wheeL"

    def test_a_locked_file_the_bucket_does_not_list_fails_the_sync(self, co, capsys) -> None:
        """The loop visits what the listing names, so a copy planted under a name
        the bucket lacks would never be held to its pin (broker#125 CR 1)."""
        del co.bucket.objects[sync_wheelhouse.PREFIX + WHEEL]
        co.plant(WHEEL, b"the WHEEL")
        rc, err = co.sync(capsys)
        assert rc == 1
        assert WHEEL in err and "does not list" in err

    def test_a_file_that_matches_its_pin_is_not_fetched_again(self, co, capsys) -> None:
        co.plant(WHEEL, GOOD[WHEEL])
        co.plant(AIO, GOOD[AIO])
        rc, err = co.sync(capsys)
        assert rc == 0, err
        assert WHEEL not in co.bucket.downloads and AIO not in co.bucket.downloads

    def test_a_locked_file_with_no_digest_fails_the_sync(self, co, capsys) -> None:
        co.pin({WHEEL: sha(GOOD[WHEEL])})
        rc, err = co.sync(capsys)
        assert rc == 1
        assert AIO in err and TABLE in err
        assert "sha256sum .wheelhouse/" in err, "how to make the pin"
        assert not co.bucket.downloads
        # CR 2: a local pin error never reached GCS, so it doesn't read as one.
        assert "cannot hold the wheelhouse to its pins" in err
        assert "could not sync gs://" not in err

    @pytest.mark.parametrize("value", ["", "abc123", "A" * 64])
    def test_a_pin_that_is_not_a_sha256_fails_the_sync(self, co, capsys, value) -> None:
        """A blank pin is falsy, and must not fall back to the size skip (broker#125 CR 5)."""
        co.pin({WHEEL: value, AIO: sha(GOOD[AIO])})
        co.plant(WHEEL, b"the WHEEL")
        rc, err = co.sync(capsys)
        assert rc == 1
        assert WHEEL in err and "not a sha256" in err
        assert not co.bucket.downloads

    def test_a_lock_that_spells_the_wheelhouse_as_pyproject_does_is_still_read(
        self, co, capsys
    ) -> None:
        """deploy.sh's locked_wheels() takes ./.wheelhouse too."""
        (co.root / "uv.lock").write_text(lock(source="./.wheelhouse"))
        co.plant(WHEEL, b"the WHEEL")
        rc, err = co.sync(capsys)
        assert rc == 0, err
        assert (co.wheelhouse / WHEEL).read_bytes() == GOOD[WHEEL]


class TestAFileTheLockDoesNotName:
    def test_a_same_size_file_is_still_skipped(self, co, capsys) -> None:
        """Nothing installs it: the bucket holds every co-core ever published, and
        only the locked ones reach a release. It meets its pin when a lock names it."""
        co.plant(OTHER, b"OTHER")
        rc, err = co.sync(capsys)
        assert rc == 0, err
        assert OTHER not in co.bucket.downloads
        assert (co.wheelhouse / OTHER).read_bytes() == b"OTHER"


def real_locked_files() -> set[str]:
    return set(sync_wheelhouse.locked_files(tomllib.loads((REPO / "uv.lock").read_text())))


def real_pins() -> dict[str, str]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["processor"][TABLE]


class TestTheRealPin:
    def test_the_lock_names_the_three_cannobserv_wheels(self) -> None:
        assert {n.split("-")[0] for n in real_locked_files()} == {
            "co_core",
            "co_core_aio",
            "co_core_sync",
        }

    def test_every_locked_wheelhouse_file_has_a_digest_and_no_digest_outlives_one(self) -> None:
        assert set(real_pins()) == real_locked_files()

    def test_the_table_is_in_the_form_deploy_sh_reads(self) -> None:
        """deploy.sh reads the table with awk, a line at a time: one quoted name, one
        quoted digest. tomllib takes more, so the form is held here."""
        text = (REPO / "pyproject.toml").read_text()
        section = re.search(rf"^\[tool\.processor\.{TABLE}\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
        assert section, f"[tool.processor.{TABLE}] is not a table header of its own"
        rows = {}
        for line in section.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r'"([^"/]+)" = "([0-9a-f]{64})"', line)
            assert match, f"not a line deploy.sh reads: {line!r}"
            rows[match.group(1)] = match.group(2)
        assert rows == real_pins()

    def test_the_wheelhouse_here_holds_the_pinned_files(self) -> None:
        """Where a wheelhouse is populated: co-processor (built locally), and CI after
        its from-empty sync, which is the bucket's side of the comparison."""
        wheelhouse = REPO / ".wheelhouse"
        present = {n: wheelhouse / n for n in real_pins() if (wheelhouse / n).is_file()}
        if not present:
            pytest.skip("no populated wheelhouse here")
        for name, path in present.items():
            assert sha(path.read_bytes()) == real_pins()[name], name
