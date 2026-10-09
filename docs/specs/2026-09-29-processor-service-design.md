# Processor — the cohort's discrete-transform service — design

**Status:** design approved section by section 2026-09-29 (brainstorming in an
Observo session); this document is the founding spec for `CannObserv/processor`.
**Issue:** CannObserv/processor#1, recreated from observo#629 (observo is
private; GitHub refuses a private → public transfer). **Supersedes:** #629's
premise that Observo is the processor.
**Contract of record:** co-core `docs/CHANGE_BUS.md` § *Watcher-issued processing
— the `content.process` pair* (cannobserv v0.19.4; by v0.19.7, the pinned version,
`docs/change_bus/process.md`), and Watcher's
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
| Service | systemd unit `processor`: `MemoryMax` below VM RAM, `Restart=on-failure`, `OOMPolicy=continue` (Section 4). Runs as its own user `processor` (no sudo, not in `docker`, no home), from a read-only release at `/srv/processor/live` that `scripts/deploy.sh` builds from a commit on `origin/main` (amended 2026-10-06, #2: the cohort's release standard, broker#22 / status#9) |
| Env | `/etc/processor/.env` (root 600, read by systemd before it switches users), `CO_PROCESSOR_*` via pydantic-settings — never `os.getenv`. The GCS key reaches the service by `LoadCredential=` (amended 2026-10-06, #2) |
| Tailnet | `tag:processor`; policy `tag:processor` → `tag:broker` on 6379, and → `tag:status` on 9000 (amended 2026-10-07, #35: the drift check; since 2026-10-08 also the liveness check-in, #39) |
| Drift check | `processor-drift.timer` → `processor-drift.service`, hourly, as `processor`: `processor drift` asks GitHub (unauthenticated) whether live's `REVISION` lags `origin/main` in code that runs, and checks in to Status's monitor `co-processor-drift` at `http://status:9000`; `alert` once code has waited past 8 h, nothing when GitHub can't say. Apart from `processor run`: an outage of either costs only the check. Its key is a `LoadCredential=` from `/etc/processor/status-checkin.key`; it reads no `.env` (amended 2026-10-07, #35; CannObserv/status#24) |
| Liveness | `processor run` checks in `ok` to Status's monitor `co-processor-live` every 5 min, only while its consume loop is making progress (a finished read, message or backoff within 605 s). Never `alert`: silence past the monitor's grace (900 s) is the page. Each check-in runs in a daemon thread, bounded at 10 s, one attempt per tick, so a Status outage never touches consumption. The same key as the drift check, by `LoadCredential=` (amended 2026-10-08, #39; CannObserv/status#24) |
| Bus URL | `redis://processor:<pw>@broker:6379/0` — the MagicDNS name, never the address, which a broker rebuild changes; the VM runs Tailscale with `--accept-dns=true`, as the cohort does (amended 2026-10-01, #8) |

**Grants.**

| Principal | Resource | Grant |
|---|---|---|
| ACL user `processor` | `content.process` | `+xreadgroup +xack +xautoclaim +xgroup\|create +xlen +xrange +xinfo\|stream +info +ping` |
| ACL user `processor` | `content.derived`, `content.process.dlq` | selector `(+xadd ~content.derived ~content.process.dlq)` |
| ACL user `processor` | `content.process.dlq` | selector `(+xdel ~content.process.dlq)` |
| `co-gcs-processor-writer` | `gs://co-gcs-processor` | `objectCreator` + `objectViewer`; **no delete** — append-only, never deleted |
| `co-gcs-processor-writer` | `gs://co-gcs-blobs` | `objectViewer` (Replicator's raw blobs — the input) |
| Status tenant `co-processor` (production key, `/etc/processor/status-checkin.key`) | Status monitors `co-processor-drift` and `co-processor-live` | check-in only (`POST /api/v1/monitors/{id}/checkin`); Status's operator owns the monitors and their channels (CannObserv/status#24, #35, #39) |
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

**Containment (amended 2026-10-06, #2).** The child is assumed to be
compromised by the document it parses. Until 2026-10-06 it ran as the
operator's user, with sudo and the `docker` group, and could read the env
file, the GCS key, the parent's `/proc/<pid>/environ`, and open any
connection. Now, in layers:

1. **The child contains itself** (`processor._contain`, stdlib `ctypes`, no
   binding) after `RLIMIT_AS` and before it reads its request:
   - *Landlock* (ABI ≥ 6): read only on a derived allowlist, and no program
     run from it (no `execve`, not even the dynamic loader; CR 2) (each
     `sys.path` entry, the stdlib, the directories of the shared libraries
     already loaded (the loader's, so libgcc_s beside libc is found; Python's
     `LIBDIR` is not the system's on CI), `/etc/ld.so.cache`, and the
     mime-types files co-core reads at import).
     It writes nothing, and reads nothing under `/proc`, `/etc/processor`,
     `/run/credentials` or a home directory. No TCP bind or connect. Abstract
     unix sockets and signals are scoped to its own domain. An allowlist
     entry that would widen the set (`/`, `/etc`, `/home`, a home directory,
     a directory holding `.env`) is refused.
   - *seccomp*: `socket`, `socketpair` and `io_uring_*` fail with EPERM;
     another arch's syscalls kill it. Landlock on 6.12 does not cover a
     connect to an existing pathname unix socket (tailscaled's is 0666;
     docker's meant root), so the child opens no socket at all.

   `CO_PROCESSOR_CHILD_CONTAINMENT` is `required` by default: `processor
   run` refuses to start where the kernel cannot contain the child, or
   where one real extraction through the contained child fails at boot (the
   canary; CR 1). A child that fails to contain itself exits 70 before
   reading its request
   (a crash, never a verdict). `off` is for dev and CI kernels only; the
   test suite's header says which mode it ran.
2. **The parent is undumpable** (`PR_SET_DUMPABLE` 0, first thing in
   `processor run`), so no same-uid process reads its environment, which
   holds the broker credential.
3. **A dedicated user, `processor`**, owns nothing it runs: the release is
   `exedev`'s and read-only, `/etc/processor` is root's, and the keys (GCS's,
   and since #39 Status's) come through `LoadCredential=`. `ProtectHome=yes` and `ProtectSystem=strict`.

What stays reachable from a compromised child: the kernel's other syscalls;
its own code, the stdlib and the shared libraries; CPU to the timeout and
memory to `RLIMIT_AS`; and the output of its own command, which only
Watcher's comparison checks.

**Layout:**

- `processors/extract.py` — the pure core: `(raw, media_type, source_spec) →
  outcome` (canonical bytes, `output_digest`, `empty`, `spec_fingerprint`,
  `spec_schema_version`, `processor_version`). No I/O; the golden-digest parity
  target.
- Thin shells: bus wiring, the input/output store builders (`GcsBlobStore` from
  `co_core_sync.drivers.blobstore.gcs`), settings, `__main__`, the DLQ CLI.
- A processor registry keyed by `command.processor`; org adapters later join as
  registry entries or spec-selected variants without touching the shells.

**Health (no HTTP server in v1):** systemd restarts a crash. **The liveness
check-in** (amended 2026-10-08, #39; `processor.liveness`) is a task beside the
consume loop on the same event loop. The loop stamps `last_progress` after every
step, backoff included, and after every message, reclaimed ones included. A
reclaim walk over a backlog takes one command's time per entry, so it is not a
wedge. The heartbeat checks in `ok` to `co-processor-live` every
`live_interval_s` while that stamp is no older than `reclaim_min_idle_ms +
read_block_ms`, the reclaim's own bound for a dead command plus one read.
Otherwise it stays silent:
- an idle queue still turns the loop, so it never reads as an outage;
- a broker outage backs off and counts as alive;
- a wedged loop goes silent while the process stays up.

The check-in is blocking `requests`, so it runs in a daemon thread, not the
default executor. The handler's GCS calls use that executor, and `asyncio.run`
waits for it at exit. Each check-in is awaited for at most
`live_checkin_timeout_s`. A tick whose predecessor is still in flight is
skipped, and nothing raises into the loop. The broker probe and
`XINFO GROUPS` cover lag and a missing group; structured JSON logs in the cohort
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
| A non-transient exception escaping the handler (a bug, or an ack or dead-letter refused), amended 2026-09-30, 2026-10-02 and 2026-10-05 | at the cap, `extraction_error`, terminal, unless this entry's fact already went out (a fact published before a refused ack stands) | no; counted like a strike, and the 3rd attempt publishes, then dead-letters the entry with the exception as its reason. A dead-letter that does not land is retried alone, without running the command again (#28) |
| GCS 5xx / 429 / timeout / auth; broker `NOPERM`; broker `OOM command not allowed`; broker `MISCONF` / `BUSY` / `MASTERDOWN` / `TRYAGAIN` / `CLUSTERDOWN` / `NOREPLICAS` (amended 2026-09-30) | — | no; the entry is reclaimed |
| A non-transient publish failure (e.g. `WRONGTYPE`, or a bug in serialization), amended 2026-09-30, 2026-10-02 and 2026-10-05 | at the cap, `extraction_error`, terminal, best effort (the same refusal usually refuses it too) | no; counted like a strike, and the 3rd attempt dead-letters the entry (retried alone if it does not land, #28) |

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
  - **No re-run after giving up** (amended 2026-10-05, #28). If the give-up
    does not finish (the failure fact refused transiently, or the dead-letter
    refused either way), the entry stays pending and the next reclaim retries
    the give-up only: the failure fact if it has not gone out, then the
    dead-letter, with the original reason and traceback. The command does not
    run again, so no success fact can follow the failure, and the untrusted
    parser (#2) never sees that input again. A dead-letter refused every time
    (say `WRONGTYPE` on `content.process.dlq`) leaves the entry pending: each
    reclaim, about every 11 min, logs one `dead-lettering` record at ERROR
    (`handle_skipped` true) and runs nothing. That memory is per process, like
    the strikes: after a restart the command runs again from attempt 1, and a
    run that now succeeds publishes a success fact and acks. Under
    first-fact-wins the failure stands.
  - **Frames that are not commands** (undecodable, or foreign events) carry no
    `command_id`, so they get no fact.
- **Every outcome logs** `command_id`, `info_source_id`, reason and timings. It also logs
  `input_digest` once it is a valid fingerprint; a malformed one is left out, and its
  `invalid_input` detail quotes it.
  Once a complete fact exists, it adds `output_digest` (absent when `empty`),
  `output_size_bytes`, `empty` and `processor_version`, taken from that fact (#26).
  Whether the store wrote a new object is not logged: co-core's write-if-absent
  swallows the 412.

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
Status re-checked against each repo on 2026-10-05 (#23).

| Where | Change | Status |
|---|---|---|
| observo | #629 recreated as processor#1 (body plus its co-core and broker comments) and closed with a pointer — a private → public transfer is refused. This spec is committed there as the decision record. Remove `CO_OBSERVO_BROKER_TOKEN` from `/etc/observo/.env` once the broker strips `observo`. No code change. | **Done.** observo#629 closed 2026-09-29; the token removed 2026-10-01 (observo#652). |
| processor | Founding spec (this file, at the same path), `AGENTS.md`, #1 (from observo#629). | **Done.** |
| broker | New issue: ACL user `processor` (Section 2); strip `observo`'s grants; probe watches `processor.process`; participants table and `docs/STREAMS.md`; `docs/NETWORK-PATHS.md` client cells from `co-processor`. | **Done:** broker#75, closed 2026-10-05. |
| watcher | On #325 or a design-doc PR: Section 3's processor is Processor; output root `gs://co-gcs-processor`; the reader grant; the `WATCHER_EXTRACT_MODE` value `observo` (rename is Watcher's call); the Section 5 question below; the lockstep bump policy. | **Done:** watcher#325, closed 2026-10-03. |
| cannobserv | Doc-only issue: `CHANGE_BUS.md` names Observo / `observo.process` as the processor. | **Resolved upstream:** cannobserv#503 (closed 2026-10-05) names the group `processor.process` (on `main`, unreleased; the pinned 0.19.7 still carries the old docstrings). No issue filed from here. The row named the wrong file. The stale text was co-core's, not `CHANGE_BUS.md`: `streams.py` (module docstring, `CONTENT_PROCESS` comment) and `test_streams.py`. |
| replicator | New issue amending #69's roles charter (closed): "Observo transforms" becomes Observo = stream transforms and interpretation, Processor = discrete transforms. | **Moot, no post.** The quoted "Observo transforms" is in no Replicator file. The committed charter (`docs/contracts/replicator-boundaries.md`, "The successor question left this repo") keeps Replicator out of processing, points to archiver#179 and names no home. "Leaning Observo" survives only in #69's closing comment, which points to archiver#179, so the archiver comment corrects the chain. |
| archiver | Informational comment on #179: the processor is Processor. | **Done:** [archiver#179 comment](https://github.com/CannObserv/archiver/issues/179#issuecomment-6002156775), 2026-10-05. |
| GCP / tailnet | Bucket, service account and grants (Section 2); the tailnet rule. Operator acts. | **Done.** GCP: #14; the tailnet rule 2026-09-29 ([DEPLOYMENT.md](../DEPLOYMENT.md) prerequisites). |

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
- **Containment (amended 2026-10-06, #2).** Each layer alone, in a
  subprocess (`tests/test_contain.py`). Landlock alone still lets a pathname
  unix socket through, and that test fails if a kernel closes the gap. Then
  the contained child (`tests/test_child.py`): every denial is paired with an
  uncontained control, and the whole corpus, `cases.json` plus `real/`, gives
  results identical to the in-process `extract`. The suite runs `required`
  where the kernel allows it, says which mode in its header, and never skips
  on `co-processor`. `scripts/deploy.sh` runs end to end against a throwaway
  root with stubs (`tests/test_deploy.py`).
- **The drift check (amended 2026-10-07, #35).** Its verdicts come from
  GitHub's answer shapes through a fake `get` (`tests/test_drift.py`); the
  getter, the Status check-in (`tests/test_checkin.py`) and `processor drift`
  end to end run against local HTTP stubs, never GitHub or Status. A runtime
  lag past the grace sends `alert`, a docs-only one `ok`, a silent GitHub
  nothing. `scripts/deploy.sh` installing every unit under `deploy/`, and
  enabling a new timer once verified, has its own cases in
  `tests/test_deploy.py`.
- **The liveness check-in (amended 2026-10-08, #39).** The heartbeat's rules
  (fresh or stale, a refusal, an unreachable or hung Status, a tick that
  raises, cancel mid-check-in) run against local HTTP stubs and a socket that
  takes the connection and never answers (`tests/test_liveness.py`). Beside the
  real consumer on the scratch Redis, a hung Status never delays a command, and
  a wedged loop goes silent while the event loop keeps ticking
  (`tests/test_consumer.py`). `processor run` checking in end to end, and
  starting with liveness off on a missing key, is in `tests/test_main.py`. The
  smoke run's credential fallback is in `tests/test_deploy.py`.
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
