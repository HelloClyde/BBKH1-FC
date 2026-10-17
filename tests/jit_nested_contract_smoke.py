"""Real-MIPS nested opaque-callback credential regression.

An actual MIPS bus-handler trampoline calls X6502_Run while a frame is open.
The nested CPU validates a compiled block, then the callback changes that
block's operand before returning to the outer CPU. No product instruction is
patched, and Python never recursively starts Unicorn from a callback.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_SP

from jit_contract_smoke import Pair, ROOT, clock, entry, stamp, u32
from jit_event_smoke import snapshot


def observed(c):
    return (c.m.read(c.s['X'], 28) + c.m.read(c.s['RAM'], 2048) +
            c.m.read(c.s['timestamp'], 4) + c.m.read(c.s['sound_timestamp'], 4) +
            c.m.read(c.s['h1_apu_clock'], 13))


def case(args, mapper, kind):
    p = Pair(args, mapper)
    n = p.new
    future = 0x9200
    future_code = bytes.fromhex('a9 11 aa e8 85 10 4c 00 94')
    # The indirect opcodes are unsupported by the emitter, so those cases
    # necessarily reach the original C RdMemNorm/WrMemNorm path.
    outer = {'native_write': 'a9 23 8d 00 50 4c 00 92',
             'c_read': 'b1 20 4c 00 92',
             'c_write': '91 20 4c 00 92',
             'native_mapirq': 'a9 23 aa 4c 00 92',
             'c_mapirq': 'a9 23 aa 4c 00 92'}[kind]
    mapper_callback = kind.endswith('mapirq')
    code = bytes.fromhex(outer)
    p.code(0x9000, code)
    p.code(0x9400, bytes.fromhex('ea 4c 00 94'))
    _, gate = p.compile(future, future_code)
    nested_states, observations = [[], []], []
    native_entries = []

    def native_entry(*unused):
        native_entries.append((u32(n, 'h1_jit_contract_epoch'), stamp(n, future)))

    hook = n.m.uc.hook_add(UC_HOOK_CODE, native_entry, begin=gate, end=gate)
    known_mapper_hooks = {}
    for index, c in enumerate(p.cores):
        outer_states = []
        known_mapper_hooks[c] = u32(c, 'MapIRQHook')

        def before_nested(*unused, core=c, saved=outer_states):
            assert not saved, 'unexpected recursive test callback'
            saved.append(core.m.read(core.s['X'], 28))
            if core is n:
                caller = struct.unpack('<I', core.m.read(
                    core.m.uc.reg_read(UC_MIPS_REG_SP) + 28, 4))[0]
                assert u32(core, 'h1_jit_contract_epoch') == 0
                assert u32(core, 'h1_jit_contract_dirty') == 1
                assert (core.arena[0] <= caller < core.arena[1]) == kind.startswith('native_'), hex(caller)
            if mapper_callback:
                # An unknown mapper callback may restore the pinned mapper
                # and recursively enter CPU execution before its other effects.
                core.m.word(core.s['MapIRQHook'], known_mapper_hooks[core])
            # Give the nested CPU its own complete register/budget context.
            # Global time and APU state intentionally continue advancing.
            cpu = bytearray(28)
            struct.pack_into('<H', cpu, 4, future)
            cpu[6:12] = bytes((0x35, 0xfe, 0x80, 0xf0, 0x45, 0x45))
            core.m.write(core.s['X'], bytes(cpu))
            return 0

        def after_nested(*unused, core=c, saved=outer_states, log=nested_states[index]):
            log.append(observed(core))
            nested = core.m.read(core.s['X'], 28)
            assert nested[6:8] == b'\x11\x12', nested.hex()
            assert struct.unpack_from('<H', nested, 4)[0] == future + 6, nested.hex()
            if core is n:
                active, credential = u32(core, 'h1_jit_contract_epoch'), stamp(core, future)
                assert active and active == credential, (active, credential)
                assert u32(core, 'h1_jit_contract_frame') == 1
                observations.append({'nested_epoch': active, 'validated_stamp': credential,
                                     'nested_cpu': nested.hex()})
            # Preserve the nested register result as callback-owned RAM, then
            # restore every outer register/budget field. This models a nested
            # client with separate CPU context without discarding its result.
            core.m.write(core.s['RAM'] + 0x300, nested)
            core.m.write(core.s['X'], saved.pop())
            core.prg(future + 1, b'\x71')
            return 0

        c.stub(0x80001b80, before_nested)
        c.stub(0x80001ba0, after_nested)
        jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
        words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0,
                 0x34040018, jal(c.s['X6502_Run']), 0,
                 jal(0x80001ba0), 0, 0x8fbf001c,
                 0x3402005a, 0x03e00008, 0x27bd0020]
        c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
        if mapper_callback:
            c.m.word(c.s['MapIRQHook'], 0x80001c00)
        else:
            table = 'ARead' if kind == 'c_read' else 'BWrite'
            c.m.word(c.s[table] + 0x5000 * 4, 0x80001c00)
    if kind == 'native_write':
        p.compile(0x9000, code)
    elif kind == 'native_mapirq':
        # Force the complete-guard backend while MapIRQHook is unknown, so
        # its real generic tick invokes the callback from generated MIPS.
        page = struct.unpack('<I', n.m.read(n.s['Page'] + (0x9000 >> 11) * 4, 4))[0]
        n.call(n.s['h1_nes_jit_compile'], 0x9000, code[0], page, 1)
        assert entry(n, 0x9000)[0] and entry(n, 0x9000)[6] == 0
    elif not mapper_callback:
        assert n.m.read(n.s['h1_jit_modes'] + code[0], 1) == b'\0'
        assert entry(n, 0x9000)[0] == 0

    ram = bytearray((i * 37 + 11) & 255 for i in range(2048))
    ram[0x20:0x22] = b'\x00\x50'
    values = []
    for c in p.cores:
        clock(c)
        if mapper_callback:
            c.m.word(c.s['MapIRQHook'], known_mapper_hooks[c])
        c.call(c.s['h1_nes_jit_frame_begin'])
        if c is n:
            assert u32(c, 'h1_jit_contract_frame') == 1
        if mapper_callback:
            c.m.word(c.s['MapIRQHook'], 0x80001c00)
        values.append(c.run(0x9000, code, [35, 254, 0, 240, 4, 4], bytes(ram), 150))
    assert len(nested_states[0]) == len(nested_states[1]) == 1
    assert nested_states[0] == nested_states[1], (mapper, kind, 'nested CPU/RAM/clock state')
    assert observations and native_entries, (mapper, kind, 'nested native validation absent')
    assert values[0] == values[1], (mapper, kind, 'outer CPU/RAM/clock mismatch',
                                    values[0][:28].hex(), values[1][:28].hex(),
                                    values[0][28 + 0x10], values[1][28 + 0x10])
    assert observed(p.old) == observed(p.new), (mapper, kind, 'outer APU state')
    assert snapshot(p.old) == snapshot(p.new), (mapper, kind, 'serialized state')
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    assert n.m.read(n.s['RAM'] + 0x306, 2) == b'\x11\x12'
    # The first return to the validated future block must have retired the
    # nested callback's credential; a private gate alone does not prove reuse.
    assert len(native_entries) >= 2 and native_entries[1][0] == 0, native_entries
    for c in p.cores:
        c.call(c.s['h1_nes_jit_frame_end'])
        assert u32(c, 'h1_jit_contract_epoch') == 0
    assert snapshot(p.old) == snapshot(p.new), (mapper, kind, 'frame end serialized state')
    n.m.uc.hook_del(hook)
    p.close()
    return {'mapper': mapper, 'path': kind, 'nested': observations,
            'entry_credentials': native_entries, 'outer_operand_result': 0x71,
            'nested_register_result': {'A': 0x11, 'X': 0x12}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-nested-contract.json')
    args = parser.parse_args()
    results = [case(args, mapper, kind) for mapper in (0, 19)
               for kind in ('native_write', 'c_read', 'c_write', 'native_mapirq', 'c_mapirq')]
    report = {'ok': True, 'differential_cases': len(results),
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'reference': 'same artifact, public JIT lifecycle closed', 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
