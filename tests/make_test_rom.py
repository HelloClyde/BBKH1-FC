"""Build an original, redistributable NES NROM diagnostic cartridge.

No external ROM, assembler, or copyrighted game data is needed. The cartridge
draws repeating dark/blue/red/green tiles, changes blue to white while A is
held, and plays a pulse tone. Battery RAM records observable test results:
6000=5A, 6001=A5 (A held) / 11 (released), 6002=frame counter,
6003=42 (program initialized), 6004=1E (rendering enabled),
6005=C0 (previous 5A loaded from battery RAM) / 00 (fresh cartridge).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import struct


def make_rom(*, battery: bool = True, tone: bool = True) -> bytes:
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str, bool]] = []

    def emit(*values: int) -> None:
        code.extend(values)

    def label(name: str) -> None:
        labels[name] = 0x8000 + len(code)

    def branch(op: int, name: str) -> None:
        emit(op, 0)
        fixups.append((len(code) - 1, name, True))

    def jump(name: str) -> None:
        emit(0x4C, 0, 0)
        fixups.append((len(code) - 2, name, False))

    def store(address: int, value: int) -> None:
        emit(0xA9, value, 0x8D, address & 255, address >> 8)

    def wait_vblank(name: str) -> None:
        label(name)
        emit(0x2C, 0x02, 0x20)  # BIT PPUSTATUS; clears vblank after observing it
        branch(0x10, name)       # BPL

    label('reset')
    emit(0x78, 0xD8, 0xA2, 0xFF, 0x9A)  # SEI, CLD, LDX #FF, TXS
    store(0x2000, 0)
    store(0x2001, 0)
    store(0x4010, 0)
    store(0x4015, 0)
    store(0x4017, 0x40)  # Disable frame-counter IRQ
    wait_vblank('warmup1')
    wait_vblank('warmup2')

    emit(0xAD, 0x00, 0x60, 0xC9, 0x5A)  # LDA $6000; CMP #5A
    branch(0xD0, 'fresh')
    emit(0xA9, 0xC0)
    jump('restore_flag')
    label('fresh')
    emit(0xA9, 0)
    label('restore_flag')
    emit(0x8D, 0x05, 0x60)
    for address, value in [(0x6000, 0x5A), (0x6001, 0x11), (0x6002, 0),
                           (0x6003, 0x42), (0x6004, 0)]:
        store(address, value)

    # Set all 960 nametable cells plus 64 attributes while rendering is off.
    # A tile's color is its low two bits; attributes are reset separately.
    store(0x2006, 0x20)
    store(0x2006, 0)
    emit(0xA2, 0, 0xA0, 4)  # LDX #0; LDY #4
    label('nametable')
    emit(0x8A, 0x29, 3, 0x8D, 0x07, 0x20, 0xE8)  # TXA; AND #3; STA; INX
    branch(0xD0, 'nametable')
    emit(0x88)  # DEY
    branch(0xD0, 'nametable')
    store(0x2006, 0x23)
    store(0x2006, 0xC0)
    emit(0xA2, 64, 0xA9, 0)
    label('attributes')
    emit(0x8D, 0x07, 0x20, 0xCA)
    branch(0xD0, 'attributes')
    store(0x2006, 0x3F)
    store(0x2006, 0)
    for _ in range(8):
        for color in (0x0F, 0x21, 0x16, 0x2A):
            store(0x2007, color)

    if tone:
        # 50% pulse, constant volume, halted length counter, approximately 440Hz.
        for address, value in [(0x4015, 1), (0x4000, 0xBF), (0x4001, 0),
                               (0x4002, 0xFD), (0x4003, 0x08)]:
            store(address, value)
    store(0x2005, 0)
    store(0x2005, 0)
    store(0x2000, 0)
    store(0x2001, 0x1E)
    store(0x6004, 0x1E)

    wait_vblank('frame')
    store(0x4016, 1)
    store(0x4016, 0)
    emit(0xAD, 0x16, 0x40, 0x29, 1)  # LDA JOY1; AND #1 (A)
    branch(0xF0, 'released')
    store(0x6001, 0xA5)
    emit(0xA2, 0x30)  # white
    jump('palette')
    label('released')
    store(0x6001, 0x11)
    emit(0xA2, 0x21)  # blue
    label('palette')
    store(0x2006, 0x3F)
    store(0x2006, 1)
    emit(0x8E, 0x07, 0x20)  # STX PPUDATA
    store(0x2005, 0)
    store(0x2005, 0)
    store(0x2000, 0)
    emit(0xEE, 0x02, 0x60)  # INC $6002
    jump('frame')
    label('interrupt')
    emit(0x40)  # RTI (NMI and maskable IRQ are disabled)

    for offset, target, relative in fixups:
        address = labels[target]
        if relative:
            displacement = address - (0x8000 + offset + 1)
            assert -128 <= displacement <= 127, (target, displacement)
            code[offset] = displacement & 255
        else:
            code[offset:offset + 2] = struct.pack('<H', address)
    assert len(code) < 0x3FFA
    prg = bytearray([0xEA]) * 16384
    prg[:len(code)] = code
    prg[0x3FFA:] = struct.pack('<3H', labels['interrupt'], labels['reset'], labels['interrupt'])
    chr_data = bytearray(8192)
    for color in range(4):
        chr_data[color * 16:color * 16 + 16] = bytes(
            [255 if color & 1 else 0] * 8 + [255 if color & 2 else 0] * 8)
    header = b'NES\x1A' + bytes([1, 1, 2 if battery else 0, 0, 1, 0, 0]) + bytes(5)
    assert len(header) == 16
    return header + prg + chr_data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--no-battery', action='store_true')
    parser.add_argument('--silent', action='store_true')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(make_rom(battery=not args.no_battery, tone=not args.silent))
    print(args.output)
