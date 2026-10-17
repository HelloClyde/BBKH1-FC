"""Compare the native return boundary before the C dispatcher restores X.PC.

The reference is the previous event-run JIT, which stores PC/PI per instruction.
Interpreter differential tests independently validate the reference semantics.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
from jit_smoke import Core, ROOT
from jit_event_smoke import put, snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    p.add_argument('--baseline-bda', type=Path, required=True)
    p.add_argument('--baseline-elf', type=Path, required=True)
    p.add_argument('--nm', type=Path, required=True)
    p.add_argument('--output', type=Path, default=ROOT/'build/verification/jit-run-state.json')
    a = p.parse_args()
    old = Core(a.baseline_bda, a.baseline_elf, a.nm)
    new = Core(a.bda, a.elf, a.nm)
    rng = random.Random(65025)
    cases = 0
    # Every fixed-cycle family admitted into a run, including read-modify-write,
    # CPU register transfers, carry/overflow flags and operand-vs-opcode DB.
    programs = (
        'a9 80 aa e8 85 10 58 78 ea',
        'ad 00 10 09 01 8d ff 07 e6 12 ce ff 07 a2 04 ea',
        'a9 ff 38 69 01 2a 6a e9 80 24 10 2c ff 07',
        'a2 ff a0 80 b5 01 96 ff b4 01 f6 ff d6 00',
        '18 f8 58 38 b8 78 d8 ea',
        'a9 80 aa a8 9a ba 8a 98 c8 ca 88',
    )
    for program in programs:
        code = bytes.fromhex(program) + b'\x60'
        for count in (-1, 0, 1, 96, 97, 192, 193, 384, 385, 768, 769, 10000):
            for carry in (0, 2):
                regs = bytes(rng.randrange(256) for _ in range(6))
                ram = bytes(rng.randrange(256) for _ in range(2048))
                results = []
                for c in (old, new):
                    c.prg(0x9000, code)
                    page = struct.unpack('<I', c.m.read(c.s['Page']+18*4, 4))[0]
                    c.call(c.s['h1_nes_jit_compile'], 0x9000, code[0], page, 1)
                    table = struct.unpack('<I', c.m.read(c.s['h1_jit_entries'], 4))[0]
                    entry = struct.unpack('<I', c.m.read(table+0x9000*16, 4))[0]
                    assert entry
                    cpu = bytearray(28)
                    struct.pack_into('<I', cpu, 0, carry)
                    struct.pack_into('<H', cpu, 4, 0x9000)
                    cpu[6:12] = regs
                    struct.pack_into('<i', cpu, 16, count)
                    cpu[24] = 0x5a
                    c.m.write(c.s['X'], bytes(cpu)); c.m.write(c.s['RAM'], ram)
                    c.m.word(c.s['timestamp'], 1000); c.m.word(c.s['sound_timestamp'], 1000)
                    put(c, 'fhcnt', 'h1_apu_fhcnt', 1000000)
                    put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)
                    put(c, 'DMCSize', 'h1_apu_dmcsize', 0)
                    result = c.call(entry, c.s['X'], c.s['RAM'])
                    results.append((result, c.m.read(c.s['X'], 28),
                                    c.m.read(c.s['RAM'], 2048), snapshot(c)))
                assert results[0] == results[1], (program, count, carry)
                cases += 1
    old.close(); new.close()
    report = {'ok': True, 'cases': cases,
              'baseline_bda_sha256': hashlib.sha256(a.baseline_bda.read_bytes()).hexdigest(),
              'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'checks': ['native return packet and complete CPU/RAM/serialized state',
                         'X.PC and previous P before dispatcher restoration',
                         'negative/zero budgets, exact boundaries and partial run exits',
                         'all admitted pure operand families; incoming cycle carry']}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
