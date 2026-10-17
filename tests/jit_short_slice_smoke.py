"""Actual MIPS boundary differential for short CPU budgets.

The production wrapper and bus/APU callbacks run unchanged. The diagnostic
owns only initial CPU/register/cartridge state, as the existing JIT tests do.
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-bda', type=Path, required=True)
    parser.add_argument('--baseline-elf', type=Path, required=True)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--cost-aware-admission', action='store_true',
                        help='Assert opcode-cost admission on candidates that implement it')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-short-slices.json')
    args = parser.parse_args()
    cases = 0
    baseline_native = 0
    candidate_native = 0

    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        cores = [Core(bda, elf, args.nm, bytes(rom)) for bda, elf in
                 ((args.baseline_bda, args.baseline_elf), (args.bda, args.elf))]
        cycle_table = cores[1].m.read(cores[1].s['CycTable'], 256)
        regs = [35, 254, 128, 240, 4, 4]
        ram = bytearray(2048)
        ram[0xff:0x101] = b'\xff\x00'
        # RTI/RTS have safe fixed stack targets in this original fixture.
        ram[0x1f1:0x1f4] = b'\x04\x00\x90'

        def run(label, code, budget, pal=0, carry=0, irq=0, frame=1000000,
                dmc=1000000, dma=0, pc=0x90fd, status=4):
            nonlocal cases
            results = []
            before = cores[1].native
            for c in cores:
                c.prg(0x9100, b'\xea' * 128)
                c.prg(0x9200, b'\xea' * 64)
                c.prg(0xfffa, struct.pack('<3H', 0x9200, 0x9200, 0x9200))
                c.prg(pc, code + b'\xea' * 16)
                page = struct.unpack('<I', c.m.read(c.s['Page'] + (pc >> 11) * 4, 4))[0]
                c.call(c.s['h1_nes_jit_compile'], pc, code[0], page, 1)
                put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
                put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
                put(c, 'DMCSize', 'h1_apu_dmcsize', dma)
                put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
                if mapper == 19:
                    put(c, 'IRQa', 'h1_namco_irq_enabled', int(frame != 1000000), 1)
                    put(c, 'IRQCount', 'h1_namco_irq_count', 0x7ffe, 2)
                c.m.write(c.s['PAL'], bytes([pal]))
                c.m.word(c.s['timestamp'], 0)
                c.m.word(c.s['sound_timestamp'], 0)
                state = bytearray(28)
                struct.pack_into('<I', state, 0, carry & 0xffffffff)
                struct.pack_into('<H', state, 4, pc)
                state[6:12] = bytes(regs[:4] + [status, status])
                # One PPU cycle credits 15 or 16 count units. Set the residual
                # explicitly to test each opcode's own admission boundary.
                struct.pack_into('<i', state, 16, budget - (15 if pal else 16))
                struct.pack_into('<I', state, 20, irq)
                c.m.write(c.s['X'], bytes(state))
                c.m.write(c.s['RAM'], bytes(ram))
                c.call(c.s['X6502_Run'], 1)
                results.append(c.m.read(c.s['X'], 28) + c.m.read(c.s['RAM'], 2048) +
                               c.m.read(c.s['timestamp'], 4) + c.m.read(c.s['sound_timestamp'], 4))
            assert results[0] == results[1], (mapper, label, budget, pal, 'CPU/RAM/timestamps')
            assert snapshot(cores[0]) == snapshot(cores[1]), (mapper, label, budget, pal, 'serialized state')
            threshold = cycle_table[code[0]]*48 if args.cost_aware_admission else 96
            if budget <= threshold:
                assert cores[1].native == before, (label, 'short budget entered native')
            if (budget == cycle_table[code[0]]*48+1 and
                    label in ('NOP', 'immediate', 'indexed-page-cross', 'RMW') and
                    not irq and not carry and frame == 1000000 and not dma):
                assert cores[1].native > before, (label, 'eligible budget missed native')
            cases += 1

        programs = {
            'NOP': bytes.fromhex('ea'), 'immediate': bytes.fromhex('a9 42'),
            'branch-page-cross': bytes.fromhex('d0 01'),
            'indexed-page-cross': bytes.fromhex('bd ff 00'),
            'indirect-indexed': bytes.fromhex('b1 ff'),
            'PPU-status': bytes.fromhex('ad 02 20'),
            'PPU-scroll': bytes.fromhex('8d 05 20'),
            'APU-period': bytes.fromhex('8d 02 40'),
            'RMW': bytes.fromhex('ee ff 00'), 'PHP': bytes.fromhex('08'),
            'BRK': bytes.fromhex('00'), 'JSR': bytes.fromhex('20 00 92'),
            'RTS': bytes.fromhex('60'), 'RTI': bytes.fromhex('40'),
            'PHA': bytes.fromhex('48'), 'PLA': bytes.fromhex('68'),
            'CLI': bytes.fromhex('58')}
        for pal in (0, 1):
            for label, code in programs.items():
                cost = cycle_table[code[0]] * 48
                for budget in sorted(set((-96, 0, 1, 16, 48, 95, 96, 97, 112,
                                          cost-1, cost, cost+1, cost+16))):
                    run(label, code, budget, pal)
        for carry in (-3, 0, 2):
            for irq in (0, 1, 0x40, 0x80):
                for budget in (1, 96, 97):
                    for status in (0, 4):
                        run('IRQ/DMC/APU/carry', bytes.fromhex('ea a9 42'), budget,
                            carry=carry, irq=irq, frame=95, dmc=3, dma=1, status=status)
        baseline_native += cores[0].native
        candidate_native += cores[1].native
        for c in cores:
            c.close()
    report = dict(ok=True, cases=cases, baseline_native_blocks=baseline_native,
                  candidate_native_blocks=candidate_native,
                  admission='opcode-base-cost' if args.cost_aware_admission else 'minimum-96-units',
                  bda_sha256=hashlib.sha256(args.bda.read_bytes()).hexdigest(),
                  checks=['empty/nonempty budgets before/at/after each opcode base-cycle cost',
                          'PAL/NTSC exact cycle credit and whole-instruction overshoot',
                          'branch/index page crossing, stack, PPU/APU bus operations',
                          'IRQ/NMI, DMC DMA, APU frame and Namco IRQ deadlines',
                          'previous instruction carry and complete serialized state'])
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
