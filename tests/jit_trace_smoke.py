"""Execute conditional control-flow boundaries against the MIPS interpreter.

The same cases cover bounded trace experiments and production branch-tail
regions. No emulator code is mocked or patched. Only cartridge/initial
machine state are test-owned; budget, mapper and audio callbacks are real.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_smoke import Core, ROOT
from jit_event_smoke import put, snapshot
from make_test_rom import make_rom


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-bda', 'baseline-elf', 'bda', 'elf', 'nm', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    counts = {'branch_budget': 0, 'events': 0, 'mutation': 0, 'long_trace': 0}
    retired = 0
    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        old = Core(a.baseline_bda, a.baseline_elf, a.nm, bytes(rom))
        new = Core(a.bda, a.elf, a.nm, bytes(rom))
        cores = old, new
        ram = bytes((i * 11 + 7) & 255 for i in range(2048))
        for c in cores:
            c.prg(0x9000, b'\xea' * 2048)
            c.prg(0x9800, b'\xea' * 256)
            c.prg(0x9a00, bytes.fromhex('ea 4c 00 9a'))
            c.prg(0xfffa, struct.pack('<3H', 0x9a00, 0x9a00, 0x9a00))

        def run(code, pc, budget, status=4, pal=0, frame=1000000,
                dmc=1000000, dma=0, irq=0, carry=0, mutate=None,
                category='branch_budget', overclock=0):
            nonlocal retired
            values = []
            before = struct.unpack('<8I', new.m.read(new.s['h1_jit_stats'], 32))[3]
            for c in cores:
                c.prg(pc, code + b'\xea' * 48)
                if c is new:
                    page = struct.unpack('<I', c.m.read(c.s['Page'] + (pc >> 11) * 4, 4))[0]
                    c.call(c.s['h1_nes_jit_compile'], pc, code[0], page, 1)
                    table = struct.unpack('<I', c.m.read(c.s['h1_jit_entries'], 4))[0]
                    assert struct.unpack('<I', c.m.read(table + pc * 16, 4))[0], (hex(pc), code.hex())
                if mutate:
                    c.prg(pc + mutate[0], bytes([mutate[1]]))
                put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
                put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
                put(c, 'DMCSize', 'h1_apu_dmcsize', dma)
                put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
                if mapper == 19:
                    put(c, 'IRQa', 'h1_namco_irq_enabled', int(irq != 0), 1)
                    put(c, 'IRQCount', 'h1_namco_irq_count', irq, 2)
                c.m.write(c.s['PAL'], bytes([pal]))
                c.m.word(c.s['overclocked'], overclock)
                c.m.word(c.s['timestamp'], 0)
                c.m.word(c.s['sound_timestamp'], 0)
                cpu = bytearray(28)
                struct.pack_into('<I', cpu, 0, carry)
                struct.pack_into('<H', cpu, 4, pc)
                cpu[6:12] = bytes([35, 254, 128, 240, status, status])
                struct.pack_into('<i', cpu, 16, budget - (15 if pal else 16))
                c.m.write(c.s['X'], bytes(cpu))
                c.m.write(c.s['RAM'], ram)
                c.call(c.s['X6502_Run'], 1)
                values.append(c.m.read(c.s['X'], 28) + c.m.read(c.s['RAM'], 2048) +
                              c.m.read(c.s['timestamp'], 4) + c.m.read(c.s['sound_timestamp'], 4))
            label = (mapper, pc, code.hex(), budget, status, pal, frame, dmc, dma, irq, carry, mutate)
            assert values[0] == values[1], (label, values[0][:28].hex(), values[1][:28].hex())
            assert snapshot(old) == snapshot(new), (label, 'serialized state')
            after = struct.unpack('<8I', new.m.read(new.s['h1_jit_stats'], 32))[3]
            retired += (after - before) & 0xffffffff
            counts[category] += 1

        # Both polarities of all eight conditions, plus fall-through and taken
        # paths at each possible CPU/branch-extra-cycle boundary.
        for op in (0x10, 0x30, 0x50, 0x70, 0x90, 0xb0, 0xd0, 0xf0):
            code = bytes([0xea, op, 4, 0xa9, 0x12, 0x85, 0x10, 0xe8,
                          0xea, 0x10, 0, 0xea, 0x4c, 0, 0x9a])
            for status in (4, 0xc7):
                for budget in (1, 96, 97, 191, 192, 193, 239, 240, 241,
                               287, 288, 289, 479, 480, 481, 1200):
                    run(code, 0x90fc, budget, status)
        programs = (
            (0x9200, bytes.fromhex('ea e8 d0 fc a9 42 ea 10 00 ea 4c 00 9a')),
            (0x9200, bytes.fromhex('ea e8 d0 fd a9 42 ea 10 00 ea 4c 00 9a')),
            (0x92fc, bytes.fromhex('ea d0 04 a9 12 85 10 e8 ea 10 00 ea 4c 00 9a')),
            (0x97f9, bytes.fromhex('ea d0 04 a9 12 85 10 e8 ea 10 00 ea 4c 00 9a')),
            (0x9400, bytes.fromhex('ea d0 03 8d 05 20 e8 ea 10 00 ea 4c 00 9a')),
        )
        for pc, code in programs:
            for status in (4, 6):
                for pal in (0, 1):
                    for budget in (97, 191, 192, 193, 240, 241, 287, 288, 289, 768):
                        run(code, pc, budget, status, pal)
        event_code = bytes.fromhex('ea d0 04 a9 12 85 10 e8 ea 10 00 ea 4c 00 9a')
        for status in (4, 6):
            for frame in (95, 96, 97, 191, 192, 193, 240, 241, 1000000):
                for dmc in (1, 2, 3, 4, 5, 1000000):
                    for dma in (0, 1):
                        run(event_code, 0x94fc, 769, status, frame=frame,
                            dmc=dmc, dma=dma, carry=2, category='events')
            if mapper == 19:
                for irq in (1, 0x7ffc, 0x7ffd, 0x7ffe, 0x7fff, 0xffff):
                    run(event_code, 0x94fc, 769, status, irq=irq, category='events')
        # Identical first opcode/page, but changed branch displacement, later
        # opcode, or later immediate: public entry must retire stale code.
        for offset, value in ((2, 0), (3, 0xa2), (4, 0x81), (8, 0x18)):
            for status in (4, 6):
                run(event_code, 0x9600, 769, status, mutate=(offset, value), category='mutation')
        # One trace loops past the 15-bit retirement packet limit. Audio is
        # disabled only for this artificial oversized CPU call, as in the
        # existing native-chain overflow regression.
        run(bytes.fromhex('ea e8 d0 fc ea 4c 00 90'), 0x9000, 400000 * 16,
            overclock=1, category='long_trace')
        for c in cores:
            c.m.word(c.s['overclocked'], 0)
            c.close()
    assert retired > 0
    report = {'ok': True, 'cases': sum(counts.values()), 'counts': counts,
              'native_retired': retired, 'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'checks': ['all branch conditions and both directions',
                         'nonterminal fall-through, internal back edges and forward side exits',
                         'CPU page/PRG page boundaries and PAL/NTSC budgets',
                         'PPU write skipped/taken paths, exact DB/PI/PC and cycle overshoot',
                         'APU/DMC/DMA/Namco IRQ event boundaries and carry',
                         'exact CPU/RAM/timestamps and serialized state',
                         'same-page later byte mutation and native packet overflow']}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
