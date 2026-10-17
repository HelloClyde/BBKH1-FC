"""Actual-MIPS CPU wrapper differential for non-executing PPU slices.

Checks original credit/flush/revoke semantics, pending IRQ/DMC and recursively
entered empty CPU slices. Uses the previous ordinary JIT build as reference.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_smoke import Core
from jit_event_smoke import snapshot, put
from make_test_rom import make_rom


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('baseline-bda', 'baseline-elf', 'bda', 'elf', 'nm', 'output'):
        p.add_argument('--' + key, type=Path, required=True)
    a = p.parse_args()
    cases = 0
    nested_cases = 0
    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        cores = [Core(bda, elf, a.nm, bytes(rom)) for bda, elf in
                 ((a.baseline_bda, a.baseline_elf), (a.bda, a.elf))]
        table_ptrs = [c.m.read(c.s['h1_jit_entries'], 4) for c in cores]
        for c in cores:
            c.prg(0x9000, bytes.fromhex('ea ea ea 4c 00 90'))
            c.prg(0x9400, bytes.fromhex('ea 4c 00 94'))
            c.prg(0xfffa, struct.pack('<3H', 0x9400, 0x9400, 0x9400))

        def observed(c):
            fields = ('timestamp', 'sound_timestamp', 'h1_cpu_apu_deadline',
                      'h1_jit_contract_epoch', 'h1_jit_contract_frame', 'h1_jit_contract_dirty')
            return c.m.read(c.s['X'], 28) + c.m.read(c.s['RAM'], 2048) + b''.join(
                c.m.read(c.s[name], 4) for name in fields)

        for enabled in (0, 1):
            for scope, dirty in ((0, 0), (0, 1), (1, 0), (1, 1)):
                for pal in (0, 1):
                    for cycles in (1, 6, 16):
                        for desired in (-145, -1, 0, 1, 15, 16, 97):
                            for irq in (0, 1, 0x80):
                                results = []
                                snapshots = []
                                for c, table in zip(cores, table_ptrs):
                                    c.m.write(c.s['h1_jit_entries'], table if enabled else bytes(4))
                                    c.m.word(c.s['h1_jit_contract_frame'], scope)
                                    c.m.word(c.s['h1_jit_contract_dirty'], dirty)
                                    c.m.word(c.s['h1_jit_contract_epoch'], 77)
                                    c.m.word(c.s['h1_cpu_apu_deadline'], 12345)
                                    c.m.word(c.s['timestamp'], 123)
                                    c.m.word(c.s['sound_timestamp'], 123)
                                    c.m.write(c.s['PAL'], bytes([pal]))
                                    put(c, 'fhcnt', 'h1_apu_fhcnt', 145)
                                    put(c, 'DMCacc', 'h1_apu_dmcacc', 3)
                                    put(c, 'DMCSize', 'h1_apu_dmcsize', 1)
                                    put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
                                    if mapper == 19:
                                        put(c, 'IRQa', 'h1_namco_irq_enabled', 1, 1)
                                        put(c, 'IRQCount', 'h1_namco_irq_count', 0x7ffe, 2)
                                    cpu = bytearray(28)
                                    struct.pack_into('<I', cpu, 0, 2)
                                    struct.pack_into('<H', cpu, 4, 0x9000)
                                    cpu[6:12] = bytes([35, 254, 128, 240, 4, 0])
                                    struct.pack_into('<i', cpu, 16, desired - cycles * (15 if pal else 16))
                                    struct.pack_into('<I', cpu, 20, irq)
                                    cpu[24] = 0xa7
                                    c.m.write(c.s['X'], bytes(cpu))
                                    c.m.write(c.s['RAM'], bytes(2048))
                                    before = c.native
                                    c.call(c.s['X6502_Run'], cycles)
                                    if desired <= 0:
                                        expected = bytearray(cpu)
                                        struct.pack_into('<i', expected, 16, desired)
                                        assert c.m.read(c.s['X'], 28) == bytes(expected), 'empty slice changed CPU state'
                                        assert c.native == before, 'empty slice entered native code'
                                    results.append(observed(c))
                                    snapshots.append(snapshot(c))
                                    c.m.write(c.s['h1_jit_entries'], table)
                                label = (mapper, enabled, scope, dirty, pal, cycles, desired, irq)
                                assert results[0] == results[1], (label, 'CPU/RAM/clock/credential')
                                assert snapshots[0] == snapshots[1], (label, 'serialized state')
                                cases += 1
        # A real MIPS callback enters the public CPU wrapper recursively with
        # a negative count. Python stubs only provide/save the test-owned
        # nested CPU context; they never start Unicorn recursively.
        for scope in (0, 1):
            logs = [[], []]
            for index, c in enumerate(cores):
                c.m.word(c.s['h1_jit_contract_frame'], scope)
                c.m.word(c.s['h1_jit_contract_dirty'], 0)
                c.m.word(c.s['h1_jit_contract_epoch'], 0)
                c.m.word(c.s['h1_cpu_apu_deadline'], 0)
                c.m.word(c.s['timestamp'], 0)
                c.m.word(c.s['sound_timestamp'], 0)
                c.m.write(c.s['PAL'], b'\x00')
                put(c, 'fhcnt', 'h1_apu_fhcnt', 1000000)
                put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)
                put(c, 'DMCSize', 'h1_apu_dmcsize', 0)
                if mapper == 19:
                    put(c, 'IRQa', 'h1_namco_irq_enabled', 0, 1)
                saved = []

                def before_nested(*unused, core=c, saved=saved):
                    saved.append(core.m.read(core.s['X'], 28))
                    nested = bytearray(saved[0])
                    struct.pack_into('<i', nested, 16, -1000)
                    struct.pack_into('<H', nested, 4, 0x9400)
                    nested[11] = nested[10] ^ 4
                    core.m.write(core.s['X'], bytes(nested))
                    return 0

                def after_nested(*unused, core=c, saved=saved, log=logs[index]):
                    nested = core.m.read(core.s['X'], 28)
                    assert struct.unpack_from('<i', nested, 16)[0] == -904
                    assert struct.unpack_from('<H', nested, 4)[0] == 0x9400
                    log.append(nested)
                    core.m.write(core.s['X'], saved.pop())
                    return 0

                c.stub(0x80001b80, before_nested)
                c.stub(0x80001ba0, after_nested)
                jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
                words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0,
                         0x34040006, jal(c.s['X6502_Run']), 0,
                         jal(0x80001ba0), 0, 0x8fbf001c,
                         0x3402005a, 0x03e00008, 0x27bd0020]
                c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
                c.m.word(c.s['ARead'] + 0x5000 * 4, 0x80001c00)
                c.run(0x9000, bytes.fromhex('ad 00 50 ea ea ea'), [35, 0, 0, 240, 4, 0], bytes(2048), 13)
            assert len(logs[0]) == 1 and logs[0] == logs[1]
            assert observed(cores[0]) == observed(cores[1])
            assert snapshot(cores[0]) == snapshot(cores[1])
            nested_cases += 1
        for c in cores:
            c.close()
    report = {'ok': True, 'cases': cases, 'recursive_empty_cases': nested_cases,
              'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'checks': ['negative/zero/just-positive CPU credit in PAL and NTSC',
                         'JIT/no-JIT wrapper paths and clean/dirty frame/no-frame credentials',
                         'empty slices preserve PC, PI!=P, DB, IRQ, tcount and pending DMA',
                         'exact CPU/RAM/timestamps, APU deadline, credentials and serialized state',
                         'actual MIPS recursive opaque read with an empty nested CPU slice']}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
