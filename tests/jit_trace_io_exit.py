"""Actual MIPS trace side exits stay distinct from an executed terminal IO.

The early taken target deliberately equals the IO's sequential successor.
Precompiled successor and nested blocks make accidental chaining observable.
Only fixture ROM and bus-handler trampolines are supplied by this test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_contract_smoke import Pair, clock, entry, stamp, u32
from jit_event_smoke import snapshot


def case(args, mapper, taken, kind):
    p = Pair(args, mapper)
    n = p.new
    pc, target, inner = 0x9000, 0x9006, 0x9400
    # NOP; BNE successor; STA $5000; successor: LDA #$11; STA $10; PHP.
    # PHP terminates compilation, and the selected CPU budget ends before it.
    code = bytes.fromhex('ea d0 03 8d 00 50 a9 11 85 10 08')
    inner_code = bytes.fromhex('a9 33 aa e8 85 20 08')
    calls = {id(c): 0 for c in p.cores}
    nested = {id(c): [] for c in p.cores}
    for c in p.cores:
        saved = []
        def before(*unused, c=c, saved=saved):
            calls[id(c)] += 1
            if c is n:
                assert u32(c, 'h1_jit_contract_epoch') == 0
                assert u32(c, 'h1_jit_contract_dirty') == 1
            if kind == 'nested':
                saved.append(c.m.read(c.s['X'], 28))
                cpu = bytearray(28)
                struct.pack_into('<H', cpu, 4, inner)
                cpu[6:12] = bytes((0x35, 0xfe, 0x80, 0xf0, 0x45, 0x45))
                c.m.write(c.s['X'], bytes(cpu))
            elif kind == 'mutate':
                c.prg(target + 1, b'\x71')
            return 0
        def after(*unused, c=c, saved=saved):
            state = c.m.read(c.s['X'], 28)
            nested[id(c)].append(state + c.m.read(c.s['RAM'], 2048))
            assert state[6:8] == b'\x33\x34', state.hex()
            if c is n:
                assert u32(c, 'h1_jit_contract_epoch') == stamp(c, inner) != 0
            c.m.write(c.s['X'], saved.pop())
            c.prg(target + 1, b'\x71')
            return 0
        c.stub(0x80001b80, before)
        if kind == 'nested':
            c.stub(0x80001ba0, after)
            jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
            words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0,
                     0x34040018, jal(c.s['X6502_Run']), 0,
                     jal(0x80001ba0), 0, 0x8fbf001c,
                     0x03e00008, 0x27bd0020]
            c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
            c.m.word(c.s['BWrite'] + 0x5000 * 4, 0x80001c00)
        else:
            c.m.word(c.s['BWrite'] + 0x5000 * 4, 0x80001b80)
    p.code(pc, code)
    p.compile(target, code[6:])
    p.compile(inner, inner_code)
    p.compile(pc, code)
    assert entry(n, pc)[5] == 3, ('trace must include NOP/branch/IO', entry(n, pc))
    assert entry(n, target)[0]
    before_links = u32(n, 'h1_jit_links')
    values = []
    ram = bytes((i * 37 + 11) & 255 for i in range(2048))
    for c in p.cores:
        clock(c)
        c.call(c.s['h1_nes_jit_frame_begin'])
        values.append(c.run(pc, code, [35, 254, 128, 240, 4 if taken else 6,
                                      4 if taken else 6], ram, 28 if taken else 37))
    assert values[0] == values[1], (mapper, taken, kind, values[0][:28].hex(), values[1][:28].hex())
    assert snapshot(p.old) == snapshot(n), (mapper, taken, kind, 'serialized')
    assert calls[id(p.old)] == calls[id(n)] == (0 if taken else 1)
    assert nested[id(p.old)] == nested[id(n)]
    links = u32(n, 'h1_jit_links') - before_links
    assert links == (1 if taken else 0), (mapper, taken, kind, 'native links', links)
    expected = 0x11 if taken or kind == 'plain' else 0x71
    assert n.m.read(n.s['RAM'] + 0x10, 1)[0] == expected
    if kind == 'nested' and not taken:
        assert nested[id(n)], 'No real nested CPU execution'
    for c in p.cores:
        c.call(c.s['h1_nes_jit_frame_end'])
        assert u32(c, 'h1_jit_contract_epoch') == 0
    p.close()
    return {'mapper': mapper, 'taken': taken, 'callback': kind,
            'native_links': links, 'result': expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bda', 'elf', 'nm', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    results = [case(args, mapper, taken, kind) for mapper in (0, 19)
               for taken in (False, True) for kind in ('plain', 'mutate', 'nested')]
    report = {'ok': True, 'cases': len(results), 'results': results,
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'reference': 'same artifact with public JIT lifecycle closed',
              'checks': ['taken target equals sequential PC after terminal IO',
                         'exactly one native link when skipping IO; none after IO',
                         'precompiled successor byte mutation is revalidated',
                         'real MIPS nested CPU credentials retired after callback',
                         'CPU/RAM/time and full serialized state equality']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
