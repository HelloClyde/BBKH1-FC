"""Execute the actual H1FC MIPS BDA using mocked H1 firmware service tables.

Requires Python, unicorn and Pillow. ROMs and H1 files are generated in memory;
the test never mounts or writes an H1 device. Use a --test-frames 20 build, or
pass --menu for a release build (P opens the menu, then Exit is selected).
This verifies machine code and integration, not physical H1 timing or sound.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from PIL import Image
from h1_mock import Machine, ROOT
from make_test_rom import make_rom

LOG = 'A:\\FC\\h1fc.log'
ROM = 'A:\\FC\\H1TEST.nes'


def save_data(machine: Machine, path: str) -> bytes:
    slots = [value for name, value in machine.files.items()
             if name in (path + '.s0', path + '.s1')]
    assert slots, 'Battery RAM was not persisted'
    slot = max(slots, key=lambda value: struct.unpack_from('<I', value, 12)[0])
    assert len(slot) == 28 + 8192, len(slot)
    assert zlib.crc32(slot[:24]) == struct.unpack_from('<I', slot, 24)[0], 'header CRC'
    assert zlib.crc32(slot[28:]) == struct.unpack_from('<I', slot, 20)[0], 'payload CRC'
    return slot[28:]


def check_screen(machine: Machine, pressed: bool, destination: Path) -> list[int]:
    pixels = struct.unpack('<130560H', machine.gameplay_screen or machine.screen)
    image = Image.new('RGB', (480, 272))
    image.putdata([((p >> 11) * 255 // 31, ((p >> 5) & 63) * 255 // 63,
                    (p & 31) * 255 // 31) for p in pixels])
    image.save(destination)
    # Default aspect mode scales a full 256x240 NES picture to 290x272.
    width, left = 256 * 272 // 240, (480 - (256 * 272 // 240)) // 2
    row = pixels[136 * 480:137 * 480]
    assert set(row[:left]) == {0} and set(row[left + width:]) == {0}, 'side borders'
    colors = [row[left + source_x * width // 256] for source_x in (4, 12, 20, 28)]
    assert len(set(colors)) == 4, [hex(value) for value in colors]
    for block in range(1, 8):
        repeated = [row[left + (source_x + block * 32) * width // 256]
                    for source_x in (4, 12, 20, 28)]
        assert repeated == colors, (block, repeated, colors)
    rgb = [((p >> 11) * 255 // 31, ((p >> 5) & 63) * 255 // 63,
            (p & 31) * 255 // 31) for p in colors]
    assert max(rgb[0]) <= 24, ('dark background stripe', rgb)
    assert rgb[2][0] > max(rgb[2][1:]), ('red stripe', rgb)
    assert rgb[3][1] > max(rgb[3][0], rgb[3][2]), ('green stripe', rgb)
    if pressed:
        assert min(rgb[1]) > 200, ('A should make blue stripe white', rgb)
    else:
        assert rgb[1][2] > rgb[1][0] and rgb[1][2] > rgb[1][1], ('blue stripe', rgb)
    return colors


def check_audio(machine: Machine) -> dict:
    assert machine.audio_submissions, 'No native PCM buffers submitted'
    pcm = b''.join(machine.audio_submissions)
    samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
    assert len(set(samples)) > 8 and max(samples) - min(samples) > 500, 'Pulse tone absent'
    return {'submissions': len(machine.audio_submissions), 'samples': len(samples),
            'peak_to_peak': max(samples) - min(samples)}


class RejectedROM(Machine):
    """Cancel the next selector after the expected invalid-ROM error dialog."""
    def select_file(self, *args):
        if self.select_calls:
            self.rom_path = None
        return super().select_file(*args)

    def message(self, parent, text, title, flags, *args):
        self.rejection_messages.append(self.string(text))
        return 0

    def __init__(self, *args, **kwargs):
        self.rejection_messages = []
        super().__init__(*args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, default=ROOT / 'build/test-20/H1FC.bda')
    parser.add_argument('--menu', action='store_true', help='Exit release builds through the pause menu')
    parser.add_argument('--quick', action='store_true', help='Only graphics, keys, audio, persistence and cancel')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/verification',
                        help='Keep reports for different BDA builds in separate directories')
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    cases: dict = {}

    def run(name: str, machine: Machine) -> None:
        print('RUN:', name, flush=True)
        try:
            duration = machine.run()
        finally:
            (out / (name + '.log')).write_bytes(machine.files.get(LOG, b''))
        cases[name] = {'seconds': round(duration, 2), 'frames': machine.blits}

    first = Machine(args.bda, press_a=True, menu=args.menu)
    run('pressed', first)
    data = save_data(first, ROM)
    # The pending pause suppresses gameplay input for its final core frame;
    # the pre-menu white stripe below still verifies that held A was sampled.
    expected_button = 0x11 if args.menu else 0xA5
    assert data[0:2] == bytes([0x5A, expected_button]) and data[3:6] == b'\x42\x1e\0', data[:8].hex()
    assert data[2] >= 4, ('6502 did not complete enough frames', data[:8].hex())
    cases['pressed']['colors'] = check_screen(first, True, out / 'fc-pressed.png')
    cases['pressed']['audio'] = check_audio(first)
    assert first.select_calls[0] == ('A:\\FC\\', 'nes'), first.select_calls

    second = Machine(args.bda, files=first.files, menu=args.menu)
    run('restored', second)
    restored = save_data(second, ROM)
    assert restored[0:2] == b'\x5a\x11' and restored[5] == 0xC0, restored[:8].hex()
    cases['restored']['colors'] = check_screen(second, False, out / 'fc-released.png')
    assert 'SAVE restored' in second.files[LOG].decode('gbk'), 'Save not restored'

    cancelled = Machine(args.bda, path=None)
    run('selector-cancel', cancelled)
    assert cancelled.blits == 0 and len(cancelled.select_calls) == 1
    assert not any(name.endswith(('.s0', '.s1')) for name in cancelled.files)
    assert b'APP_END result=0' in cancelled.files[LOG]
    assert not cancelled.menu_frames and b'CORE_INIT_BEGIN' not in cancelled.files[LOG]

    if args.menu:
        for machine in (first, second):
            assert b'PAUSE_MENU_BEGIN' in machine.files[LOG]
            assert b'PAUSE_MENU_END action=2' in machine.files[LOG]
            assert b'SAVE_EXIT_END status=1' in machine.files[LOG]

    if not args.quick:
        path = 'A:\\游戏\\原创测试.nes'
        chinese = Machine(args.bda, path=path, menu=args.menu, switch=args.menu)
        run('chinese-path', chinese)
        assert save_data(chinese, path)[0] == 0x5A
        check_screen(chinese, False, out / 'fc-chinese-path.png')
        if args.menu:
            assert chinese.select_calls[1] == ('A:\\游戏\\', 'nes'), chinese.select_calls

        path = 'A:\\FC\\NOBATT.nes'
        no_battery = Machine(args.bda, path=path, files={path: make_rom(battery=False)}, menu=args.menu)
        run('no-battery', no_battery)
        assert not any(name.endswith(('.s0', '.s1')) for name in no_battery.files)
        check_screen(no_battery, False, out / 'fc-no-battery.png')

        # Preserve both CRC-protected slots if the same path names another ROM,
        # including a cartridge whose first 512 bytes are completely identical.
        changed_rom = bytearray(make_rom())
        changed_rom[16 + 0x3000] ^= 1  # Unexecuted PRG padding, well after header/code.
        assert changed_rom[:512] == make_rom()[:512]
        for name, files in [('changed-cartridge', {**second.files, ROM: bytes(changed_rom)}),
                            ('corrupt-save', dict(second.files))]:
            if name == 'corrupt-save':
                for filename, content in list(files.items()):
                    if filename in (ROM + '.s0', ROM + '.s1'):
                        damaged = bytearray(content)
                        damaged[100] ^= 1
                        files[filename] = bytes(damaged)
            protected = {filename: content for filename, content in files.items()
                         if filename.endswith(('.s0', '.s1'))}
            rejected = RejectedROM(args.bda, files=files, menu=args.menu)
            run(name, rejected)
            assert rejected.rejection_messages, 'Incompatible/damaged save was not reported'
            assert b'SAVE_LOAD_END status=-1' in rejected.files[LOG]
            assert {filename: content for filename, content in rejected.files.items()
                    if filename.endswith(('.s0', '.s1'))} == protected, 'Existing save was overwritten'
            cases[name]['existing_slots_preserved'] = True

        huge = bytearray(make_rom())
        huge[4], huge[7], huge[9] = 0xFC, 8, 15  # NES 2.0 exponent size, 2^63 bytes.
        unsupported = bytearray(make_rom())
        unsupported[6:9] = b'\xf2\xf8\x0f'  # NES 2.0 mapper 4095.
        malformed = [('bad-magic', bytes(512)), ('short-header', b'NES\x1a'),
                     ('truncated', make_rom()[:100]), ('huge-nes2-size', bytes(huge)),
                     ('unsupported-mapper', bytes(unsupported))]
        for name, content in malformed:
            path = 'A:\\FC\\INVALID.nes'
            invalid = RejectedROM(args.bda, path=path, files={path: content}, menu=args.menu)
            run(name, invalid)
            assert invalid.rejection_messages, 'Malformed cartridge was not rejected'
            assert not any(filename.endswith(('.s0', '.s1')) for filename in invalid.files)
            cases[name]['dialogs'] = invalid.rejection_messages

    report = {'ok': True, 'kind': 'Actual MIPS H1FC BDA with mocked H1 firmware',
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'rom_sha256': hashlib.sha256(make_rom()).hexdigest(),
              'menu': args.menu, 'quick': args.quick, 'cases': cases,
              'assertions': ['MIPS ABI and resource cleanup', '256x240 background and aspect scaling',
                             'A press and release', 'APU pulse PCM', '8 KiB battery save with CRC',
                             'battery RAM restored across launches', 'native selector and cancel'],
              'limitation': 'Does not establish physical H1 frame rate, audio quality, or firmware compatibility.'}
    destination = out / ('menu-smoke.json' if args.menu else 'mips-smoke.json')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
