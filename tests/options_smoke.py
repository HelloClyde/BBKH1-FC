"""Exercise PAL and display skipping through a real test-20 MIPS BDA.

Uses the same in-memory H1 firmware mock as mips_smoke.py. Per-ROM settings
are genuine version-2, CRC-protected config slots, with full-ROM identity.
No guest program, core memory or executable instruction is patched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

from h1_mock import Machine, ROOT
from make_test_rom import make_rom
from mips_smoke import check_audio, save_data

LOG = 'A:\\FC\\h1fc.log'
FRAMES = 20


def cartridge(region: str) -> bytes:
    rom = bytearray(make_rom())
    if region == 'PAL':
        # NES 2.0 mapper 0, 16 KiB PRG, 8 KiB CHR and 8 KiB battery RAM.
        # Byte 12 is the standard header timing mode; the frontend remains Auto.
        rom[7:13] = bytes([8, 0, 0, 0x70, 0, 1])
    return bytes(rom)


def config_slot(rom: bytes, skip: int) -> bytes:
    mapping = bytearray(45)
    for key, retro_button in {16: 8, 17: 0, 25: 3, 23: 2, 37: 4,
                              27: 5, 40: 6, 35: 7, 39: 8}.items():
        mapping[key] = retro_button + 1
    payload = struct.pack('<5I', 0x31474643, 2, 1, skip, 1) + mapping + bytes([100, 0, 0])
    assert len(payload) == 68
    identity = zlib.crc32(rom) ^ len(rom)
    header = struct.pack('<6I', 0x31534748, 1, identity, 1,
                         len(payload), zlib.crc32(payload))
    return header + struct.pack('<I', zlib.crc32(header)) + payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, default=ROOT / 'build/test-20/H1FC.bda')
    args = parser.parse_args()
    output = ROOT / 'build/verification'
    output.mkdir(parents=True, exist_ok=True)
    cases = {}

    def run(region: str, skip: int | None, restore_files=None):
        name = f'{region.lower()}-' + ('default' if skip is None else f'skip{skip}') + ('-restored' if restore_files else '')
        path = f'A:\\FC\\OPTIONS-{region}.nes'
        rom = cartridge(region)
        files = dict(restore_files) if restore_files else {path: rom}
        if skip is not None and not restore_files:
            files[path + '.cfg.s0'] = config_slot(rom, skip)
        expected_skip = 1 if skip is None else skip
        machine = Machine(args.bda, files=files, path=path)
        print('RUN:', name, flush=True)
        try:
            duration = machine.run()
        finally:
            (output / ('options-' + name + '.log')).write_bytes(machine.files.get(LOG, b''))
        log = machine.files[LOG].decode('gbk')
        assert f'CONFIG_LOAD status={int(skip is not None)} enabled=1 scale=1 skip={expected_skip} sound=1 volume=100' in log
        assert machine.blits == (FRAMES + expected_skip) // (expected_skip + 1), (name, machine.blits)
        assert f'STOP video={machine.blits} errors=0' in log
        timing = re.search(r'NES_TIMING frame_us=(\d+) rate=(\d+)', log)
        assert timing, 'No core AV timing advertised'
        frame_us, rate = map(int, timing.groups())
        assert rate == 22050
        expected_us = 20000 if region == 'PAL' else 16639
        assert abs(frame_us - expected_us) < 20, (name, frame_us)
        close = re.search(r'AUDIO_CLOSE_BEGIN batches=(\d+) submitted=(\d+) nonzero=(\d+) '
                          r'dropped=(\d+) failures=(\d+) underruns=(\d+)', log)
        assert close, 'No native audio shutdown metrics'
        batches, submitted, nonzero, dropped, failures, underruns = map(int, close.groups())
        assert batches == FRAMES and nonzero > 1000, (name, close.groups())
        assert dropped == failures == 0, (name, close.groups())
        saved = save_data(machine, path)
        assert saved[0:2] == b'\x5a\x11' and saved[3:5] == b'\x42\x1e'
        assert saved[2] >= 10, ('CPU did not advance through skipped output frames', saved[:8].hex())
        assert saved[5] == (0xC0 if restore_files else 0), ('Battery restore marker', saved[:8].hex())
        if restore_files:
            assert 'SAVE restored' in log
        cases[name] = {'seconds': round(duration, 2), 'display_frames': machine.blits,
                       'core_frames': batches, 'cpu_frame_counter': saved[2],
                       'frame_us': frame_us, 'sample_rate': rate,
                       'audio_nonzero_samples': nonzero, 'audio_dropped': dropped,
                       'audio_failures': failures, 'mock_audio_underruns': underruns,
                       'pcm': check_audio(machine),
                       'battery_restored': bool(restore_files),
                       'save_prefix': saved[:8].hex(),
                       'screen_sha256': hashlib.sha256(machine.screen).hexdigest(),
                       'rom_sha256': hashlib.sha256(rom).hexdigest()}
        return machine, saved

    for region in ('NTSC', 'PAL'):
        baseline = None
        for skip in range(3):
            machine, saved = run(region, skip)
            if baseline is None:
                baseline = (saved, machine.screen, cases[f'{region.lower()}-skip0'])
            else:
                assert saved == baseline[0], 'Display skipping changed final CPU-written battery RAM'
                assert machine.screen == baseline[1], 'Display skipping changed the final static picture'
                assert cases[f'{region.lower()}-skip{skip}']['audio_nonzero_samples'] == baseline[2]['audio_nonzero_samples'], \
                    'Display skipping changed core audio generation'
        run(region, 2, machine.files)
        default, saved = run(region, None)
        assert saved == baseline[0] and default.screen == baseline[1]
        assert cases[f'{region.lower()}-default']['audio_nonzero_samples'] == baseline[2]['audio_nonzero_samples']

    report = {'ok': True, 'kind': 'Actual test-20 MIPS BDA with mocked H1 firmware',
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'cases': cases,
              'assertions': ['NES 2.0 PAL header selects approximately 50 Hz core timing',
                             'Absent settings default to skip 1: 10 displays and 20 CPU/APU frames',
                             'NTSC remains approximately 60.1 Hz',
                             'Skip 0/1/2 displays exactly 20/10/7 of 20 core frames',
                             'Every skipped output frame still runs CPU and generates APU audio',
                             'Final CPU-written battery RAM is identical across skip levels',
                             'Battery save is restored on relaunch with skip 2 in both regions',
                             'Settings are loaded through CRC-checked per-ROM config slots',
                             'MIPS ABI and guest resource cleanup'],
              'limitation': 'Timing values are core declarations. Mock execution does not measure physical '
                            'H1 speed or audio quality. Skipping omits output conversion/display; '
                            'the PPU still executes.'}
    destination = output / 'options-smoke.json'
    destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
