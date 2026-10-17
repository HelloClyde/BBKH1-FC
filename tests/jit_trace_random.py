"""Independent seeded control-flow differential for actual MIPS JIT traces.

Programs are generated as instruction graphs, independently of emitter layout.
Branches target instruction starts in both directions. CPU budgets stop before,
on and after branches and loop iterations; no opcode body is replaced by a mock.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct

from jit_smoke import Core
from jit_event_smoke import snapshot, put
from make_test_rom import make_rom


def program(rng, pc):
    instructions = [b'\xea']
    branch_positions = sorted(rng.sample(range(1, 12), 3))
    for index in range(1, 13):
        if index in branch_positions:
            instructions.append(bytes((rng.choice((0x10, 0x30, 0x50, 0x70,
                                                  0x90, 0xb0, 0xd0, 0xf0)), 0)))
        else:
            kind = rng.randrange(4)
            if kind == 0:
                instructions.append(bytes((rng.choice((0x18, 0x38, 0xb8, 0xaa,
                                                       0x8a, 0xa8, 0x98, 0xe8,
                                                       0xca, 0xc8, 0x88, 0xea)),)))
            elif kind == 1:
                instructions.append(bytes((rng.choice((0xa9, 0xa2, 0xa0, 0xc9,
                                                       0xe0, 0xc0, 0x29, 0x09,
                                                       0x49, 0x69, 0xe9)), rng.randrange(256))))
            else:
                instructions.append(bytes((rng.choice((0xa5, 0xa6, 0xa4, 0x85,
                                                       0x86, 0x84, 0xe6, 0xc6)), rng.randrange(256))))
    addresses = []
    cursor = pc
    for instruction in instructions:
        addresses.append(cursor)
        cursor += len(instruction)
    addresses.append(cursor)
    instructions.append(bytes((0x4c, pc & 255, pc >> 8)))
    targets = []
    for pos in branch_positions:
        dest = rng.randrange(len(instructions))
        offset = addresses[dest] - addresses[pos] - 2
        assert -128 <= offset <= 127
        instructions[pos] = bytes((instructions[pos][0], offset & 255))
        targets.append([pos, dest])
    return b''.join(instructions), addresses, branch_positions, targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bda', 'elf', 'baseline-bda', 'baseline-elf', 'nm', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--programs', type=int, default=24)
    parser.add_argument('--terminal-blocks', action='store_true',
                        help='Also test candidates intentionally ending blocks at each branch')
    args = parser.parse_args()
    rng = random.Random(0x47400114)
    cases = expanded = native = 0
    counts = {'backward_edges': 0, 'forward_edges': 0, 'self_edges': 0,
              'apu_deadline_cases': 0, 'budget_cases': 0}
    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        old = Core(args.baseline_bda, args.baseline_elf, args.nm, bytes(rom))
        new = Core(args.bda, args.elf, args.nm, bytes(rom))
        for number in range(args.programs):
            # 0x90f0 crosses a CPU 256-byte page, while every graph remains
            # in one immutable 2 KiB PRG page, matching trace admission.
            pc = (0x9000, 0x90f0, 0x9710)[number % 3]
            code, addresses, branch_positions, targets = program(rng, pc)
            for source, dest in targets:
                counts['backward_edges' if dest < source else
                       'forward_edges' if dest > source else 'self_edges'] += 1
            for core in (old, new):
                core.prg(pc, code)
            new.call(new.s['h1_nes_jit_invalidate'])
            page = struct.unpack('<I', new.m.read(new.s['Page'] + (pc >> 11) * 4, 4))[0]
            new.call(new.s['h1_nes_jit_compile'], pc, code[0], page, 1)
            table = struct.unpack('<I', new.m.read(new.s['h1_jit_entries'], 4))[0]
            entry = new.m.read(table + pc * 16, 16)
            assert struct.unpack_from('<I', entry)[0], ('missing entry', number)
            if entry[14] > branch_positions[0] + 1:
                expanded += 1
            ram = bytes(rng.randrange(256) for _ in range(2048))
            budgets = (1, 6, 7, 10, 11, 12, 13, 16, 19, 24, 25, 31,
                       48, 49, 75, 100, 200, 499)
            for repetition, budget in enumerate(budgets):
                regs = [rng.randrange(256) for _ in range(6)]
                regs[4] = (0, 0xff, 0x81, 0x02, 0x40, 0x04)[repetition % 6]
                regs[5] = regs[4]
                frame = (1, 95, 96, 97, 1000000)[repetition % 5]
                carry = (0, 1, 2)[repetition % 3]
                for core in (old, new):
                    put(core, 'fhcnt', 'h1_apu_fhcnt', frame)
                    put(core, 'DMCacc', 'h1_apu_dmcacc', 1000000)
                    put(core, 'DMCSize', 'h1_apu_dmcsize', 0)
                    put(core, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
                    if mapper:
                        put(core, 'IRQa', 'h1_namco_irq_enabled', 1, 1)
                        put(core, 'IRQCount', 'h1_namco_irq_count',
                            (0x7ffc, 0x7ffe, 0xffff, 0)[repetition % 4], 2)
                label = (mapper, number, budget, frame, carry, code.hex(), targets)
                first = old.run(pc, code, regs, ram, budget, tcount=carry)
                second = new.run(pc, code, regs, ram, budget, tcount=carry)
                assert first == second, (label, first[:28].hex(), second[:28].hex())
                left, right = snapshot(old), snapshot(new)
                assert left == right, (label, 'state', [(i, a, b) for i, (a, b)
                    in enumerate(zip(left, right)) if a != b][:20])
                counts['apu_deadline_cases' if frame < 1000000 else 'budget_cases'] += 1
                cases += 1
            print(json.dumps({'mapper': mapper, 'program': number + 1,
                              'cases': cases, 'expanded': expanded}), flush=True)
        native += new.native
        old.close()
        new.close()
    if not args.terminal_blocks:
        assert expanded > 0, 'Candidate never compiled across the first branch'
    assert native > 0, 'Candidate never executed generated MIPS'
    report = {'ok': True, 'seed': '0x47400114', 'cases': cases,
              'programs': args.programs * 2, 'expanded_trace_entries': expanded,
              'native_block_entries': native, **counts,
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'baseline_sha256': hashlib.sha256(args.baseline_bda.read_bytes()).hexdigest(),
              'checks': ['actual MIPS CPU/RAM/time and complete serialized-state equality',
                         'all eight branch conditions with seeded flag/register states',
                         'backward, forward and self edges at instruction starts',
                         'CPU page-crossing branches and short budget overshoot',
                         'APU frame deadline and Namco IRQ wrap/threshold',
                         'generated MIPS execution independently asserted'],
              'requires_trace_expansion': not args.terminal_blocks}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
