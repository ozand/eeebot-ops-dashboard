"""Run only on the disposable systemd CI runner; never target host units."""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile


def systemctl(*args):
    return subprocess.check_output(('systemctl', *args), timeout=15, text=True)


def require(ok):
    if not ok:
        raise RuntimeError('fixture assertion failed')


def main():
    require(os.geteuid() == 0)
    helper = runpy.run_path(str(Path(__file__).with_name('hold-431.py')))
    operate = helper['operate']
    units = Path('/run/systemd/system')
    with tempfile.TemporaryDirectory(prefix='431-probe-', dir='/root') as dirname:
        root = Path(dirname)
        service, timer = root.name + '.service', root.name + '.timer'
        fragment, timerfile = units / service, units / timer
        dropdir = units / (service + '.d')
        drop = dropdir / '99-eeebot-pr431-capture.conf'
        marker = units / '.eeebot-pr431-capture-marker'
        require(all(not os.path.lexists(p) for p in (fragment, timerfile, dropdir, marker)))
        operate.__globals__.update(UNITS=units, SVC=service, TIMER=timer)
        ran = root / 'ran'
        def step(action):
            operate(action, root, 20)

        try:
            fragment.write_text(f'[Service]\nType=oneshot\nExecStart=/usr/bin/touch {ran}\n')
            timerfile.write_text(f'[Timer]\nOnActiveSec=1h\nUnit={service}\n')
            dropdir.mkdir()
            systemctl('daemon-reload')
            systemctl('start', timer)
            systemctl('start', service)
            require(ran.is_file())
            ran.unlink()
            step('precheck')
            step('arm')
            step('check')
            systemctl('start', service)
            require(not ran.exists())
            require(systemctl('show', '-p', 'ConditionResult', '--value', service).strip() == 'no')
            print('PASS: loaded condition skipped the harmless command', flush=True)

            # Reproduce the partial-release filesystem/manager state, not a killed reload.
            drop.unlink()
            require(marker.is_file() and not drop.exists())
            step('inspect')
            step('release-verified-baseline')
            require(not marker.exists() and not drop.exists())
            require(systemctl('is-active', timer).strip() == 'active')
            systemctl('start', service)
            require(ran.is_file())
            print('PASS: explicit release recovered from missing drop-in before reload', flush=True)
        finally:
            systemctl('stop', timer, service)
            for path in (drop, marker, timerfile, fragment):
                path.unlink(missing_ok=True)
            if dropdir.exists():
                dropdir.rmdir()
            systemctl('daemon-reload')


if __name__ == '__main__':
    main()
