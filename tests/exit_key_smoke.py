"""Verify game B keys, pause-menu returns, binding capture and config migration.

Runs the actual release MIPS BDA against mocked H1 service tables. The original
homebrew ROM reads the controller's serial B bit and records it in battery RAM;
no core state, guest instruction or running ROM is patched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from h1_mock import Machine, ROOT
from make_test_rom import make_rom
from mips_smoke import save_data
from pause_menu_smoke import symbol

LOG = 'A:\\FC\\h1fc.log'
ROM = 'A:\\FC\\EXIT-KEYS.nes'


def tap(key):
    return [(9, key), (10, key)]


def config(rom, version, mapping, volume=55):
    payload = struct.pack('<5I', 0x31474643, version, 2, 1, 0) + bytes(mapping) + bytes([volume, 0, 0])
    header = struct.pack('<6I', 0x31534748, 1, zlib.crc32(rom) ^ len(rom), 1,
                         len(payload), zlib.crc32(payload))
    return header + struct.pack('<I', zlib.crc32(header)) + payload


def saved_config(machine):
    slots = [machine.files[p] for p in (ROM + '.cfg.s0', ROM + '.cfg.s1') if p in machine.files]
    slot = max(slots, key=lambda value: struct.unpack_from('<I', value, 12)[0])
    assert len(slot) == 96 and zlib.crc32(slot[:24]) == struct.unpack_from('<I', slot, 24)[0]
    payload = slot[28:]
    assert zlib.crc32(payload) == struct.unpack_from('<I', slot, 20)[0]
    return payload


class GameKeys(Machine):
    def __init__(self, bda, map_path):
        super().__init__(bda, path=ROM, files={ROM: make_rom(button='b')})
        self.stage = 'intro'
        self.key_index = 0
        self.at_frame = 0
        self.actions = []
        self.menu_count = 0
        self.battery_address = None
        self.page_table = symbol('Page', map_path)
        self.frozen = None
        self.observed = []

    def button_value(self):
        if self.battery_address is None:
            # The frontend's save buffer is a checkpoint snapshot. Read the
            # actual NROM cartridge RAM through FCEUmm's live 2 KiB CPU map.
            base = struct.unpack('<I', self.read(self.page_table + (0x6000 >> 11) * 4, 4))[0]
            self.battery_address = (base + 0x6000) & 0xffffffff
        return self.read(self.battery_address + 1, 1)[0]

    def event(self, code, key, *args):
        log = self.files.get(LOG, b'')
        paused = log.rfind(b'PAUSE_MENU_BEGIN') > log.rfind(b'PAUSE_MENU_END')
        event = (-1, -1)
        if paused:
            assert self.stage in ('opening', 'menu'), 'Game B key opened a menu'
            assert not self.audio_active and not self.audio_queue
            if self.stage == 'opening':
                self.frozen = self.read(self.battery_address, 8192)
                self.stage = 'menu'
            assert self.read(self.battery_address, 8192) == self.frozen
            assert self.actions, 'Menu action was not accepted'
            event = self.actions.pop(0)
        elif self.stage == 'menu':
            assert not self.actions, 'Holding P closed the menu'
            if self.menu_count == 2:
                assert b'PAUSE_MENU_END action=2' in log
                self.stage = 'exiting'
            else:
                self.stage = 'return-held'
                self.at_frame = self.blits
        elif self.stage == 'intro' and self.blits >= 8:
            self.stage = 'pressed'
            self.at_frame = self.blits
            event = (9, 24)
        elif self.stage in ('pressed', 'repeat') and self.blits >= self.at_frame + 4:
            assert b'PAUSE_MENU_BEGIN' not in log and self.button_value() == 0xA5
            self.observed.append((24 if not self.key_index else 41, self.stage, 0xA5))
            self.at_frame = self.blits
            if self.stage == 'pressed':
                self.stage = 'repeat'
                event = (9, 24 if not self.key_index else 41)
            else:
                self.stage = 'released'
                event = (10, 24 if not self.key_index else 41)
        elif self.stage == 'released' and self.blits >= self.at_frame + 4:
            assert b'PAUSE_MENU_BEGIN' not in log and self.button_value() == 0x11
            self.observed.append((24 if not self.key_index else 41, 'released', 0x11))
            self.key_index += 1
            self.at_frame = self.blits
            if self.key_index == 1:
                self.stage = 'pressed'
                event = (9, 41)
            else:
                self.stage = 'start-menu'
        elif self.stage == 'start-menu':
            self.stage = 'opening'
            event = (9, 31)
            if self.menu_count < 2:
                # Hold/repeat P, release it, then return with Escape or Back.
                self.actions = [(9, 31)] * 4 + [(10, 31), (9, (24, 41)[self.menu_count])]
            else:
                self.actions = [(10, 31)] + tap(37) + [(9, 39)]
        elif self.stage == 'return-held' and self.blits >= self.at_frame + 4:
            # The menu's still-held return key must not leak a B press into
            # the game or reopen the menu while waiting for physical release.
            assert self.button_value() == 0x11
            event = (10, (24, 41)[self.menu_count])
            self.menu_count += 1
            self.stage = 'return-released'
            self.at_frame = self.blits
        elif self.stage == 'return-released' and self.blits >= self.at_frame + 2:
            self.stage = 'start-menu'
        self.word(code, event[0])
        self.word(key, event[1])
        return 0


class Capture(Machine):
    def __init__(self, bda):
        super().__init__(bda, path=ROM, menu=True)
        # Main Right -> bindings; Down twice -> A -> capture Escape.
        # Right -> B -> capture Back. Right -> START -> P cancels capture.
        # Back -> main cursor 0; Up -> 7, Confirm -> Exit.
        self.exit_menu_actions = ([(10, 31)] + tap(35) + tap(39) + tap(27) * 2 + tap(39) + tap(24)
                                  + tap(35) + tap(39) + tap(41) + tap(35) + tap(39) + tap(31)
                                  + tap(41) + tap(37) + [(9, 39)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, default=ROOT / 'dist/H1FC-jit.bda')
    parser.add_argument('--map', type=Path, default=ROOT / 'build/release-jit/H1FC.map')
    args = parser.parse_args()
    output = ROOT / 'build/verification'
    output.mkdir(parents=True, exist_ok=True)
    cases = {}

    def run(name, machine):
        print('RUN:', name, flush=True)
        try:
            seconds = machine.run()
        finally:
            (output / ('exit-key-' + name + '.log')).write_bytes(machine.files.get(LOG, b''))
        cases[name] = {'seconds': round(seconds, 2), 'frames': machine.blits}
        return machine

    keys = run('game-and-menu', GameKeys(args.bda, args.map))
    assert keys.key_index == 2 and len(keys.observed) == 6 and keys.menu_count == 2
    assert keys.files[LOG].count(b'PAUSE_MENU_BEGIN') == 3
    assert keys.files[LOG].count(b'PAUSE_MENU_END action=0') == 2
    assert keys.files[LOG].count(b'PAUSE_MENU_END action=2') == 1
    assert b'SAVE_EXIT_END status=1' in keys.files[LOG] and b'APP_END result=0' in keys.files[LOG]
    assert save_data(keys, ROM)[:2] == b'\x5a\x11'
    cases['game-and-menu']['rom_b_observations'] = keys.observed

    # Legacy configs deliberately contain nondefault mappings and display /
    # sound options. Migration must add only the two formerly reserved keys.
    rom = make_rom()
    mapping = bytearray(45)
    for key, value in {1: 5, 16: 9, 17: 1, 25: 4, 23: 3, 27: 6, 35: 8, 37: 5, 40: 7}.items():
        mapping[key] = value
    for version in (1, 2):
        files = {ROM: rom, ROM + '.cfg.s0': config(rom, version, mapping)}
        migrated = run(f'v{version}-migration', Machine(args.bda, path=ROM, files=files, menu=True))
        payload = saved_config(migrated)
        expected = bytearray(mapping)
        expected[24] = expected[41] = 1
        assert struct.unpack_from('<5I', payload) == (0x31474643, 3, 2, 1, 0)
        assert payload[20:65] == expected and payload[65] == (100 if version == 1 else 55)
        if version == 2:
            reloaded = run('v3-migration-reload', Machine(args.bda, path=ROM, files=migrated.files, menu=True))
            assert saved_config(reloaded) == payload

    custom = bytearray(mapping)
    custom[24], custom[41] = 9, 0
    current = run('v3-custom', Machine(args.bda, path=ROM, menu=True,
                  files={ROM: rom, ROM + '.cfg.s0': config(rom, 3, custom)}))
    assert saved_config(current)[20:65] == custom, 'v3 mapping was reset or migrated again'

    capture = run('capture-and-cancel', Capture(args.bda))
    payload = saved_config(capture)
    assert payload[20 + 24] == 9 and payload[20 + 41] == 1
    assert payload[20 + 16] == payload[20 + 39] == payload[20 + 17] == 0
    assert payload[20 + 25] == 4 and payload[20 + 31] == 0, 'P cancel changed START mapping'
    assert b'MENU_PAGE page=1 cursor=6 capture=6' in capture.files[LOG]
    assert b'MENU_PAGE page=1 cursor=6 capture=-1' in capture.files[LOG]
    assert b'PAUSE_MENU_END action=2' in capture.files[LOG]

    report = {'ok': True, 'kind': 'Actual MIPS BDA with mocked H1 service tables',
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(), 'cases': cases,
              'assertions': ['Escape and Back deliver held/repeated/released B to the original ROM',
                             'Game Escape and Back never pause or exit',
                             'P opens pause and ignores held-key repeats',
                             'Escape and Back return from menus without leaking B',
                             'Explicit Exit saves and releases all resources',
                             'Legacy v1/v2 config migration preserves other mappings and options',
                             'v3 reload preserves custom Escape/Back mappings',
                             'Capture accepts Escape and Back; P cancels without binding'],
              'limitation': 'Mocked services do not establish physical H1 timing or audio quality.'}
    (output / 'exit-key-smoke.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
