"""Protect live MIPS return addresses when nested compilation fills the arena.

Only diagnostic ROM, handler pointers, and explicit arena-boundary metadata
are changed. An actual MIPS callback trampoline enters X6502_Run recursively;
Python callbacks never recursively invoke Unicorn. The optional old-version
negative control stops inside the callback before resuming overwritten code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_SP

from jit_contract_smoke import Pair, ROOT, clock, entry, u32
from jit_event_smoke import snapshot


def raw_state(c):
    return (c.m.read(c.s['X'], 28) + c.m.read(c.s['RAM'], 2048) +
            c.m.read(c.s['timestamp'], 4) + c.m.read(c.s['sound_timestamp'], 4) +
            c.m.read(c.s['h1_apu_clock'], 13))


def depth(c):
    return u32(c, 'h1_jit_native_depth') if 'h1_jit_native_depth' in c.s else None


def case(args, mapper, full, direct=False):
    p = Pair(args, mapper)
    n = p.new
    negative = args.expect_unsafe_wrap
    if not negative:
        assert 'h1_jit_native_depth' in n.s
    cache = u32(n, 'cache')
    cache_size = u32(n, 'cache_bytes')
    assert cache_size == args.cache_mib * 1024 * 1024, cache_size
    outer_pc, prefix_pc, inner_pc = 0x9000, 0x9100, 0x9200
    outer_code = bytes.fromhex('a9 23 8d 00 50 4c 00 94')
    prefix_code = bytes.fromhex('a9 11 aa e8 4c 00 92')
    inner_code = bytes.fromhex('a9 11 aa e8 85 10 4c 00 94')
    p.code(outer_pc, outer_code)
    p.code(prefix_pc, prefix_code)
    p.code(inner_pc, inner_code)
    p.code(0x9400, bytes.fromhex('ea 4c 00 94'))
    nested_states, callback_observations = [[], []], []
    before_arena = {}
    compile_observations = []
    native_depths = []

    def compile_entry(uc, *unused):
        if uc.reg_read(UC_MIPS_REG_A0) == inner_pc:
            compile_observations.append({'depth': depth(n), 'used': u32(n, 'used_bytes')})

    def native_entry(*unused):
        native_depths.append(depth(n))

    # Install all observation hooks before executing the outer CPU.
    hooks = [n.m.uc.hook_add(UC_HOOK_CODE, compile_entry,
                            begin=n.s['h1_nes_jit_compile'], end=n.s['h1_nes_jit_compile']),
             n.m.uc.hook_add(UC_HOOK_CODE, native_entry, begin=cache, end=cache + cache_size - 1)]
    for index, c in enumerate(p.cores):
        saved = []

        def before_nested(*unused, core=c, saved=saved):
            assert not saved
            saved.append(core.m.read(core.s['X'], 28))
            if core is n:
                caller = struct.unpack('<I', core.m.read(
                    core.m.uc.reg_read(UC_MIPS_REG_SP) + 28, 4))[0]
                assert cache <= caller < cache + before_arena['bytes'], hex(caller)
                if not negative:
                    assert depth(core) == 1
            cpu = bytearray(28)
            struct.pack_into('<H', cpu, 4, prefix_pc)
            cpu[6:12] = bytes((0x35, 0xfe, 0x80, 0xf0, 0x45, 0x45))
            core.m.write(core.s['X'], bytes(cpu))
            return 0

        def after_nested(*unused, core=c, saved=saved, log=nested_states[index]):
            log.append(raw_state(core))
            nested = core.m.read(core.s['X'], 28)
            assert nested[6:8] == b'\x11\x12', nested.hex()
            assert struct.unpack_from('<H', nested, 4)[0] == inner_pc + 6
            if core is n:
                unchanged = core.m.read(cache, before_arena['bytes']) == before_arena['code']
                state = {'depth': depth(core), 'outer_code_unchanged': unchanged,
                         'epoch': u32(core, 'h1_jit_epoch'), 'wraps': u32(core, 'h1_jit_wraps'),
                         'used': u32(core, 'used_bytes'), 'inner_run': entry(core, inner_pc)[0],
                         'inner_heat': entry(core, inner_pc)[4]}
                callback_observations.append(state)
                if negative:
                    assert full and not unchanged, state
                    assert state['wraps'] == before_arena['wraps'] + 1, state
                    assert state['epoch'] != before_arena['epoch'], state
                    # We have proved corruption while still in our separately
                    # allocated callback, before any overwritten outer resume.
                    core.m.finished = True
                    core.m.uc.emu_stop()
                    return 0
                assert depth(core) == 1 and unchanged, state
                assert state['epoch'] == before_arena['epoch'], state
                assert state['wraps'] == before_arena['wraps'], state
                if full:
                    assert state['used'] == before_arena['used'], state
                    assert state['inner_run'] == 0 and state['inner_heat'] == 8, state
                else:
                    assert state['used'] > before_arena['used'] and state['inner_run'], state
            core.m.write(core.s['RAM'] + 0x300, nested)
            core.m.write(core.s['X'], saved.pop())
            return 0

        c.stub(0x80001b80, before_nested)
        c.stub(0x80001ba0, after_nested)
        jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
        words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0,
                 0x34040033, jal(c.s['X6502_Run']), 0,
                 jal(0x80001ba0), 0, 0x8fbf001c,
                 0x3402005a, 0x03e00008, 0x27bd0020]
        c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
        c.m.word(c.s['BWrite'] + 0x5000 * 4, 0x80001c00)
    p.compile(outer_pc, outer_code)
    assert entry(n, outer_pc)[0] == cache + 32
    outer_entry = entry(n, outer_pc)[0]
    # A warm nested prefix proves real depth 2 even when the cold target must
    # remain interpreted. It returns to depth 1 before cold compilation.
    p.compile(prefix_pc, prefix_code)
    allocated = u32(n, 'used_bytes')
    before_arena.update(bytes=allocated, code=n.m.read(cache, allocated),
                        epoch=u32(n, 'h1_jit_epoch'), wraps=u32(n, 'h1_jit_wraps'))
    if full:
        n.m.word(n.s['used_bytes'], cache_size - 32)
    before_arena['used'] = u32(n, 'used_bytes')
    page = struct.unpack('<I', n.m.read(n.s['Page'] + (inner_pc >> 11) * 4, 4))[0]
    n.call(n.s['h1_nes_jit_bind'], inner_pc, inner_code[0], page)
    inner_entry = u32(n, 'h1_jit_entries') + inner_pc * 16
    # Seed only the cold admission counter, so the actual nested dispatcher
    # performs compilation on this first execution. No native body exists yet.
    n.m.write(inner_entry + 13, b'\x07')
    assert entry(n, inner_pc)[0] == 0
    ram = bytes((i * 37 + 11) & 255 for i in range(2048))
    values = []
    for c in p.cores:
        clock(c)
        c.call(c.s['h1_nes_jit_frame_begin'])
        # Exactly LDA + terminal STA: stop before an outer dispatcher could
        # safely compile another block after the active native frame returns.
        if direct and c is n:
            cpu = bytearray(28)
            struct.pack_into('<H', cpu, 4, outer_pc)
            cpu[6:12] = bytes((35, 254, 0, 240, 4, 4))
            struct.pack_into('<i', cpu, 16, 6 * 48)
            c.m.write(c.s['X'], bytes(cpu))
            c.m.write(c.s['RAM'], ram)
            packet = c.call(outer_entry, c.s['X'], c.s['RAM'])
            assert packet & 0xffff == outer_pc + 5, hex(packet)
            assert (packet >> 16) & 0x7fff == 2 and not packet & 0x80000000, hex(packet)
            values.append(None)
        else:
            values.append(c.run(outer_pc, outer_code, [35, 254, 0, 240, 4, 4], ram, 18))
    assert len(nested_states[0]) == len(nested_states[1]) == 1
    assert nested_states[0] == nested_states[1], (mapper, full, 'nested CPU/RAM/APU')
    assert compile_observations and callback_observations
    if negative:
        result = {'mapper': mapper, 'cache_mib': args.cache_mib,
                  'outer_resume_aborted': True, 'live_code_overwrite_detected': True,
                  'callback': callback_observations[0]}
    else:
        if direct:
            # A raw block returns a PC packet; materializing that PC belongs
            # to the C dispatcher. Compare all other observable state without
            # pretending the raw entry is itself a complete CPU invocation.
            assert raw_state(p.old)[:4] + raw_state(p.old)[6:] == raw_state(n)[:4] + raw_state(n)[6:]
        else:
            assert values[0] == values[1], (mapper, full, 'outer CPU/RAM/clock')
            assert raw_state(p.old) == raw_state(n), (mapper, full, 'outer APU')
            assert snapshot(p.old) == snapshot(n), (mapper, full, 'outer serialized')
        assert depth(n) == 0
        assert compile_observations[0]['depth'] == 1
        assert 1 in native_depths and 2 in native_depths, native_depths
        assert n.m.read(cache, allocated) == before_arena['code']
        assert u32(n, 'h1_jit_epoch') == before_arena['epoch']
        assert u32(n, 'h1_jit_wraps') == before_arena['wraps']
        for c in p.cores:
            c.call(c.s['h1_nes_jit_frame_end'])
        if full:
            # The same deferred entry must become compilable at depth zero;
            # reclaiming the arena is now safe and must advance its epoch.
            n.call(n.s['h1_nes_jit_compile'], inner_pc, inner_code[0], page, 0)
            assert compile_observations[-1]['depth'] == 0
            assert entry(n, inner_pc)[0] == cache + 32
            assert u32(n, 'h1_jit_wraps') == before_arena['wraps'] + 1
            assert u32(n, 'h1_jit_epoch') != before_arena['epoch']
            assert u32(n, 'used_bytes') < cache_size - 32
        before_native = n.native
        p.run(inner_pc, inner_code, 24, 'depth-zero cold block executes after nested callback')
        assert n.native > before_native and depth(n) == 0
        result = {'mapper': mapper, 'cache_mib': args.cache_mib, 'arena_full': full,
                  'entry_path': 'public_strict' if direct else 'dispatcher_private_gate',
                  'callback': callback_observations[0], 'compile_calls': compile_observations,
                  'native_depths': sorted(set(native_depths)),
                  'safe_wrap_after_outer_return': full,
                  'outer_cpu_ram_apu_serialized_equal': not direct,
                  'post_wrap_cpu_ram_apu_serialized_equal': True}
    for hook in hooks:
        n.m.uc.hook_del(hook)
    p.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--cache-mib', type=int, required=True, choices=(4, 8))
    parser.add_argument('--expect-unsafe-wrap', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-arena-nested.json')
    args = parser.parse_args()
    modes = ((True, False),) if args.expect_unsafe_wrap else ((False, False), (True, False), (True, True))
    results = [case(args, mapper, full, direct) for mapper in (0, 19) for full, direct in modes]
    report = {'ok': True, 'negative_control': args.expect_unsafe_wrap, 'cases': len(results),
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(), 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
