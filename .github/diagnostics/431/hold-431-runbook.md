# #431 capture-only hold — executable companion (not yet cleared for eeepc)

Use with `capture-window-431.md`, `rollout-431.md` and `scratch-restore-431.md`. `hold-431.py` handles only the publisher condition and its reversal. It never starts/stops a unit, reads the install tree, or runs capture, sync or publish. Commands below are for the designated operator after exact review and a successful disposable-systemd test of the companion; do not use the production publisher as the test.

## Preflight

Use clean root Python 3.9+ in isolated mode (`/usr/bin/python3 -I`), with the verified helper in a fresh protected directory below `/root`; call that directory `$work`. No symlink or mount alias, including in the existing publisher drop-in directory. Confirm host policy, relevant ACLs, the persistent boot filesystem, effective trigger paths and protection from other writers to unit files. Reserve exclusive custody for the full operation. Verify baseline master and plan prerequisites. Run:

```sh
/usr/bin/python3 -I "$work/hold-431.py" precheck "$work" 20
```

This is read-only. Require a successful exit and `BASELINE CONFIG MATCHABLE`. No hold on bad input, foreign state or unknown config. Set an explicit absolute cutoff shorter than the verified timer interval. Reserve a distinct release margin covering **both** the release baseline checks and systemd reversal. Measure these separately (read-only baseline verification on eeepc; reversal on disposable systemd with this helper); use observed values plus operator-selected slack, never treat either as a worst-case host bound. No capture if remaining time cannot cover release at every handoff.

## Arm, drain, capture, release

```sh
/usr/bin/python3 -I "$work/hold-431.py" arm "$work" 20
/usr/bin/python3 -I "$work/hold-431.py" check "$work" 20
```

Run separately, not as a pasted batch. `ARMED` means the marker and mandatory condition are in place, not that the publisher is idle. On a busy result, wait only for the already admitted work to finish naturally, within the cutoff. `UNIT QUIET` is one checked state: also verify all writer paths, master and the ten-file baseline as required by the capture plan. Then capture only those ten files. Do not begin the scratch trial yet.

By the start of the reserved release margin, stop capture. Check master, the ten installed files against the approved baseline and current disk configuration, including any new fragments/drop-ins and the original timer and bridge definitions. The helper compares loaded publisher/timer configuration, source-file identities and timer activity; it does not replace this disk, trigger and baseline check. Release also requires that no changes or jobs outside the known writer boundary can invalidate the check. Do not proceed after an ambiguous result.

```sh
/usr/bin/python3 -I "$work/hold-431.py" release-verified-baseline "$work" 20
```

Verify exit 0, `RELEASED`, original effective configuration and normal scheduling; only then rehearse restore in scratch. A normal job can start as soon as the release reload takes effect. Never compare publisher ActiveState to a historical idle snapshot to infer configuration restoration.

## Interrupted or nonzero action

Keep the protected work directory. Do not rerun `arm`, delete files by name, use a trap that releases automatically, or assume the hold is effective. Re-establish custody first. If a manager reload was sent, establish that it has settled before interpreting a new snapshot; a command timeout is not proof that reload did not happen.

```sh
/usr/bin/python3 -I "$work/hold-431.py" inspect "$work" 20
```

`MATCHED` reports disk marker/drop-in presence and whether the condition is loaded, not readiness. If it matches and the independent release checks above pass, the same explicit release command handles marker-only, loaded or not-yet-loaded drop-in, and interrupted release states. If no baseline record exists, foreign configuration is present, the check is inconclusive, or custody cannot be re-established, stop and escalate the actual state. Before arming establish an available operator and a reconnection or local access path; this procedure cannot guarantee restoration if the host is unreachable. No automatic unhold, reboot, or manual publisher start.