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
co-core 0.19.5 in Observo's venv, then against tags `v0.19.4` and `v0.19.7`
on `co-processor`.

**Amended 2026-09-29 (`co-processor` review, processor#1):**
- The pin moves from 0.19.4 to **0.19.7**, Watcher's locked version (D5,
  Sections 2, 5 and 7).
- The child process is a fresh interpreter per command (Section 3).
- The parity corpus adds real samples (Section 7).
- Open Questions 2 and 3 are answered.

**Amended 2026-10-02 (watcher#325's answers, processor#16):**
- Open Question 1 is answered: yes, Watcher refreshes the baseline's `processor_version`.
- Watcher accepts D5's lockstep and pins `==0.19.7` when #325 lands.
- Watcher's reader is `co-gcs-blob-reader` (Section 2).
- Watcher no longer re-issues while Processor is down (Section 4).
- The real parity samples are in the corpus: 9 HTML inputs, 9/9 (Section 7).
- A command dead-lettered at the cap still gets a terminal fact first (Section 4,
  processor#17).

**Amended 2026-10-03 (Watcher's consumer rules, processor#20):**
- Watcher re-issues a stuck command only once Processor has answered a command
  published after it, not on any fact (Section 4).
- The first terminal fact per `command_id` decides; later ones are logged and
  dropped (Section 4).
- A command that gets no fact is bounded by Watcher's 24 h hard limit (Section 4).
- How Watcher's re-issue threshold and Processor's PEL retry interact (Section 4).
- A command dead-lettered at the cap can get a failure fact, then a success;
  the failure stands (Section 4, processor#17).

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
| D5 | **co-core pinned exactly, matched to Watcher** (0.19.7 today); bumps are deliberate and coordinated. Watcher agreed on 2026-10-02 (watcher#325): it pins `==0.19.7` when #325 lands, and neither side deploys a bump until the parity corpus passes on it unchanged. |
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
| Dependencies | `co-core[extract]`, `co-core-aio[bus]`, `co-core-sync[gcs]`, all `==0.19.7` (Section 5), from the private CannObserv index |
| CI | wheelhouse pulled through WIF as `co-pypi-reader` (org var `GCP_WIF_PROVIDER`), as in the other cohort repos |
| VM | exe.dev `co-processor`, 8 GB, default `exeuntu` image, tag `processor` |
| Service | systemd unit `processor`: `MemoryMax` below VM RAM, `Restart=on-failure`, `OOMPolicy=continue` (Section 4) |
| Env | `/etc/processor/.env` (600), `CO_PROCESSOR_*` via pydantic-settings — never `os.getenv` |
| Tailnet | `tag:processor`; policy `tag:processor` → `tag:broker` on 6379 |
| Bus URL | `redis://processor:<pw>@broker:6379/0` — the MagicDNS name, never the address, which a broker rebuild changes; the VM runs Tailscale with `--accept-dns=true`, as the cohort does (amended 2026-10-01, #8) |

**Grants.**

| Principal | Resource | Grant |
|---|---|---|
| ACL user `processor` | `content.process` | `+xreadgroup +xack +xautoclaim +xgroup\|create +xlen +xrange +xinfo\|stream +info +ping` |
| ACL user `processor` | `content.derived`, `content.process.dlq` | selector `(+xadd ~content.derived ~content.process.dlq)` |
| ACL user `processor` | `content.process.dlq` | selector `(+xdel ~content.process.dlq)` |
| `co-gcs-processor-writer` | `gs://co-gcs-processor` | `objectCreator` + `objectViewer`; **no delete** — append-only, never deleted |
| `co-gcs-processor-writer` | `gs://co-gcs-blobs` | `objectViewer` (Replicator's raw blobs — the input) |
| Watcher's service account, `co-gcs-blob-reader` (its `GCS_BLOB_CREDENTIALS`; confirmed on watcher#325, granted 2026-10-02) | `gs://co-gcs-processor` | `objectViewer`, bucket-level |

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

**The child process.** Each command gets a fresh interpreter (`python -I -m
processor._child`, over asyncio's subprocess API), killed on timeout. On a crash, the child's exit code tells the parent. The
child sets `RLIMIT_AS` on itself before it imports the extractors, so a
memory-hungry document raises `MemoryError` in the child instead of drawing
the OOM killer. A thread cannot be killed; one PDF that wedges pypdf would
stall the consumer forever.

Not a `ProcessPoolExecutor`: it cannot kill a running task on timeout
without private internals. On 3.12 it also forks from the asyncio parent
with its GCS client, and the child inherits that address space, which makes
`RLIMIT_AS` hard to size.

Not `multiprocessing` spawn either: spawn re-imports the parent's `__main__`,
with its redis and GCS clients, in every child. The child parses untrusted
documents, so it gets a scrubbed environment (no broker credential, no GCS
key). The parent decodes its result with an unpickler that allows no global
but `ExtractOutcome`. At ≤ ~100 commands/day the start-up cost is noise
(amended 2026-09-29/30).

**Known limitation (accepted for the MVP, 2026-09-30).** The scrubbed
environment and the unpickler are not a sandbox. The child runs as the
service user, so a child that a parser bug gives code execution can read
what that user can: the env file, the GCS key, and the parent's
`/proc/<pid>/environ`. It can also open network connections.
Containment is processor#2: Landlock in the child, a non-dumpable parent,
and a dedicated service user.

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
| Undecodable frame, or a well-formed frame that is not a `content_process` command | — (DLQ) | yes |
| `processor` ≠ `"extract"` | `unsupported_processor`, terminal | yes |
| Malformed digest, or `input_uri` the input store does not recognize | `invalid_input`, terminal (issuer does not re-fetch) | yes |
| Input bytes gone, or the download fails its checksum (`DataCorruption`, amended 2026-09-30) | `input_unreadable`, terminal for the command (issuer re-fetches, capped) | yes |
| Bytes hash ≠ `input_digest` | `input_digest_mismatch`, terminal | yes |
| Extractor raises, including `MemoryError` under `RLIMIT_AS` | `extraction_error`, terminal | yes |
| Child timeout or crash | — | no; the 3rd attempt publishes `extraction_error`, terminal, and acks |
| A non-transient exception escaping the handler (a bug, or an ack or dead-letter refused), amended 2026-09-30 and 2026-10-02 | at the cap, `extraction_error`, terminal, unless this entry's fact already went out (a fact published before a refused ack stands) | no; counted like a strike, and the 3rd attempt publishes, then dead-letters the entry with the exception as its reason |
| GCS 5xx / 429 / timeout / auth; broker `NOPERM`; broker `OOM command not allowed`; broker `MISCONF` / `BUSY` / `MASTERDOWN` / `TRYAGAIN` / `CLUSTERDOWN` / `NOREPLICAS` (amended 2026-09-30) | — | no; the entry is reclaimed |
| A non-transient publish failure (e.g. `WRONGTYPE`, or a bug in serialization), amended 2026-09-30 and 2026-10-02 | at the cap, `extraction_error`, terminal, best effort (the same refusal usually refuses it too) | no; counted like a strike, and the 3rd attempt dead-letters the entry |

- **`unsupported_media_type` is never emitted in v1** — `extractor_for_essence`
  is total (HTML for anything unknown).
- **The retry cap is counted in memory**, per stream entry id: `+xpending` is
  withheld and `XAUTOCLAIM` returns no delivery count. A parent restart resets
  it; with the child's `RLIMIT_AS`, parent restarts are rare and a poison
  command gets at most three more attempts. Too loose in practice → ask the
  broker for `+xpending`.
- **Infrastructure failures are uncapped.** Publish nothing; the reclaim retries.
  Watcher's reaper re-issues a stale command under a fresh id only once
  Processor has answered a command published after it (watcher#325, amended
  2026-10-02 and 2026-10-03). A fact for just any command is not enough: a
  Processor draining a backlog after an outage would trigger re-issues of
  everything queued behind its first answer. While Processor is down, commands
  queue in `processor.process`; Watcher reports one signal, `processor has not
  reached held process commands — down, or draining its backlog`, and items as
  *processing delayed*. On restart, Processor works through the backlog,
  superseded commands included. Watcher discards a fact for a superseded or
  expired command, so they need no special handling. An outage longer than
  Replicator's blob TTL yields `input_unreadable`, then Watcher's capped
  re-fetch. v1 never publishes the `transient` token.
  - **The two timers** (measured with Watcher, watcher#325, 2026-10-03).
    Watcher re-issues after 1800 s (`WATCHER_PROCESS_COMMAND_TIMEOUT_SECONDS`,
    its default). Processor retries a failed attempt from the PEL 10–11 min
    after it was delivered (`reclaim_min_idle_ms` 600 s, plus a walk every
    `reclaim_interval_s` 60 s). So for a command Processor reaches promptly,
    one failed attempt never triggers a re-issue. Only a command failing
    transiently for over 30 min, or reached late (say behind a backlog), can
    be answered twice; the original's fact then lands on an expired row and is
    dropped as `late`: duplicate work, not a wrong verdict.
- **Order is store → publish → ack.** A transient publish failure leaves the entry
  unacked; the reclaim re-runs, the store write is a no-op, the publish lands.
  An ack failure after a successful publish yields a duplicate fact. Watcher
  dedupes on its `process_commands` row: the first terminal fact per
  `command_id` decides, and later ones are logged and dropped (watcher#325,
  amended 2026-10-03). So a success then a failure leaves the success standing,
  and a duplicate success sends Archiver no second renewal.
- **Reclaim idle time exceeds the extraction timeout**, so a slow command is not
  reclaimed from under itself.
- **`OOMPolicy=continue`** on the unit: systemd's default stops the whole unit
  when any process in it is OOM-killed.
- **DLQ CLI:** `processor dlq list | show | drop` over `XRANGE`/`XDEL`
  (`dlq_name(CONTENT_PROCESS)` = `content.process.dlq`). No replay — an
  undecodable frame has nothing to replay, and a command dead-lettered after
  escaping exceptions is a bug to fix.
- **A dead-lettered command still gets a fact** (amended 2026-10-02, #17). Before
  dead-lettering a decodable command at the cap, Processor publishes
  `processing_failed` with `terminal=true`, `reason=extraction_error`, and
  `detail` = `dead-lettered: <the DLQ reason>`. Without it Watcher would wait on
  the command. Its reaper re-issues only once Processor answers a later command
  (watcher#325, amended 2026-10-03), and its health reads a quiet Processor as
  down. Watcher closes the command instead: in shadow only an audit entry,
  after cutover the item's failure path.
  - **No second failure fact.** It is skipped when this entry's fact already
    went out: a refused ack, or a dead-letter retried after the fact landed.
    That memory is per process: after a restart, a re-run that reaches the cap
    again publishes another, which Watcher drops (first fact wins).
  - **A transient refusal** leaves the entry pending (uncapped); a non-transient
    one is logged, and the entry is dead-lettered anyway. That command gets no
    fact, but it is not left open. Watcher re-issues it once it is past the
    re-issue threshold and Processor has answered a later command. In a quiet
    period, Watcher's hard limit (24 h, `WATCHER_PROCESS_COMMAND_HARD_LIMIT_SECONDS`)
    expires it instead; after cutover that fails the fetch with
    `processing_timeout` and sets the item to ERROR, and the next scheduled
    fetch starts a fresh lineage.
  - **Failure, then success.** If the dead-letter does not land after the fact
    did (a refusal of either kind, or a crash or restart in between), the entry
    stays pending and the reclaim runs the command again: still at the cap, or
    from attempt 1 after a restart, which forgets the strikes and that the
    fact went out. A run that now succeeds publishes a success fact and acks,
    and the entry never reaches the DLQ. Under first-fact-wins the failure
    stands.
  - **Frames that are not commands** (undecodable, or foreign events) carry no
    `command_id`, so they get no fact.
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
- **co-core `==0.19.7`**, the version Watcher's `uv.lock` resolves. Watcher's
  `pyproject.toml` allows `>=0.19.6,<0.20`. Both sides then report `"0.19.7+1"`
  from the first shadow command. Processor needs `GcsBlobStore` (0.19.3+) and
  the `content.process` types (0.19.4).
  - Between 0.19.4 and 0.19.7, `co_core.pure.extract` and `co_core_aio.bus` are
    byte-identical, and the `content.process` types did not change. 0.19.6 only
    added the `content.persist` pair.
  - 0.19.6 does change one thing Processor relies on: a store checks the
    fingerprint against the bytes it writes, and raises `FingerprintMismatch`.
  - This section named 0.19.4 until the 2026-09-29 amendment. That pin would
    have reported `"0.19.4+1"` against Watcher's `"0.19.7+1"`, which is Open
    Question 1's hazard from the first command.
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
   content.process` on the real broker). **Done 2026-10-02:**
   - group created 22:09:41Z on 2026-10-01;
   - deployed 23:27:27Z;
   - `scripts/smoke_scratch_bus.py` passed 23:45:29Z: the scratch bus,
     the production output store, and the digest equal to Watcher's recorded
     fingerprint.
4. Watcher #325 ships and runs **shadow**: Processor stores texts; Watcher's
   comparator counts mismatches.
5. Watcher #326 switches on zero mismatches across a window with at least one
   real change event — Watcher's gate, unchanged.

**Hard ordering:** `processor.process` exists before Watcher's first command. A
group created from `$` afterwards skips earlier entries — Watcher's reaper
recovers them, noisily, and the broker probe reports `group-missing` meanwhile. **Satisfied 2026-10-01 22:09:41Z:** `processor ensure-group`
from `$` on an empty stream, ahead of Watcher's first command.

## Section 7 — testing

TDD, red first.

- **Pure core — golden-digest parity.** A corpus of HTML / PDF / CSV fixtures,
  each with a spec and resolved `media_type`, asserting `output_digest` and
  `empty`. Include: a one-page scanned PDF (one empty chunk → `empty`, not a
  failure); an unknown essence (→ HTML); a spec whose `spec_fingerprint` raises
  (→ `None`).
  - Watcher's only file fixture is `tests/fixtures/sample.html`, so the corpus
    starts synthetic.
  - Before shadow it adds real samples: per watched item, the raw digest,
    `source_spec`, resolved `media_type` and Watcher's recorded fingerprint.
    **Done 2026-10-02 (#16):** Watcher exported 9 real inputs (watcher#325),
    all `text/html`, since production has 4 watched items, all HTML.
    `tests/fixtures/parity/real/` keeps the export verbatim and the raw blobs,
    copied before Replicator's 7-day TTL. Processor reproduces every recorded
    fingerprint and `spec_fingerprint`, 9/9, including two revisions from before
    fetch commands existed and one recorded under `0.19.4+1`. PDF, CSV and XLSX
    still rest on the synthetic corpus.
  - Parity with Watcher's local path should be proven before shadow, not
    discovered in it.
- **Pin test.** The lock's co-core equals the expected exact version and
  `processor_version(LOCAL_GENERATION) == "0.19.7+1"` — a bump is a deliberate,
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

## Contract quick reference (co-core 0.19.4–0.19.7, unchanged across them)

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
`command_id` (command), `command_id:occurred_at` (facts; duplicates differ in
`occurred_at`, so this key does not merge them, and Watcher settles on the
first terminal fact per `command_id`, Section 4). `info_source_id` is
echoed, reporting only.

## Open questions

1. ~~**Watcher (#325):** when a derived fact's fingerprint equals the latest
   revision's but `processor_version` differs, is the baseline's
   `processor_version` refreshed?~~ **Answered 2026-10-02: yes** (watcher#325).
   Watcher compares the fact's `processor_version` with
   `WatchedItem.processor_version`, read before the fact updates it. An
   equal-digest fact records the new version and does nothing else, so a
   co-core bump costs nothing unless a real change coincides with it.
   `ChangeRevision.processor_version` is never rewritten. `spec_fingerprint`
   gets the same treatment.
2. ~~**co-core-aio:** does `AsyncBusConsumer` expose a delivery count or a
   max-deliveries hook?~~ **Answered, no (0.19.4 through 0.19.7).** It offers
   primitives only: `ensure_group`, `read`, `ack`, `claim_stale` /
   `claim_stale_page`, and `dead_letter`. It has no delivery count, no hook
   and no run loop. The in-memory counter stays, and Processor owns its loop.
   It reclaims with `claim_stale_page`, which returns malformed frames for
   `dead_letter`; `claim_stale` raises instead.
3. ~~**Merge order** of `extraction_overrides_for_essence` over
   `extraction_config_from_spec`~~ **Answered: the overrides win.**
   - Watcher's `_extract_with_spec` builds `{**config_from_spec,
     **extra_config}` (`src/workers/pipeline.py`, Watcher `main` on
     2026-09-29).
   - Watcher's `_DEFAULT_EXTRACTOR_MAP` is entry-for-entry co-core's
     `EXTRACTOR_BY_ESSENCE`, with the same HTML fallback.
   - The parity corpus still pins both.
