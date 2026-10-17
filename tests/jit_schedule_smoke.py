"""Compare C APU event-deadline scheduling to this artifact's untouched interpreter.

Runs actual MIPS. Diagnostic bus callbacks observe or change state at the same
bus access; guest/native instructions are never patched. Whole-frame cost and
PCM/video equivalence are measured separately by core_equivalence.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_PC, UC_MIPS_REG_RA

from jit_event_smoke import put, snapshot
from jit_smoke import Core, ROOT
from make_test_rom import make_rom


def observed(c):
    return (c.m.read(c.s['X'], 28) +
            c.m.read(c.s['timestamp'], 4) + c.m.read(c.s['sound_timestamp'], 4) +
            c.m.read(c.s['h1_apu_clock'], 13))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    p.add_argument('--nm', type=Path, required=True)
    p.add_argument('--output', type=Path, default=ROOT/'build/verification/jit-schedule.json')
    a = p.parse_args()
    checks, cases = [], 0
    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        old, new = [Core(a.bda, a.elf, a.nm, bytes(rom)) for _ in range(2)]
        old.call(old.s['h1_nes_jit_close'])
        old.arena = None
        cores = old, new
        regs, ram = [35, 254, 128, 240, 4, 4], bytes(2048)

        def prepare(frame=1000000, dmc=1000000, dma=0, size=0):
            for c in cores:
                put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
                put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
                put(c, 'DMCSize', 'h1_apu_dmcsize', size)
                put(c, 'DMCHaveDMA', 'h1_apu_have_dma', dma, 1)
                c.m.word(c.s['timestamp'], 0)
                c.m.word(c.s['sound_timestamp'], 0)

        def run(code, cycles=70, carry=0, irq=0, encrypted=13, label=''):
            nonlocal cases
            results = [c.run(0x9000, code, regs, ram, cycles, irq,
                             encrypted=encrypted, tcount=carry & 0xffffffff) for c in cores]
            assert results[0] == results[1], (mapper, label, 'CPU/RAM/timestamp')
            assert snapshot(old) == snapshot(new), (mapper, label, 'serialized')
            assert old.m.read(old.s['h1_apu_clock'], 13) == new.m.read(new.s['h1_apu_clock'], 13)
            cases += 1

        # Mode 13 does not decode opcodes but disables native entry, isolating
        # the production C fallback without changing its instructions.
        loop = bytes.fromhex('08 48 68 28 d0 fa')
        for frame in (1, 143, 144, 145, 287, 288, 289, 1000000):
            for dmc in (1, 3, 4, 7, 1000000):
                for carry in (0, 2):
                    prepare(frame, dmc)
                    run(loop, carry=carry, label=('deadline', frame, dmc, carry))
        for dma, size in ((0, 1), (1, 1), (0, 0), (1, 0)):
            prepare(145, 4, dma, size)
            run(loop, carry=2, label=('DMA', dma, size))
        prepare(145, 4, 0, 1)
        run(loop, cycles=1, carry=-3, label='zero temp still services pending DMA')
        for branch in ('d0 00', 'd0 80'):
            prepare()
            run(bytes.fromhex(branch), cycles=1, label=('last branch carry', branch))
        prepare()
        for c in cores:
            c.prg(0x8f84, bytes.fromhex('ea ea ea'))
        run(bytes.fromhex('08 08 d0 80'), cycles=30, label='active span ends on page-cross branch')
        for c in cores:
            c.call(c.s['X6502_Run'], 1)
        assert observed(old) == observed(new), (mapper, 'active branch next slice carry')
        assert snapshot(old) == snapshot(new)
        prepare()
        wrapped, saved = [[], []], []
        for index, c in enumerate(cores):
            c.m.word(c.s['timestamp'], 0xfffffff0)
            c.m.word(c.s['sound_timestamp'], 0xfffffff0)
            slot = c.s['ARead'] + 0x5000 * 4
            saved.append((slot, c.m.read(slot, 4)))
            def wrap_observer(*unused, core=c, log=wrapped[index]):
                log.append(observed(core))
                return 0x5a
            c.m.word(slot, c.stub(0x80001b80, wrap_observer))
        run(bytes.fromhex('08 08 ea ea ea ea ea ea ad 00 50 ea ea'), cycles=80,
            label='active span timestamp wrap')
        assert wrapped[0] and wrapped[0] == wrapped[1]
        for c, (slot, value) in zip(cores, saved):
            c.m.write(slot, value)
        checks.append(f'mapper {mapper}: strict APU deadlines, pending DMA and previous-branch carry')

        # Each observer sees exact counters BEFORE its own mutation. Unknown
        # mapper callbacks see only the previous APU state, before this opcode's temp.
        for kind in ('read', 'write', 'opcode', 'mapper', 'ppu', 'input'):
            observations, saved = [[], []], []
            prepare()
            for index, c in enumerate(cores):
                def observer(address, *unused, core=c, log=observations[index], which=kind):
                    log.append(observed(core))
                    if which in ('read', 'write'):
                        put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
                    if which == 'opcode':
                        return 0xea
                    return 0x5a
                callback = c.stub(0x80001b80, observer)
                if kind in ('read', 'write', 'opcode'):
                    symbol = 'BWrite' if kind == 'write' else 'ARead'
                    slot = c.s[symbol] + 0x5000 * 4
                else:
                    slot = c.s[{'mapper':'MapIRQHook', 'ppu':'PPU_hook', 'input':'InputScanlineHook'}[kind]]
                saved.append((slot, c.m.read(slot, 4)))
                c.m.word(slot, callback)
                if kind in ('ppu', 'input'):
                    # Enable a live line so the real A2002 path reaches the
                    # installed PPU/input observer during LineUpdate.
                    c.m.word(c.s['Pline'], 0x83100000)
                    c.m.word(c.s['Plinef'], 0x83100000)
                    c.m.word(c.s['linestartts'], 0)
                    c.m.word(c.s['firsttile'], 0)
                    c.m.write(c.s['PPU'] + 1, b'\x08')
            tail = {'read':'ad 00 50', 'write':'8d 00 50', 'opcode':'4c 00 50',
                    'mapper':'ea', 'ppu':'ad 02 20', 'input':'ad 02 20'}[kind]
            run(bytes.fromhex('08 48 68 28 ' + tail + ' ea ea ea'), cycles=65,
                label=('observer', kind))
            assert observations[0] and observations[0] == observations[1], (mapper, kind, 'observer')
            for c, (slot, value) in zip(cores, saved):
                c.m.write(slot, value)
                if kind in ('ppu', 'input'):
                    c.m.word(c.s['Pline'], 0)
                    c.m.word(c.s['Plinef'], 0)
                    c.m.write(c.s['PPU'] + 1, b'\x00')
        checks.append(f'mapper {mapper}: unknown bus/opcode/mapper observers and hooked A2002 see exact counters')

        # Execute nested CPU calls in actual MIPS, never by recursively starting
        # Unicorn from a Python callback. The opaque bus handler must retire its
        # outer event deadline before entering the nested interpreter invocation.
        for kind, frame_scope in (('read', 0), ('write', 0), ('read', 1), ('write', 1)):
            observations, saved = [[], []], []
            prepare()
            if frame_scope:
                for c in cores:
                    c.call(c.s['h1_nes_jit_frame_begin'])
                assert new.m.read(new.s['h1_jit_contract_frame'], 4) == b'\x01\x00\x00\x00'
            for index, c in enumerate(cores):
                c.prg(0x9200, bytes.fromhex('08 48 68 28 4c 00 92'))
                def before_nested(*unused, core=c, log=observations[index]):
                    log.append(observed(core))
                    if 'h1_cpu_apu_deadline' in core.s:
                        assert core.m.read(core.s['h1_cpu_apu_deadline'], 4) == core.m.read(core.s['timestamp'], 4)
                    core.m.write(core.s['X'] + 4, struct.pack('<H', 0x9200))
                    return 0
                def after_nested(*unused, core=c, log=observations[index], scope=frame_scope):
                    log.append(observed(core))
                    if not scope and 'h1_cpu_apu_deadline' in core.s:
                        assert core.m.read(core.s['h1_cpu_apu_deadline'], 4) == core.m.read(core.s['timestamp'], 4)
                    if scope:
                        # Nested CPU has cached a new deadline; mutate after
                        # it returns to require the OUTER callback's close.
                        put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
                    return 0
                c.stub(0x80001b80, before_nested)
                c.stub(0x80001ba0, after_nested)
                jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
                words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0,
                         0x34040001, jal(c.s['X6502_Run']), 0,
                         jal(0x80001ba0), 0, 0x8fbf001c,
                         0x3402005a, 0x03e00008, 0x27bd0020]
                c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
                slot = c.s['ARead' if kind == 'read' else 'BWrite'] + 0x5000 * 4
                saved.append((slot, c.m.read(slot, 4)))
                c.m.word(slot, 0x80001c00)
            opcode = 'ad' if kind == 'read' else '8d'
            run(bytes.fromhex('08 48 ' + opcode + ' 00 50 68 28 ea ea'), cycles=80,
                label=('nested CPU bus callback', kind, frame_scope))
            if frame_scope:
                for c in cores:
                    c.call(c.s['X6502_Run'], 20)
                    c.call(c.s['h1_nes_jit_frame_end'])
                assert observed(old) == observed(new), (mapper, kind, 'nested callback changes deadline after nested CPU')
                assert snapshot(old) == snapshot(new)
            assert len(observations[0]) == 2 and observations[0] == observations[1], (mapper, kind, 'nested observer')
            for c, (slot, value) in zip(cores, saved):
                c.m.write(slot, value)
        checks.append(f'mapper {mapper}: real MIPS nested CPU read/write callbacks retire cached event deadlines')

        # IRQ service can exhaust the budget before this invocation reaches an
        # opcode. Its unconsumed tcount belongs to the next APU hook.
        for irq in (0x40, 0x80, 0x20, 1):
            for c in cores:
                c.prg(0xfffa, struct.pack('<3H', 0x9000, 0x9000, 0x9000))
            prepare()
            run(loop, cycles=1, carry=2, irq=irq, label=('IRQ return', irq))
            for c in cores:
                c.call(c.s['X6502_Run'], 1)
            assert observed(old) == observed(new), (mapper, irq, 'next slice carry')
            assert snapshot(old) == snapshot(new)
        for value in (1, 0, 1, 0):
            prepare()
            for c in cores:
                c.m.word(c.s['overclocked'], value)
            run(loop, label=('overclock transition', value))
        checks.append(f'mapper {mapper}: IRQ early return retains tcount; overclock calls invalidate event deadlines')

        # Only an admitted frame can preserve an absolute event deadline over
        # the PPU's short CPU slices. Both frame boundaries retire it, including
        # the timestamp reset performed by the frame/audio frontend.
        for stamp in (0, 0xfffffff0):
            prepare()
            for c in cores:
                c.m.word(c.s['timestamp'], stamp)
                c.m.word(c.s['sound_timestamp'], stamp)
                c.call(c.s['h1_nes_jit_frame_begin'])
            assert new.m.read(new.s['h1_jit_contract_frame'], 4) == b'\x01\x00\x00\x00'
            run(loop, cycles=10, label=('frame-scope first slice', stamp))
            first_deadline = new.m.read(new.s['h1_cpu_apu_deadline'], 4)
            for step in range(12):
                for c in cores:
                    c.call(c.s['X6502_Run'], 10)
                assert observed(old) == observed(new), (mapper, stamp, step, 'cross-slice')
                assert snapshot(old) == snapshot(new)
                assert new.m.read(new.s['h1_cpu_apu_deadline'], 4) == first_deadline
                cases += 1
            for c in cores:
                c.call(c.s['h1_nes_jit_frame_end'])
                assert c.m.read(c.s['h1_cpu_apu_deadline'], 4) == c.m.read(c.s['timestamp'], 4)
                c.m.word(c.s['timestamp'], 0)
                c.m.word(c.s['sound_timestamp'], 0)
                c.call(c.s['h1_nes_jit_frame_begin'])
                assert c.m.read(c.s['h1_cpu_apu_deadline'], 4) == bytes(4)
                c.call(c.s['h1_nes_jit_frame_end'])
        checks.append(f'mapper {mapper}: admitted-frame slices share deadline across timestamp wrap; both frame boundaries invalidate')

        # A normal C PHP prefix hands off to a compiled block, which later
        # returns to another unsupported instruction in the same CPU slice.
        code = bytes.fromhex('08 08 a9 23 aa e8 86 10 28 4c 00 90')
        for c in cores:
            c.prg(0x9000, code)
        page = struct.unpack('<I', new.m.read(new.s['Page'] + (0x9002 >> 11) * 4, 4))[0]
        new.call(new.s['h1_nes_jit_compile'], 0x9002, 0xa9, page, 0)
        before = new.native
        prepare()
        run(code, cycles=150, encrypted=0, label='C-native-C handoff')
        assert new.native > before
        checks.append(f'mapper {mapper}: native handoff preserves CPU/APU state')

        # No-event native instructions keep the C deadline; a real native
        # SoundCPUHook must invalidate it before resuming C interpretation.
        # A2002 excludes whole-run admission, so this enters the actual
        # per-instruction native event hook rather than a C event-prefix exit.
        code = bytes.fromhex('08 08 ad 02 20 e8 e8 08 08 ea ea ea')
        for c in cores:
            c.prg(0x9000, code)
        new.call(new.s['h1_nes_jit_compile'], 0x9002, 0xad, page, 0)
        for frame, dmc in ((481, 1000000), (1000000, 10)):
            trace = {'shared': 0, 'native_event_invalidated': 0}
            def native_entry(uc, address, size, unused):
                if struct.unpack('<i', struct.pack('<I', (struct.unpack('<I', new.m.read(new.s['h1_cpu_apu_deadline'], 4))[0] - struct.unpack('<I', new.m.read(new.s['timestamp'], 4))[0]) & 0xffffffff))[0] > 0:
                    trace['shared'] += 1
            def invalidate(uc, address, size, unused):
                pc, ra = uc.reg_read(UC_MIPS_REG_PC), uc.reg_read(UC_MIPS_REG_RA)
                value = struct.unpack('<I', new.m.read(new.s['h1_cpu_apu_deadline'], 4))[0]
                if (value == struct.unpack('<I', new.m.read(new.s['timestamp'], 4))[0] and new.s['FCEU_SoundCPUHook'] <= pc < new.s['FCEU_SoundCPUHook'] + 256
                        and new.arena[0] <= ra < new.arena[1]):
                    trace['native_event_invalidated'] += 1
            hooks = [new.m.uc.hook_add(UC_HOOK_CODE, native_entry, begin=new.arena[0], end=new.arena[1] - 1),
                     new.m.uc.hook_add(UC_HOOK_CODE, invalidate,
                                       begin=new.s['FCEU_SoundCPUHook'], end=new.s['FCEU_SoundCPUHook'] + 255)]
            prepare(frame, dmc)
            run(code, cycles=65, encrypted=0, label=('native sound event invalidates deadline', frame, dmc))
            for hook in hooks:
                new.m.uc.hook_del(hook)
            assert trace['shared'] and trace['native_event_invalidated'], (mapper, trace)
        observations, saved = [[], []], []
        code = bytes.fromhex('08 08 a9 23 8d 00 50 08 08 ea ea ea')
        for index, c in enumerate(cores):
            c.prg(0x9000, code)
            def native_write(address, value, *unused, core=c, log=observations[index]):
                log.append(observed(core))
                assert core.m.read(core.s['h1_cpu_apu_deadline'], 4) == core.m.read(core.s['timestamp'], 4)
                put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
                return 0
            slot = c.s['BWrite'] + 0x5000 * 4
            saved.append((slot, c.m.read(slot, 4)))
            c.m.word(slot, c.stub(0x80001b80, native_write))
        new.call(new.s['h1_nes_jit_compile'], 0x9002, 0xa9, page, 0)
        before = new.native
        prepare()
        run(code, cycles=65, encrypted=0, label='native unknown IO replaces APU deadline')
        assert new.native > before and observations[0] == observations[1] and observations[0]
        for c, (slot, value) in zip(cores, saved):
            c.m.write(slot, value)
        checks.append(f'mapper {mapper}: shared C/native deadline invalidated by real frame/DMC events and opaque native writes')
        for c in cores:
            c.close()
    report = {'ok':True, 'cases':cases, 'bda_sha256':hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'reference':'same artifact, public JIT lifecycle closed; original SoundCPUHook interpreter',
              'checks':checks}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
