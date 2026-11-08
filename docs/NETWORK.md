# Network

The tailnet path to the broker: Tailscale's DNS, tailscaled's boot and OOM rank, and what a tailnet flap looks like.

## At a glance

| Thing | Value |
|---|---|
| Broker | `broker:6379`, by MagicDNS name, never by address. Tailscale runs `--accept-dns=true`, with an OOM drop-in for tailscaled: [§ below](#tailscale-dns) |

## Tailscale DNS

Since #8 (2026-10-01) this VM runs Tailscale with `--accept-dns=true`, as every cohort node does (observo#631, notifier#43 D8). It had run with DNS off since provisioning: the `tailscale up` of 2026-09-29, from an Observo session (observo#629), passed `--accept-dns=false`, and nothing records a reason.

How it works here:

- exeuntu has no `systemd-resolved`, so tailscaled runs in direct mode and writes `/etc/resolv.conf` itself, pointing it at MagicDNS (`100.100.100.100`).
- exe.dev wrote `nameserver 169.254.169.254` (its resolver) into `/etc/resolv.conf` at the VM's first boot: the file has the same 2026-09-29 22:50:42 mtime as `/etc/hosts`. At boot, before systemd starts, exe.dev's init rewrote it when it differed: after the 2026-10-01 hard reset it replaced the tailscaled file it found. It left it alone when it matched, after that day's graceful reboot, as it has `/etc/hosts` at both boots. tailscaled keeps that file as `/etc/resolv.pre-tailscale-backup.conf` and forwards public names to it, since the tailnet sets no global resolvers. The unit's `ExecStopPost=/usr/sbin/tailscaled --cleanup` puts it back on a clean stop, and tailscaled takes it over again when it starts.
- `broker` resolves through MagicDNS, so the bus URL names it and never its address. Broker's `docs/RECOVERY.md` rebuilds the node under the same name with a new address. Only the peers the tailnet policy shows this node resolve; on 2026-10-01 that was `broker` alone.

**`CorpDNS` must be true.** It can be false while `tailscale status` looks healthy: the peers are listed, and every tailnet name still fails (archiver#193). `tests/test_tailscaled.py` checks it on this node.

```bash
tailscale debug prefs | grep CorpDNS     # true
getent hosts broker storage.googleapis.com
```

### tailscaled's OOM rank

The accepted cost: tailscaled sits in the path of every lookup on the host, as well as carrying the bus. [deploy/tailscaled.service.d/90-processor-oom.conf](../deploy/tailscaled.service.d/90-processor-oom.conf) ranks it at `-950`, below `processor.service` (`-500`). `OOMScoreAdjust=` applies at exec, so restart tailscaled after installing it. The restart blips DNS and the tailnet: on 2026-10-01 `broker` resolved again 2.4 s after it, and `oom_score` went from 670 to 37.

```bash
sudo install -D -m 644 deploy/tailscaled.service.d/90-processor-oom.conf /etc/systemd/system/tailscaled.service.d/90-processor-oom.conf
sudo systemctl daemon-reload && sudo systemctl restart tailscaled
cat /proc/$(systemctl show -p MainPID --value tailscaled)/oom_score_adj   # -950
```

### At boot

**Proven across a graceful reboot (`sudo systemctl reboot`) and a hard reset (`exe.dev restart`), both on 2026-10-01.** A probe ran at each boot:

- One unit snapshots `/etc/resolv.conf` and the backup before tailscaled starts (from the hard reset on).
- A second unit times each lookup from tailscaled's start. It would have turned Tailscale DNS off if public names had still failed after 90 s.

The probe, its two units and its log are in `/var/backups/processor/8/` (root only). To arm it again, copy both units to `/etc/systemd/system/` and `systemctl enable` both; the second unit disables both after one run.

The graceful reboot:

- tailscaled started 1.1 s into boot, at `-950`, with `CorpDNS: true`.
- Public names resolved on the probe's first try (+0.5 s), through exe.dev's resolver in the file the stop hook had restored, which was in place from boot.
- tailscaled took the file over at +1.46 s, exact from the file's mtime. `broker` resolved by +1.65 s (replicator#88 measured about 2 s).
- The boot's journal has no name-resolution error.

The hard reset skips the stop hook, so tailscaled's own file and its backup were still in place when the VM went down:

- exe.dev's init replaced the file with `nameserver 169.254.169.254` 1.5 s into boot (mtime at 1.534 s), as it started its guest daemon (`exe-init guestd`, 1.53 s). guestd runs in PID 1's own `init.scope` rather than as a service, the mark of a process exe-init forked before it exec'd systemd. journald came up at 1.84 s. So every lookup a systemd unit makes before tailscaled takes over goes to exe.dev's resolver, as after a clean stop.
- tailscaled started at 2.1 s, at `-950`, with `CorpDNS: true`. It deleted the backup the reset left behind, then took the file over at +1.60 s, exact from the file's mtime, backing up exe.dev's fresh copy.
- Public names resolved on the probe's first try (+0.8 s). `broker` and `index` resolved by +1.67 s.
- The boot's journal has no name-resolution error.

The second unit polled every 0.25 s, starting 0.32 s (reboot) and 0.44 s (reset) after tailscaled, so the lookup times are upper bounds.

**With the unit installed, a graceful reboot on 2026-10-03 (#15)** (the first boot since the 2026-10-02 install):

- The stop hook restored exe.dev's resolver at 16:53:14Z. tailscaled and `processor` started together at 16:53:19.4–19.7Z, and tailscaled took the file over at 19.4 + 1.35 s.
- So the GCS preflight resolved through exe.dev's resolver. `starting` came at 21.74 and `consuming` at 21.76, about 1 s after `broker` became resolvable.
- `NRestarts=0`. No backoff warning, no preflight failure, and no name-resolution error in the boot's journal.
- The broker path was direct on the first ping and still direct after 5 idle minutes.
- `tests/test_main.py` pins the restart path: preflight failure exits 1, `Restart=on-failure`, inside the default start limit.

**With the unit installed, a hard reset on 2026-10-30 (#83)** (`ssh exe.dev restart co-processor`, the operator's, at about 17:54:12Z; the record is on #83):

- **The old boot just ends.** There's no shutdown, and no `stopped` from `processor`. Nothing of its journal was lost: a `logger` marker written seconds before the restart is its last line. That's measured but unexplained, since journald syncs every 5 min by default.
- **The new boot:**
  - the kernel's first line came at 17:54:32.5Z, the same kernel (`6.12.113`);
  - tailscaled and `processor` started at 17:54:33.6–33.9Z, and tailscaled took the file over 1.55 s later;
  - `starting` came at 38.430 (most of the 4.7 s since the unit started is the GCS preflight), and `consuming` at 38.447.
- **The bus was out for about 26 s at most** (from the marker to `consuming`), against about 20 s at #77's graceful reboot.
- **No restart or warning:** `NRestarts=0`, no WARNING, and no name-resolution error.
- **Status:**
  - liveness checked in at once (202); the silence was about 47 s against the 900 s grace, so nothing paged;
  - drift was `ok` at boot + 5.5 min (`OnBootSec=5min`);
  - disk headroom checked in at boot + 6 min, and neither GC timer caught up.
- **Nothing was in flight,** so nothing was reclaimed, and the DLQ stayed empty. A command caught mid-run would come back 10–11 min after its delivery (`reclaim_min_idle_ms` plus the 60 s tick), since a hard reset sends no SIGTERM and the command doesn't finish.

On both paths, tailnet names such as `broker` don't resolve until tailscaled takes the file over, 1.5–1.6 s after it starts; public names do. The bus loop backs off and retries a name that does not resolve yet, as it does for any broker fault (`tests/test_consumer.py`). The GCS preflight's listing retries a connection error for up to 120 s (the storage client's `DEFAULT_RETRY`). A start that still fails exits 1, and systemd restarts it 5 s later.

Check after a reboot:

```bash
tailscale debug prefs | grep CorpDNS                                       # true
getent hosts broker storage.googleapis.com github.com
cat /proc/$(systemctl show -p MainPID --value tailscaled)/oom_score_adj   # -950
journalctl -u processor -b | grep -ciE 'name or service not known|temporary failure in name resolution'
```

### Rollback

```bash
sudo tailscale set --accept-dns=false   # tailscaled moves its backup back over /etc/resolv.conf
grep nameserver /etc/resolv.conf        # 169.254.169.254
```

The backup is gone afterwards (measured 2026-10-01), so there is nothing left to copy. Restore it by hand only while tailscaled is down and its own file is still in place: `sudo cp /etc/resolv.pre-tailscale-backup.conf /etc/resolv.conf`. Once tailscaled is back up, run the `set` above, or it takes the file over again.

After this, `broker` stops resolving. Until DNS is back on, put the broker's address from `tailscale status` into `CO_PROCESSOR_BUS_URL` and restart the service.

## Tailnet flaps

**The rule** (operator, 2026-10-23, #67): "A tailnet flap gets an issue only if it delays a command, pages, or outlasts liveness's 900 s grace. Any other flap is logged on the epic with its window and signature."

A flap is the tailnet path to `broker` going away for minutes while tailscaled keeps running. The consumer backs off and retries, with no restart (MONITORING.md § Liveness). It has ridden out both flaps so far:

| Window | What it was | Processor |
|---|---|---|
| 2026-10-10 02:26–04:12Z | Path churn between direct (peers at the shared NAT `16.145.19.221`) and DERP. No `LinkChange` and no clock jump | 19 WARNINGs, 14 `open-conn-track` timeouts, no command affected |
| 2026-10-22 04:13:38–04:15:30Z | A host-side event on five of the six VMs behind egress `32.185.164.108` (broker, processor, replicator, status, notifier): memory-balloon pressure, then each VM paused, staggered over about 40 min | 6 WARNINGs, 1 failed check-in, no command delayed, nothing paged |

**The 2026-10-22 signature,** in order:

- The journal: no entry at all 04:12:53–04:13:26Z (32.7 s, the length of the jump). An idle VM can be that quiet, so this suggests the pause rather than proving it.
- kernel: `virtio_balloon virtio5: Out of puff! Can't get 1 pages` ×26, 04:13:26–38Z. The host was inflating the balloon, and the guest had no pages to give back. These were the only balloon messages on this boot (since 2026-10-03).
- `processor`: `consumer loop error; backing off`, `error` `TimeoutError: Timeout reading from broker:6379`, then `Timeout connecting to server` until the path is back (backoff 1, 2, 4 … s).
- tailscaled:
  - `magicsock: endpoints changed`, with a second STUN address beside the usual one;
  - `LinkChange: major, rebinding … rebind-reason=[time-jumped(33s),ips-changed]`. The wall clock jumped 33 s, which is what a paused VM looks like from inside;
  - `closing connection to derp-10 (rebind-ping-fail)`, then a reconnect;
  - `open-conn-track` timeouts.
- `processor`: possibly one `liveness check-in failed` (the 10 s bound). The next check-in lands inside the 900 s grace, so nothing pages.
- Recovery on its own: `NRestarts=0`, and `ack: complete` resumes. A command issued during the gap stays in the stream and is acked once the path is back.

**The cohort saw the same event** (a Mayfly questionnaire, 2026-10-23; synthesis on #67, the broker's timeline on CannObserv/broker#118):
- co-broker and co-replicator, behind the same egress, each logged the first balloon pressure of their boot (04:13:07Z and 04:13:39Z), then a 31–33 s time jump;
- co-status (moved onto that egress during the event) had balloon pressure at 04:14:04Z and a pause at about 04:17:07–24Z;
- notifier, behind it too, had the same 19 min later: balloon pressure at 04:32:48Z, then a pause logged at 04:51:10Z;
- on two VMs without swap (status at 7.9 GiB, notifier at 3.8 GiB, as their earlyoom configs size them), the balloon pushed memory to earlyoom's floor, and earlyoom killed a user process (not a service);
- co-index, behind the same egress, logged neither. It has room to spare, and `Out of puff` appears only when the guest *can't* give pages back, so a quiet balloon there isn't ruled out. It did pause on its own on 2026-10-18 around 04:06Z; co-processor didn't;
- co-registrar (archiver), co-provisioner and co-watcher, each on its own egress, logged neither; they only lost `broker` for about 2 min.

The events were staggered per VM, not one simultaneous pause. Every bus client lost `broker` at once, from about 04:13:18 to 04:15:33Z. That window matches the broker's balloon reclaim, not its own pause, which its clock places about 04:16:14–43Z (CannObserv/broker#118).

exe.dev's docs don't describe maintenance, host migration, ballooning or pauses. The cause is a host-side memory reclaim they don't explain. Asking exe.dev is the operator's step (CannObserv/broker#118).

**Telling it apart:**

- **A broker-side problem** (a Redis restart, the broker's patch window, broker#91, or a host pause of the broker alone, which is how archiver, provisioner and watcher saw 2026-10-22): this VM's tailscaled logs no `LinkChange` or `time-jumped`. The broker's patch run (2026-10-13 04:17–04:22Z) left backoff WARNINGs at its Redis restart and at its reboot, and `open-conn-track` timeouts during the reboot.
- **A tailscaled restart here** (#65, 2026-10-22 23:40:29Z): tailscaled gets a new `MainPID` and logs `linkChange: in state NoState`. The tailnet is down for 2.3 s, and the broker socket survives with no WARNING.
- **A flap:** this VM's tailscaled logs `endpoints changed` and `LinkChange` (and, on 2026-10-22, `time-jumped`), or peers go stale or relayed in `tailscale status`.

```bash
journalctl -u processor -p warning -o cat --since <start> | jq -cR 'fromjson? | {timestamp, level, logger, message, error}'
journalctl -u tailscaled -o short-iso --since <start> | grep -E 'LinkChange|endpoints changed|rebind-reason|closing connection to derp|open-conn-track'
journalctl -k -o short-iso --since <start> | grep -E 'Out of puff|balloon'
systemctl show processor -p NRestarts
tailscale status
```

`-p warning` does the level filter (#76) and `fromjson?` drops systemd's own warning lines; for a window before #76's deploy, every record is `PRIORITY=6`, so drop `-p warning` and filter with `select(.level != "INFO")` instead (DEPLOYMENT.md § Operate → Logs).

Log a flap on the epic as: the window (first WARNING to the first `ack: complete` after it), the counts above, and which kind it was.
