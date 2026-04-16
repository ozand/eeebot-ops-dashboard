# eeepc Privileged Live Activation Handoff

Last updated: 2026-04-16 UTC

This is the operator-safe handoff for activating the already-implemented goal-rotation/task-plan fixes on live eeepc.

Do not treat this as the switch itself. It is the exact deploy/verify bundle for a privileged operator.

## Deploy commits

Apply the nanobot commits that contain the fixes:
- `bfca5f5` — `feat: add bounded task plan snapshots`
- `d48c777` — `Add loop-breaker goal rotation guard`

Deploy the release archive built from `d48c777`:
- archive: `/tmp/nanobot-d48c777.tar.gz`
- sha256: `9dd2de92e4723de925e1ac1b91c35578142bd6d24fbad2302c3cb2cb8953ddfe`

Suggested pinned release directory for the activation step:
- `/home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777`

## Live target paths

- pinned runtime base: `/home/opencode/.nanobot-eeepc/runtime/pinned`
- active symlink: `/home/opencode/.nanobot-eeepc/runtime/pinned/current`
- live authority root: `/var/lib/eeepc-agent/self-evolving-agent/state`
- gateway service: `nanobot-gateway-eeepc.service`

## Privileged activation commands

Run these as the privileged operator on eeepc:

```bash
sudo mkdir -p /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777
sudo tar -xzf /tmp/nanobot-d48c777.tar.gz -C /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777
sudo chown -R opencode:opencode /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777
sudo ln -sfn /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777 /home/opencode/.nanobot-eeepc/runtime/pinned/current
sudo systemctl restart nanobot-gateway-eeepc.service
```

## Verification commands after switch

```bash
sudo readlink -f /home/opencode/.nanobot-eeepc/runtime/pinned/current
sudo systemctl is-active nanobot-gateway-eeepc.service
sudo journalctl -u nanobot-gateway-eeepc.service -n 50 --no-pager
sudo env PYTHONPATH=/home/opencode/.nanobot-eeepc/runtime/pinned/20260416-2051-d48c777 \
  /home/opencode/.venvs/nanobot/bin/nanobot status \
  --runtime-state-source host_control_plane \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state
```

## Expected proof if successful

The status output should include these exact operator-visible fields:
- `Runtime state source: host_control_plane`
- `Runtime state root: /var/lib/eeepc-agent/self-evolving-agent/state`
- `Runtime status: PASS` or another truthful live status
- `Active goal`
- `Plan source`
- `Task plan schema: task-plan-v1`
- `Goal rotation reason`
- `Goal rotation streak`
- `Goal rotation trigger`
- `Goal rotation artifacts`
- `Report source`
- `Goal source`
- `Outbox source`

Expected proof files and fields:

- `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-*.json`
  - `process_reflection.status`
  - `goal.goal_id`
  - `follow_through.artifact_paths`

- `/var/lib/eeepc-agent/self-evolving-agent/state/goals/current.json`
  - `schema_version`
  - `current_task_id`
  - `task_counts`
  - `reward_signal`
  - `rotation_reason`
  - `rotation_streak`
  - `rotation_trigger_goal`
  - `rotation_trigger_artifact_paths`

- `/var/lib/eeepc-agent/self-evolving-agent/state/goals/active.json`
  - same rotation and task-plan fields if current.json is absent

- `/var/lib/eeepc-agent/self-evolving-agent/state/goals/history/cycle-*.json`
  - task-plan history fallback fields when present

- `/var/lib/eeepc-agent/self-evolving-agent/state/outbox/report.index.json`
  - `status`
  - `source`
  - `goal.goal_id`
  - `goal.follow_through.artifact_paths`
  - `capability_gate.approval`

## Operator rule

Do not widen scope. If the symlink switch or service restart fails, stop, record the failure, and do not improvise a broader repair in the same step.
