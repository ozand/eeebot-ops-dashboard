"""One-off systemd condition transaction. Not an installer or a backup tool."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

UNITS = Path('/etc/systemd/system')
SVC = 'eeebot-techtree-publish.service'
TIMER = 'eeebot-techtree-publish.timer'
DEADLINE = 0.0
PHASE = 'preflight'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}


def require(ok):
    if not ok:
        raise RuntimeError('state mismatch')


def remaining():
    left = DEADLINE - time.monotonic()
    require(left > 0)
    return left


def call(*args):
    left = remaining()
    return subprocess.run(args, env=ENV, check=True, timeout=left,
                          capture_output=True, text=True).stdout


def protected_dir(path):
    for p in (path, *path.parents):
        st = p.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022)


def absent(path):
    return not os.path.lexists(path)


def contents(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
        s = os.fstat(f.fileno())
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size <= limit and s.st_uid == 0
                and not s.st_mode & 0o022)
        b = f.read(limit + 1)
        require(len(b) == s.st_size)
        return b


def create(path, data, mode):
    remaining()
    fd, tmp = tempfile.mkstemp(prefix='.431-tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        remaining()
        os.link(tmp, path)  # Never replace an existing object.
    finally:
        os.unlink(tmp)
        sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def properties(unit, *keys):
    out = call('systemctl', '--no-pager', 'show', '--all', unit,
               *(f'--property={key}' for key in keys))
    rows = [line.split('=', 1) for line in out.splitlines()]
    result = dict(rows)
    require(len(rows) == len(keys) and set(result) == set(keys))
    return result


def conditions(unit):
    label = re.sub(r'[^a-zA-Z0-9]', lambda m: f'_{ord(m[0]):02x}', unit)
    row = shlex.split(call('busctl', '--no-pager', 'get-property',
                          'org.freedesktop.systemd1',
                          '/org/freedesktop/systemd1/unit/' + label,
                          'org.freedesktop.systemd1.Unit', 'Conditions'))
    require(len(row) >= 2 and row[0] == 'a(sbbsi)' and row[1].isdigit())
    count = int(row[1])
    require(len(row) == 2 + count * 5)
    result = []
    for i in range(2, len(row), 5):
        kind, trigger, negate, param, status = row[i:i + 5]
        require(trigger in ('true', 'false') and negate in ('true', 'false')
                and status in ('-1', '0', '1'))
        result.append([kind, trigger, negate, param])
    return result


def config(service, timer, drop, marker, guarded, pending_ok=False):
    result = {}
    for unit in (service, timer):
        p = properties(unit, 'Id', 'LoadState', 'UnitFileState', 'FragmentPath',
                       'DropInPaths', 'NeedDaemonReload')
        pending = p.pop('NeedDaemonReload')
        require(p['Id'] == unit and p['LoadState'] == 'loaded')
        require(pending == 'no' or (pending == 'yes' and pending_ok and unit == service))
        includes = p['DropInPaths'].split()
        expected = int(guarded and unit == service) if guarded is not None else None
        count = includes.count(str(drop))
        require(count in (0, 1))
        require(count == expected if expected is not None else (unit == service or count == 0))
        p['DropInPaths'] = [path for path in includes if path != str(drop)]
        paths = [p['FragmentPath'], *p['DropInPaths']]
        require(all(Path(path).is_absolute() for path in paths))
        p['files'] = []
        for path in paths:
            st = os.stat(path)
            require(stat.S_ISREG(st.st_mode))
            p['files'].append([st.st_dev, st.st_ino, st.st_size, st.st_ctime_ns])
        cond = conditions(unit)
        exact = ['ConditionPathExists', 'false', 'true', str(marker)]
        require(cond.count(exact) == count)
        p['Conditions'] = [c for c in cond if c != exact]
        result[unit] = p
    result[timer].update(properties(timer, 'ActiveState'))
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def remove(path, expected):
    require(contents(path, len(expected)) == expected)
    remaining()
    path.unlink()
    sync_dir(path.parent)


def quiet(service):
    p = properties(service, 'ActiveState', 'MainPID', 'ControlPID')
    require(p['ActiveState'] in ('inactive', 'failed') and p['MainPID'] == p['ControlPID'] == '0')
    jobs = call('systemctl', 'list-jobs', '--no-pager', '--no-legend', '--full', service)
    require(not jobs.strip())


def operate(action, scratch, seconds):
    global DEADLINE, PHASE
    PHASE = 'preflight'
    start = time.monotonic()
    DEADLINE = start + seconds
    drop = UNITS / (SVC + '.d') / '99-eeebot-pr431-capture.conf'
    marker = UNITS / '.eeebot-pr431-capture-marker'
    drop_bytes = f'[Unit]\nConditionPathExists=!{marker}\n'.encode()
    marker_bytes = b'eeebot-pr431-capture-only\n'
    state = scratch / '431-unit-baseline'
    protected_dir(drop.parent)
    protected_dir(scratch)
    require(stat.S_IMODE(scratch.stat().st_mode) == 0o700)

    def check(guarded, pending_ok=False):
        return config(SVC, TIMER, drop, marker, guarded, pending_ok).encode()

    def reload():
        call('systemctl', 'daemon-reload')

    if action in ('precheck', 'arm'):
        require(absent(marker) and absent(drop) and absent(state))
        baseline = check(False)
        if action == 'precheck':
            print(f'BASELINE CONFIG MATCHABLE; elapsed_seconds={time.monotonic() - start:.3f}', flush=True)
            return
        create(state, baseline, 0o600)
        PHASE = 'arming-marker'
        create(marker, marker_bytes, 0o600)
        PHASE = 'arming-drop-in'
        create(drop, drop_bytes, 0o644)
        PHASE = 'arming-reload'
        reload()
        require(contents(marker, len(marker_bytes)) == marker_bytes
                and contents(drop, len(drop_bytes)) == drop_bytes
                and check(True) == baseline)
        print('ARMED; active/queued work not checked', flush=True)
    else:
        baseline = contents(state, 64)
        require(re.fullmatch(b'[a-f0-9]{64}', baseline) is not None)
        have_marker, have_drop = not absent(marker), not absent(drop)
        require(not have_marker or contents(marker, len(marker_bytes)) == marker_bytes)
        require(not have_drop or contents(drop, len(drop_bytes)) == drop_bytes)
        if action == 'check':
            require(have_marker and have_drop and check(True) == baseline)
            quiet(SVC)
            print('UNIT QUIET; verify independent writers and baseline separately', flush=True)
        elif action in ('inspect', 'release-verified-baseline'):
            PHASE = 'inspect'
            require(check(None, pending_ok=True) == baseline)
            if action == 'inspect':
                exact = ['ConditionPathExists', 'false', 'true', str(marker)]
                loaded = exact in conditions(SVC)
                print(f'MATCHED; marker={int(have_marker)} drop_in={int(have_drop)} '
                      f'loaded_condition={int(loaded)}; not readiness', flush=True)
                return
            PHASE = 'releasing-drop-in'
            if have_drop:
                remove(drop, drop_bytes)
            PHASE = 'releasing-reload'
            reload()
            require(check(False) == baseline and absent(drop))
            PHASE = 'releasing-marker'
            if have_marker:
                remove(marker, marker_bytes)
            require(absent(marker) and check(False) == baseline)
            print('RELEASED; original unit configuration matched', flush=True)
        else:
            raise RuntimeError('unsupported action')
    print(f'elapsed_seconds={time.monotonic() - start:.3f}', flush=True)


if __name__ == '__main__':
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0)
        require(len(sys.argv) == 4 and sys.argv[1] in (
                'precheck', 'arm', 'check', 'inspect', 'release-verified-baseline'))
        scratch = Path(sys.argv[2])
        require(scratch.is_absolute() and scratch.resolve(strict=True) == scratch)
        require(scratch.is_relative_to('/root') and scratch != Path('/root'))
        seconds = int(sys.argv[3])
        require(1 <= seconds <= 120)
        operate(sys.argv[1], scratch, seconds)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f'STOP phase={PHASE} error={type(error).__name__}', file=sys.stderr)
        sys.exit(1)
