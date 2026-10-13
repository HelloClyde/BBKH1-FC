"""Verify all three NES state slots, paused CPU/RAM, PCM resume and menu exit."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

from PIL import Image
from h1_mock import Machine, ROOT
from make_test_rom import make_rom

LOG = 'A:\\FC\\h1fc.log'
ROM = 'A:\\FC\\PAUSE.nes'


def key(code: int) -> list[tuple[int, int]]:
    return [(9, code), (10, code)]


def symbol(name: str, map_path: Path) -> int:
    match = re.search(r'(0x[0-9a-fA-F]+)\s+' + re.escape(name) + r'\s*$',
                      map_path.read_text(), re.M)
    assert match, ('Missing map symbol', name)
    return int(match[1], 16)


class Pause(Machine):
    def __init__(self, bda: Path, map_path: Path, files=None, errors=False):
        self.opens = self.closes = self.inits = 0
        super().__init__(bda, path=ROM, files=files or {ROM: make_rom()})
        self.watched = [(symbol('RAM', map_path), 2048), (symbol('X', map_path) + 4, 8)]
        self.pauses = self.idle = self.restored = 0
        self.mutated = False
        self.resumed_at = None
        self.error_mode = errors
        self.frozen = None
        self.golden = None
        self.actions = [(10, 31)]  # Release the opening P key.
        if errors:
            # Load corrupt slot 1 and empty slot 2, then resume.
            self.actions += key(35) * 3 + key(39) + key(39) + key(27) + key(39) + key(41) + key(41)
        else:
            # Main menu down selects Save. State pages move one slot per down.
            self.actions += (key(27) + key(39) + key(39) + key(27) + key(39)
                             + key(27) + key(39) + key(41)
                             + key(35) * 3 + key(39) + key(39) + key(27)
                             + key(39) + key(27) + key(39) + key(41) + key(39))

    def pcm_open(self, *args):
        self.opens += 1
        return super().pcm_open(*args)

    def pcm_close(self, *args):
        self.closes += 1
        return super().pcm_close(*args)

    def pcm_init(self, *args):
        self.inits += 1
        return super().pcm_init(*args)

    def snapshot(self):
        return [self.read(address, size) for address, size in self.watched]

    def event(self, code, button, *args):
        log = self.files.get(LOG, b'')
        paused = log.rfind(b'PAUSE_MENU_BEGIN') > log.rfind(b'PAUSE_MENU_END')
        event = (-1, -1)
        if self.blits >= 8 and not self.pauses:
            self.pauses = 1
            event = (9, 31)
        elif paused:
            assert not self.audio_active and not self.audio_queue, 'Menu must pause PCM'
            if self.pauses == 1 and self.idle < 12:
                if self.frozen is None:
                    self.frozen = self.snapshot()
                assert self.snapshot() == self.frozen, 'CPU/RAM advanced while paused'
                self.idle += 1
            else:
                if self.error_mode and self.pauses == 1:
                    assert self.snapshot() == self.frozen, 'Invalid state changed CPU/RAM'
                if not self.error_mode and self.pauses == 1:
                    if b'STATE_SAVE slot=3 status=1' in log and self.golden is None:
                        self.golden = self.snapshot()
                    if self.mutated and ('STATE_LOAD slot=%d status=1' % (self.restored + 1)).encode() in log:
                        assert self.snapshot() == self.golden, 'State did not restore CPU and RAM'
                        self.restored += 1
                        self.mutated = False
                    if self.golden and self.restored < 3 and not self.mutated:
                        for address, size in self.watched:
                            self.write(address, bytes([0x55 + self.restored]) * size)
                        self.mutated = True
                assert self.actions, 'Unexpected menu remained open'
                event = self.actions.pop(0)
        elif self.pauses:
            if self.resumed_at is None:
                self.resumed_at = self.blits
            if self.blits - self.resumed_at >= 4 and self.pauses == 1:
                self.pauses = 2
                self.actions = [(10, 31)] + key(27) * 3 + key(35) + key(39)
                event = (9, 31)  # Second pause: select Exit from the menu.
        self.word(code, event[0])
        self.word(button, event[1])
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, default=ROOT / 'dist/H1FC.bda')
    parser.add_argument('--map', type=Path, default=ROOT / 'build/release/H1FC.map')
    args = parser.parse_args()
    out = ROOT / 'build/verification'
    out.mkdir(parents=True, exist_ok=True)

    machine = Pause(args.bda, args.map)
    try:
        seconds = machine.run()
    finally:
        (out / 'pause-menu.log').write_bytes(machine.files.get(LOG, b''))
    assert machine.restored == 3 and machine.pauses == 2
    assert machine.opens == machine.closes == machine.inits == 1, 'PCM device not reused on resume'
    assert b'PAUSE_MENU_END action=0' in machine.files[LOG]
    assert b'PAUSE_MENU_END action=2' in machine.files[LOG]
    state_sizes = []
    for slot in range(1, 4):
        saved = machine.files[ROM + '.st%d.s0' % slot]
        assert zlib.crc32(saved[:24]) == struct.unpack_from('<I', saved, 24)[0]
        assert zlib.crc32(saved[28:]) == struct.unpack_from('<I', saved, 20)[0]
        assert struct.unpack_from('<3I', saved, 28) == (0x31545348, 1, 3)
        state_sizes.append(len(saved))
    frame = struct.unpack('<130560I', machine.menu_frames[1])
    image = Image.new('RGB', (480, 272))
    image.putdata([((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255) for pixel in frame])
    image.save(out / 'fc-pause-menu.png')

    files = dict(machine.files)
    corrupted_path = ROM + '.st1.s0'
    corrupted = bytearray(files[corrupted_path])
    corrupted[100] ^= 1
    files[corrupted_path] = bytes(corrupted)
    del files[ROM + '.st2.s0']
    invalid = Pause(args.bda, args.map, files=files, errors=True)
    try:
        error_seconds = invalid.run()
    finally:
        (out / 'pause-menu-errors.log').write_bytes(invalid.files.get(LOG, b''))
    assert b'STATE_LOAD slot=1 status=-1' in invalid.files[LOG]
    assert b'STATE_LOAD slot=2 status=0' in invalid.files[LOG]
    assert invalid.files[corrupted_path] == bytes(corrupted)

    report = {'ok': True, 'kind': 'Actual MIPS BDA, mocked H1 service tables',
              'sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'seconds': [round(seconds, 2), round(error_seconds, 2)],
              'state_slots_restored': machine.restored, 'state_bytes': state_sizes,
              'paused_cpu_ram_stable': True, 'mutated_cpu_ram_restored': True,
              'invalid_state_preserved': True, 'empty_state_safe': True,
              'pcm_device_reused': True, 'pause_menu_exit': True,
              'video_frames': machine.blits}
    (out / 'pause-menu-smoke.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
