# Processor — the cohort's discrete-transform service — design

**Status:** design approved section by section 2026-09-29 (brainstorming in an
Observo session); this document is the founding spec for `CannObserv/processor`.
**Issue:** CannObserv/processor#1, recreated from observo#629 (observo is
private; GitHub refuses a private → public transfer). **Supersedes:** #629's
premise that Observo is the processor.
**Contract of record:** co-core `docs/CHANGE_BUS.md` § *Watcher-issued processing
— the `content.process` pair* (cannobserv v0.19.4), and Watcher's
`docs/plans/2026-09-24-observo-extraction-and-diff-design.md` Section 1. The
co-core deltas that shaped the wire types:
cannobserv `docs/plans/2026-09-24-content-process-contract-co-core-deltas.md`.

Written for the agent that builds Processor on `co-processor`, who has not seen
the conversation that produced it. Every co-core name below was checked against
co-core 0.19.5 in Observo's venv; confirm each against the pinned 0.19.4 before
relying on it.

---

## Why a new service

Watcher needs a processor for `content.process` commands: read a raw blob,
extract text from one `source_spec`, store the canonical text permanently by
hash, publish `content.derived` (#629, Watcher design Section 3). The design
first named Observo. Reviewing it showed a mismatch of shape, not of capability.

- **Observo is a stream engine.** Its Job machinery — N+1 processes per Job,
  scheduler, completion fan-in, fleet placement, a 6 GiB service cgroup — is
  sized for hours-long media work. Its one discrete path, the #488 `asset` Job
  (`pdf_extract_text`), pays that whole cost for a sub-second PDF parse, and its
  consumer deliberately biases itself as the OOM victim beside the video
  consumers.
- **Discrete transforms are a class, not a one-off.** Watcher's text extraction
  is first; Observo's own event work needs agendas and meeting materials
  transformed next; JSON extraction (cannobserv#354), OCR and similar follow.
- **Transformation separates cleanly from interpretation.** Taking agendas as
  the example: a generic transform (any agenda → chronological item list) and
  an org adapter (TVW's agenda format → precise items) are both
  transformation, owned here; applying the result to events, timelines and
  speakers is interpretation, owned by the issuer (Observo). The TVW agenda
  adapter belongs to Processor, not Observo.
- **Headless.** Spec authoring stays with the issuer (Watcher edits its specs
  today); human correction happens at interpretation. Processor has no UI.

Considered and rejected: a headless tier inside the Observo repo, extractable
later (cheaper now, but the cohort is heading to a separate service, and it ties
extraction to Observo's co-core bump cadence — Section 5); a `transform`
JobKind in Observo (makes Watcher's commands Observo Jobs, and gives the tier
state and UI it should not have).

## Decisions

| # | Decision |
|---|---|
| D1 | The service is **Processor**: repo `CannObserv/processor`, VM `co-processor`. `command.processor` names a transform *within* it (`"extract"` in v1). |
| D2 | **Headless and stateless in v1**: no UI, no database. Idempotency is the write-if-absent store; correlation and dedupe are the issuer's (by `command_id`). |
| D3 | **Own identities**: broker ACL user, GCS service account, bucket, tailnet tag. Observo's broker#62 `observo` user is stripped. |
| D4 | **Own bucket** `gs://co-gcs-processor`, derived outputs only, so Watcher's read grant is a plain bucket-level `objectViewer`. |
| D5 | **co-core pinned exactly, matched to Watcher** (0.19.4 today); bumps are deliberate and coordinated. |
| D6 | **Extraction runs in a killable child process** with a timeout and an address-space limit, never a thread. |
| D7 | Broker `NOPERM` and `OOM` are **transient**; a reclaim **re-runs** the extraction (no dedupe key). |
| D8 | **v1 scope is #629's requirements only.** |

Observability: each issuer sees its own work (Watcher through its
`process_commands`; Observo, once it issues, likewise); Processor's health is
an ops concern (logs, the broker probe, lag); a cohort-wide plane is later work.

---

## Section 1 — role, boundaries, v1 scope

**Role:** the cohort's headless discrete-transform service. A command is a pure
function of (input bytes, spec, `processor_version`). Consumes
`content.process`, publishes `content.derived`.

**Owns:** the transforms (co-core's generic HTML/PDF/CSV extractors now; org
adapters later, selected by `command.processor` + spec); its bucket; draining
`content.process.dlq` (the writer of a queue is its drainer).

**Does not own:** spec authoring (issuer), interpretation (issuers), fetching
bytes (Replicator), change detection and diffing (Watcher), the durable registry
(Archiver).

**v1:** Watcher's text extraction — #629's requirements 1–7 as amended here.
**Later, each its own spec:** agenda adapters; Observo as an issuer; migrating
Observo's #488 `asset` Jobs; the cohort observability plane.

**Non-requirements (from #629):** no diff computation; no `source_spec` lists
(one spec per command — Watcher chains fallbacks as new commands); no
processor-version pin on the command; no job API — the bus is the interface.

## Section 2 — identities and infrastructure

| Thing | Value |
|---|---|
| Repo | `CannObserv/processor` — Python 3.12, uv, hatchling src layout |
| Dependencies | `co-core[extract]`, `co-core-aio[bus]`, `co-core-sync[gcs]`, all `==0.19.4` (Section 5), from the private CannObserv index |
| CI | wheelhouse pulled through WIF as `co-pypi-reader` (org var `GCP_WIF_PROVIDER`), as in the other cohort repos |
| VM | exe.dev `co-processor`, 8 GB, default `exeuntu` image, tag `processor` |
| Service | systemd unit `processor`: `MemoryMax` below VM RAM, `Restart=on-failure`, `OOMPolicy=continue` (Section 4) |
| Env | `/etc/processor/.env` (600), `CO_PROCESSOR_*` via pydantic-settings — never `os.getenv` |
| Tailnet | `tag:processor`; policy `tag:processor` → `tag:broker` on 6379 |
| Bus URL | `redis://processor:<pw>@<broker>:6379/0` — `<broker>` is the MagicDNS name `broker`, or `100.97.91.19` if the VM runs Tailscale with `--accept-dns=false` |

**Grants.**

| Principal | Resource | Grant |
|---|---|---|
| ACL user `processor` | `content.process` | `+xreadgroup +xack +xautoclaim +xgroup\|create +xlen +xrange +xinfo\|stream +info +ping` |
| ACL user `processor` | `content.derived`, `content.process.dlq` | selector `(+xadd ~content.derived ~content.process.dlq)` |
| ACL user `processor` | `content.process.dlq` | selector `(+xdel ~content.process.dlq)` |
| `co-gcs-processor-writer` | `gs://co-gcs-processor` | `objectCreator` + `objectViewer`; **no delete** — append-only, never deleted |
| `co-gcs-processor-writer` | `gs://co-gcs-blobs` | `objectViewer` (Replicator's raw blobs — the input) |
| Watcher's service account | `gs://co-gcs-processor` | `objectViewer`, bucket-level |

The ACL shape copies broker#62's `observo` user. Withheld on purpose, as there:
`+xpending`, `+xclaim`, `+xread`, `+xtrim`, and `+set`/`+exists` — Processor
keeps no Redis keys (idempotency is the content-addressed store). A command the
consumer needs but lacks surfaces as `NOPERM`; `ACL LOG` on the broker names it,
and the broker widens grants live.

Bucket: uniform bucket-level access, public access prevention, no lifecycle
rules, key scheme `blobs/<sha256>.bin` (co-core's cohort scheme,
`DEFAULT_PREFIX = "blobs"`). A later processor whose output Watcher must not
read gets its own prefix then.

## Section 3 — runtime and code structure

One async process, one consumer: co-core-aio's `AsyncBusConsumer` on group
`group_name(CONTENT_PROCESS, "processor")` = `processor.process`, created with
`ensure_group` from `$` **before Watcher issues its first command** (Section 6).
Serial: one command at a time.

**Per message:**

1. **Decode** — `from_wire(fields, topic=CONTENT_PROCESS, message_id=…)` into the
   canonical `ContentProcessCommand` (its `processor` is `str`, so an unknown
   processor decodes and can be refused). Undecodable → DLQ.
2. **Route** — `command.processor` through a small registry; only `"extract"`.
3. **Read input** — `validate_fingerprint(command.input_digest)` (bare hex).
   `input_uri` must equal the input store's `uri_for(input_digest)`, else
   `invalid_input`; never resolve `input_uri` as a path. Read with the input
   store's `open(input_digest)`; re-hash the bytes and compare to
   `input_digest`.
4. **Extract** — in the child process (below), the pure core:
   `extractor_for_essence(command.media_type)`; config =
   `extraction_config_from_spec(source_spec)` with
   `extraction_overrides_for_essence(command.media_type)` merged over it;
   `bytes = canonical_text(result.chunks)`. The command's `media_type` is
   already resolved by the issuer (cannobserv#486 D1) — do not re-resolve or
   sniff.
5. **Store** — unless `bytes == b""`: output store `store(bytes,
   bare_sha256(output_digest), CANONICAL_TEXT_MEDIA_TYPE)`, write-if-absent.
6. **Publish** `ProcessingCompleteEmit`, then **ack**.

**The child process.** A single-worker process pool with a per-command timeout,
recycled on timeout or crash. Its initializer sets `RLIMIT_AS` so a
memory-hungry document raises `MemoryError` in the child instead of drawing the
OOM killer. A thread cannot be killed; one PDF that wedges pypdf would stall the
consumer forever.

**Layout:**

- `processors/extract.py` — the pure core: `(raw, media_type, source_spec) →
  outcome` (canonical bytes, `output_digest`, `empty`, `spec_fingerprint`,
  `spec_schema_version`, `processor_version`). No I/O; the golden-digest parity
  target.
- Thin shells: bus wiring, the input/output store builders (`GcsBlobStore` from
  `co_core_sync.drivers.blobstore.gcs`), settings, `__main__`, the DLQ CLI.
- A processor registry keyed by `command.processor`; org adapters later join as
  registry entries or spec-selected variants without touching the shells.

**Health (no HTTP server in v1):** systemd for liveness; the broker probe and
`XINFO GROUPS` for lag and a missing group; structured JSON logs in the cohort
schema (`timestamp` ISO 8601 UTC, `level`, `logger`, `message` — the fields
Observo adopted in #395/#407) so the later plane can ingest them unchanged.

## Section 4 — failure handling

| Condition | Publish | Ack |
|---|---|---|
| Undecodable frame | — (DLQ) | yes |
| `processor` ≠ `"extract"` | `unsupported_processor`, terminal | yes |
| Malformed digest, or `input_uri` the input store does not recognize | `invalid_input`, terminal (issuer does not re-fetch) | yes |
| Input bytes gone | `input_unreadable`, terminal for the command (issuer re-fetches, capped) | yes |
| Bytes hash ≠ `input_digest` | `input_digest_mismatch`, terminal | yes |
| Extractor raises, including `MemoryError` under `RLIMIT_AS` | `extraction_error`, terminal | yes |
| Child timeout or crash | — | no; the 3rd attempt publishes `extraction_error`, terminal, and acks |
| GCS 5xx / 429 / timeout / auth; broker `NOPERM`; broker `OOM command not allowed` | — | no; the entry is reclaimed |

- **`unsupported_media_type` is never emitted in v1** — `extractor_for_essence`
  is total (HTML for anything unknown).
- **The retry cap is counted in memory**, per stream entry id: `+xpending` is
  withheld and `XAUTOCLAIM` returns no delivery count. A parent restart resets
  it; with the child's `RLIMIT_AS`, parent restarts are rare and a poison
  command gets at most three more attempts. Too loose in practice → ask the
  broker for `+xpending`.
- **Infrastructure failures are uncapped.** Publish nothing; the reclaim retries.
  Watcher's reaper re-issues stale commands under fresh ids; a fact Processor
  later publishes for a superseded command is discarded by Watcher. v1 never
  publishes the `transient` token.
- **Order is store → publish → ack.** A publish failure leaves the entry
  unacked; the reclaim re-runs, the store write is a no-op, the publish lands.
  An ack failure after a successful publish yields a duplicate fact, which
  Watcher's idempotent upsert on `command_id` absorbs.
- **Reclaim idle time exceeds the extraction timeout**, so a slow command is not
  reclaimed from under itself.
- **`OOMPolicy=continue`** on the unit: systemd's default stops the whole unit
  when any process in it is OOM-killed.
- **DLQ CLI:** `processor dlq list | show | drop` over `XRANGE`/`XDEL`
  (`dlq_name(CONTENT_PROCESS)` = `content.process.dlq`). No replay — an
  undecodable frame has nothing to replay.
- **Every outcome logs** `command_id`, `info_source_id`, reason and timings.

These answer the two points broker#62 left for the consumer to state: `NOPERM`
is transient, and under `maxmemory` a refused `XADD` is transient, with the
reclaim re-running (not re-publishing) the extraction.

## Section 5 — versioning and the co-core pin

- `processor_version = processor_version(LOCAL_GENERATION)` (co-core,
  `co_core.pure.extract.canonical`), spelled `"<co-core version>+<generation>"`.
  **`LOCAL_GENERATION = 1`**, matching Watcher's `LOCAL_EXTRACTION_GENERATION =
  1` — the dispatch Processor runs is Watcher's, lifted into co-core. Bump it by
  hand only when Processor's own logic (config merging, dispatch) changes output
  in a way co-core's version cannot see.
- **co-core `==0.19.4`**, Watcher's version. Processor needs `GcsBlobStore`
  (0.19.3+) and the `content.process` types (0.19.4), nothing newer, so both
  sides report `"0.19.4+1"` from the first shadow command.
- **Bumps are deliberate and coordinated.** `processor_version` moves on every
  co-core release whether or not output moves, and Watcher's Option A reacts to
  it. No automatic lock refresh; dependency bots skip co-core. A bump is planned
  with Watcher: the golden-digest corpus passes unchanged on the new version
  before deploy, and the bump note says whether output moved. During the shadow
  window both repos move together or neither does.

## Section 6 — cross-repo changes and cutover

Every item is external: drafted, then approved and posted one at a time.

| Where | Change |
|---|---|
| observo | #629 recreated as processor#1 (body plus its co-core and broker comments) and closed with a pointer — a private → public transfer is refused. This spec is committed there as the decision record. Remove `CO_OBSERVO_BROKER_TOKEN` from `/etc/observo/.env` once the broker strips `observo`. No code change. |
| processor | Founding spec (this file, at the same path), `AGENTS.md`, #1 (from observo#629). |
| broker | New issue: ACL user `processor` (Section 2); strip `observo`'s grants; probe watches `processor.process`; participants table and `docs/STREAMS.md`; `docs/NETWORK-PATHS.md` client cells from `co-processor`. |
| watcher | On #325 or a design-doc PR: Section 3's processor is Processor; output root `gs://co-gcs-processor`; the reader grant; the `WATCHER_EXTRACT_MODE` value `observo` (rename is Watcher's call); the Section 5 question below; the lockstep bump policy. |
| cannobserv | Doc-only issue: `CHANGE_BUS.md` names Observo / `observo.process` as the processor. |
| replicator | New issue amending #69's roles charter (closed): "Observo transforms" becomes Observo = stream transforms and interpretation, Processor = discrete transforms. |
| archiver | Informational comment on #179: the processor is Processor. |
| GCP / tailnet | Bucket, service account and grants (Section 2); the tailnet rule. Operator acts. |

**Cutover order:**

1. This spec approved; the external drafts approved.
2. Infra: VM, bucket, service account, grants, broker ACL, tailnet rule.
3. Processor v1 built on `co-processor`, deployed, group created, smoke-tested on
   a scratch Redis there (`processor` cannot and should not `XADD
   content.process` on the real broker).
4. Watcher #325 ships and runs **shadow**: Processor stores texts; Watcher's
   comparator counts mismatches.
5. Watcher #326 switches on zero mismatches across a window with at least one
   real change event — Watcher's gate, unchanged.

**Hard ordering:** `processor.process` exists before Watcher's first command. A
group created from `$` afterwards skips earlier entries — Watcher's reaper
recovers them, noisily, and the broker probe reports `group-missing` meanwhile.

## Section 7 — testing

TDD, red first.

- **Pure core — golden-digest parity.** A corpus of HTML / PDF / CSV fixtures,
  each with a spec and resolved `media_type`, asserting `output_digest` and
  `empty`. Seed it from Watcher's extraction fixtures so parity with Watcher's
  local path is proven before shadow, not discovered in it. Include: a one-page
  scanned PDF (one empty chunk → `empty`, not a failure); an unknown essence (→
  HTML); a spec whose `spec_fingerprint` raises (→ `None`).
- **Pin test.** The lock's co-core equals the expected exact version and
  `processor_version(LOCAL_GENERATION) == "0.19.4+1"` — a bump is a deliberate,
  test-failing act.
- **Shells — one test per Section 4 row.** Input/output stores: a local co-core
  store on a temp dir behind the same interface, plus fakes raising GCS 5xx /
  429 / `NotFound`. Bus: fakeredis (or co-core-aio's test double, if it ships
  one) — decode → DLQ, store → publish → ack order, publish failure leaves the
  entry unacked, ack failure yields a duplicate fact. Child process: a fixture
  sleeping past the timeout (pool recycled, 3-strike cap), one exceeding
  `RLIMIT_AS` (→ `extraction_error`), one exiting hard.
- **Integration.** A scratch Redis on `co-processor` with the real stream names;
  a test issuer `XADD`s real commands; assert facts, stored bytes, and `XINFO
  GROUPS` lag, against a scratch GCS prefix or the local store.
- **Production acceptance** is Watcher's shadow mismatch count.

## Section 8 — standup and handoff

**From the Observo session** (each outward step approved individually):

1. Commit this spec to observo `docs/specs/`.
2. Draft the Section 6 externals, a gcloud block for the bucket / service account
   / grants, and the tailnet rule.
3. Provision `new --name co-processor --tag processor --memory 8GB --disk 20GB`
   (default `exeuntu` image, which ships Claude Code) through exe.dev with
   `EXE_API_TOKEN`. **Never the `observo-worker` tag**: Observo's rowless-VM
   orphan sweep reaps by it (`exe.ls(tag=observo-worker)`).
4. First-boot setup: uv + Python 3.12, `redis-server` (scratch bus), the
   Tailscale package, `/etc/processor/` (700). No secrets in the setup script,
   and no backslashes — `/exec` unescapes them.
5. Join the tailnet with the one-time key, piped over SSH stdin (never argv or
   the setup script), advertising `tag:processor`.
6. A write deploy key for `CannObserv/processor`, generated on the VM.
7. The handoff brief as a comment on processor#1: spec link, what is
   done, what is outstanding, the first task.

**Human steps:** Claude Code login on the VM; the broker credential (`pw
PROCESSOR` on the broker node) into `/etc/processor/.env`; the
`co-gcs-processor-writer` key and a `co-pypi-reader` key (the private index,
as Observo's `CO_PYPI_KEY_FILE`) onto the VM; the GCP / tailnet console acts.

**Then the switch:** the `co-processor` agent reads processor#1 and this spec
(already committed to the repo at the same path), writes `AGENTS.md`, and runs
writing-plans for v1 there. Implementation happens on `co-processor`.

---

## Contract quick reference (co-core 0.19.4)

| Need | Name | Module |
|---|---|---|
| Stream / group / DLQ | `CONTENT_PROCESS`, `CONTENT_DERIVED`, `group_name`, `dlq_name` | `co_core.pure.adapters.bus.streams` |
| Decode | `from_wire` | `co_core.pure.adapters.bus.envelope` |
| Command / facts | `ContentProcessCommand`, `ProcessingCompleteEmit`, `ProcessingFailedEmit`, `ProcessingFailureReason` | `co_core.pure.models.changes` |
| Dispatch | `extractor_for_essence` | `co_core.pure.extract.dispatch` (needs the `extract` extra) |
| Config | `extraction_config_from_spec` | `co_core.pure.extract.extraction_defaults` |
| Essence overrides | `extraction_overrides_for_essence` | `co_core.pure.extract.media_type` |
| Canonical bytes | `canonical_text`, `canonical_text_fingerprint`, `CANONICAL_TEXT_MEDIA_TYPE`, `processor_version` | `co_core.pure.extract.canonical` |
| Spec identity | `spec_fingerprint`, `spec_schema_version` | `co_core.pure.extract.spec_fingerprint` |
| Digests | `bare_sha256`; `validate_fingerprint` | `co_core.pure.util.hashing`; `co_core.pure.util.blobstore` |
| Stores | `BlobStore` (`open`, `store`, `exists`, `uri_for`); `GcsBlobStore` | `co_core.pure.util.blobstore`; `co_core_sync.drivers.blobstore.gcs` |
| Bus client | `AsyncBusConsumer`, producer | `co-core-aio[bus]` |

Field rules the Emit classes enforce: `output_digest` is `sha256:<hex>`
(`canonical_text_fingerprint`), `input_digest` is bare hex; `empty ⇔
canonical_text(chunks) == b""` — bytes, not chunk count (`PdfExtractor` emits a
chunk per blank page); when `empty`, `output_digest` and `output_uri` are `None`
and `output_size_bytes` is `0`; `output_media_type` must be
`CANONICAL_TEXT_MEDIA_TYPE`; `spec_fingerprint` is `None` if the derivation
raises; a failure's `terminal` must agree with its reason. Idempotency keys:
`command_id` (command), `command_id:occurred_at` (facts). `info_source_id` is
echoed, reporting only.

## Open questions

1. **Watcher (#325):** when a derived fact's fingerprint equals the latest
   revision's but `processor_version` differs, is the baseline's
   `processor_version` refreshed? If not, every co-core bump silently absorbs
   the next real change on each watched item; if so, a bump costs nothing unless
   it coincides with a change (the residual Watcher already accepts).
2. **co-core-aio:** does `AsyncBusConsumer` expose a delivery count or a
   max-deliveries hook? If so, it replaces the in-memory retry counter.
3. **Merge order** of `extraction_overrides_for_essence` over
   `extraction_config_from_spec`: stated per the co-core author's #629 comment;
   confirm against Watcher's local pipeline in the parity corpus.
