"""Host-only replay protocol test and independent JSON input-script oracle."""
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    cc = os.environ.get('HOST_CC') or shutil.which('gcc')
    if not cc:
        raise SystemExit('HOST_CC or gcc is required')
    out = ROOT / 'build' / 'host-tests'
    out.mkdir(parents=True, exist_ok=True)
    suffix = '.exe' if os.name == 'nt' else ''
    for enabled in (0, 1):
        exe = out / ('diagnostic_replay_%d%s' % (enabled, suffix))
        subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-DH1_DIAGNOSTIC=%d' % enabled,
                        str(ROOT / 'tests/diagnostic_replay_test.c'),
                        str(ROOT / 'src/platform/diagnostic_replay.c'),
                        '-o', str(exe)], check=True)
        result = subprocess.run([str(exe), '--trace'], check=True,
                                capture_output=True, text=True)
        if not enabled:
            print(result.stdout.strip())
            continue
        actual = {'boot': {}, 'run': {}}
        for line in result.stdout.splitlines():
            if line.startswith('PASS:'):
                print(line)
            else:
                stage, frame, mask = line.split(',')
                assert int(frame) not in actual[stage]
                actual[stage][int(frame)] = int(mask)
        # Independent source data: the existing full-application scenario.
        events = json.loads((ROOT / 'tests/scenarios/sgz2-newgame-city.json').read_text())
        by_frame = {}
        for event in events:
            by_frame.setdefault(event['frame'], []).append(event)
        buttons = 0
        physical_to_libretro = {25: 1 << 3, 16: 1 << 8}
        for frame in range(1, 3301):
            for event in by_frame.get(frame, []):
                bit = physical_to_libretro[event['key']]
                buttons = buttons | bit if event['down'] else buttons & ~bit
            stage = 'boot' if frame <= 1500 else 'run'
            relative = frame if stage == 'boot' else frame - 1500
            assert actual[stage][relative] == buttons, (stage, relative, buttons)
        assert len(actual['boot']) == 1500 and len(actual['run']) == 1800
        print('PASS: every boot/run button mask matches the existing city JSON scenario')


if __name__ == '__main__':
    main()
