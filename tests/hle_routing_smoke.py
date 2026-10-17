"""Actual MIPS routing, direct native entry, and post-routing event observations."""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from hle_wait_smoke import Pair, PC, LOOP, stats, u32, u32_at
from jit_event_smoke import snapshot
from make_test_rom import make_rom
from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_A0


def entry(c, pc):
    table = u32(c, 'h1_jit_entries')
    return struct.unpack('<IIIBBBB', c.m.read(table + pc * 16, 16))


def compile(c, pc):
    page = u32_at(c, c.s['Page'] + (pc >> 11) * 4)
    opcode = c.m.read(page + pc, 1)[0]
    c.call(c.s['h1_nes_jit_compile'], pc, opcode, page, 1)
    return entry(c, pc)


def main():
    parser = argparse.ArgumentParser()
    for name in ('bda', 'elf', 'baseline-bda', 'baseline-elf', 'nm', 'rom', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    p = Pair(args)
    details = []
    for name, start, code in (
        ('fall-through', PC - 4, bytes.fromhex('ea ea ea ea')),
        ('jump', 0xfa00, bytes.fromhex('ea 4c 28 fb')),
        ('call', 0xfa00, bytes.fromhex('ea 20 28 fb')),
        ('taken branch', PC - 5, bytes.fromhex('ea d0 02 ea ea')),
        ('indirect jump', 0xfa00, bytes.fromhex('ea 6c 10 00')),
        ('return', 0xfa00, bytes.fromhex('ea 60')),
    ):
        for value in (0, 0x80):
            p.prepare(count=2200, value=value, pc=start, p=4, pi=4)
            for c in p.cores:
                c.prg(start, code)
                if name == 'indirect jump':
                    c.m.write(c.s['RAM'] + 0x10, struct.pack('<H', PC))
                if name == 'return':
                    c.m.write(c.s['RAM'] + 0x1fe, struct.pack('<H', PC - 1))
            target = compile(p.new, PC)
            assert target[0] == 0 and target[5] == 0, 'Admitted loop must not become native'
            source = compile(p.new, start)
            assert source[0], (name, 'Source did not compile')
            if name == 'fall-through':
                assert source[5] == 4, ('Decoder did not stop before wait loop', source)
            before = p.new.native
            p.run((name, value), value != 0)
            assert p.new.native > before, (name, 'Native source never executed')
            assert entry(p.new, PC)[0] == 0, 'Execution materialized forbidden target'
            details.append(dict(path=name, flag=value, source_ops=source[5]))
    # A direct public native entry returns a next-PC packet while leaving X.PC
    # at the last opcode, exactly the native dispatch ABI. Commit only that PC
    # as the ordinary dispatch epilogue does, then compare complete state.
    p.prepare(count=384, pc=PC - 4, value=0x80)
    for c in p.cores:
        c.prg(PC - 4, bytes.fromhex('ea ea ea ea'))
    source = compile(p.new, PC - 4)
    p.old.call(p.old.s['X6502_Run'], 0)
    before = stats(p.new)
    packet = p.new.call(source[0], p.new.s['X'], p.new.s['RAM'])
    assert packet & 0xffff == PC and (packet >> 16) & 0x7fff == 4, hex(packet)
    assert stats(p.new) == before, 'Direct native prefix unexpectedly invoked HLE'
    assert struct.unpack('<H', p.new.m.read(p.new.s['X'] + 4, 2))[0] == PC - 1
    p.new.m.write(p.new.s['X'] + 4, struct.pack('<H', packet & 0xffff))
    p.check('direct native prefix packet and exact CPU-call boundary')
    p.run('resume after direct native prefix', True, cycles=256)
    # Observe actual slow APU event after native prefix -> C HLE -> ordinary
    # instruction. All callback-visible CPU bytes must match the interpreter.
    for carry in (0, 1, 2):
        p.prepare(count=2200, pc=PC - 4, carry=carry, value=0x80,
                  frame=(20 + carry) * 48, p=0x45, pi=0x12)
        for c in p.cores:
            c.prg(PC - 4, bytes.fromhex('ea ea ea ea'))
        assert compile(p.new, PC - 4)[0]
        traces = [[], []]
        hooks = []
        for index, c in enumerate(p.cores):
            def observer(uc, address, size, unused, c=c, index=index):
                cycles = uc.reg_read(UC_MIPS_REG_A0)
                frame = struct.unpack('<i', c.m.read(c.s['h1_apu_clock'], 4))[0]
                if frame <= cycles * 48:
                    traces[index].append((cycles, c.m.read(c.s['X'], 28).hex(),
                                          u32(c, 'timestamp')))
            hooks.append((c, c.m.uc.hook_add(UC_HOOK_CODE, observer,
                          begin=c.s['FCEU_SoundCPUHook'], end=c.s['FCEU_SoundCPUHook'])))
        p.run(('native/C/HLE event boundary', carry), True)
        for c, hook in hooks:
            c.m.uc.hook_del(hook)
        assert traces[0] and traces[0] == traces[1], (carry, traces)
        assert struct.unpack_from('<H', bytes.fromhex(traces[1][0][1]), 4)[0] == PC + 2
    report = dict(ok=True, cases=p.cases, positive_hle_cases=p.hits,
                  negative_hle_cases=p.negative, route_cases=details)
    p.close()
    # Same bytes and same CPU PC in an unrelated mapper-19 ROM keep ordinary
    # native compilation. The routing change must not be a mapper-wide ban.
    rom = bytearray(make_rom(dmc=True))
    rom[6] = (rom[6] & 15) | 0x30
    rom[7] = 0x10
    offset = 16 + rom[4] * 16384 - 8192 + 0x1b28
    rom[offset:offset + len(LOOP)] = LOOP
    q = Pair(args, bytes(rom), admitted=False)
    q.prepare(count=2200)
    target = compile(q.new, PC)
    assert target[0] and target[5] == 2, ('Unrelated ROM lost native wait block', target)
    before = q.new.native
    q.run('unrelated ROM keeps ordinary native block', False)
    assert q.new.native > before
    report['cases'] += q.cases
    report['negative_hle_cases'] += q.negative
    report['unknown_rom_native_path_preserved'] = True
    q.close()
    report.update(bda_sha256=hashlib.sha256(args.bda.read_bytes()).hexdigest(),
                  baseline_sha256=hashlib.sha256(args.baseline_bda.read_bytes()).hexdigest(),
                  checks=['native fall-through/JMP/JSR/branch/indirect/RTS to HLE',
                          'zero flag follows original execution',
                          'public direct native packet and intermediate PC ABI',
                          'actual post-native HLE APU callback-visible CPU state',
                          'unrelated mapper19 retains native compilation'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    assert not args.output.exists()
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
