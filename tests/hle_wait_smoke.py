"""Actual-MIPS ROM-specific wait-loop differential and admission regression.

The reference is the frozen ordinary application running its original interpreter
after the public JIT close lifecycle. The candidate executes its unmodified CPU
entry, and positive/negative HLE counter assertions prove active path coverage.
Only diagnostic guest state, data mappings and explicitly described bus hooks
are changed; no candidate host instruction or HLE formula is reproduced here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct

from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_A0

from jit_smoke import Core
from jit_event_smoke import put, snapshot
from make_test_rom import make_rom


LOOP = bytes.fromhex('a5 62 d0 fc 60')
PC = 0xfb28
ROM_SHA = '7b4fcd1c0be33e69943dcf9c81cca67bac634a9de2881aa2f3d8f9b6a3864117'


def u32(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def stats(c):
    return struct.unpack('<3I', c.m.read(c.s['h1_hle_stats'], 12))


class Pair:
    def __init__(self, args, rom=None, admitted=True):
        self.args = args
        self.rom = args.rom.read_bytes() if rom is None else rom
        self.old = Core(args.baseline_bda, args.baseline_elf, args.nm, self.rom)
        self.new = Core(args.bda, args.elf, args.nm, self.rom)
        self.old.call(self.old.s['h1_nes_jit_close'])
        self.old.arena = None
        self.cases = 0
        self.hits = 0
        self.negative = 0
        self.initial = [snapshot(c) for c in self.cores]
        assert self.initial[0] == self.initial[1], 'Initial cartridge state differs'
        self.pages = [u32_at(c, c.s['Page'] + 31 * 4) for c in self.cores]
        self.readers = [[u32_at(c, c.s['ARead'] + (PC + i) * 4) for i in range(4)]
                        for c in self.cores]
        assert bool(u32(self.new, 'h1_hle_sgz2_enabled')) == admitted, 'ROM identity gate'
        self.original_loop = [c.m.read(page + PC, len(LOOP))
                              for c, page in zip(self.cores, self.pages)]
        if admitted:
            assert self.original_loop == [LOOP, LOOP]

    @property
    def cores(self):
        return self.old, self.new

    def prepare(self, count=1800, value=1, carry=0, p=4, pi=4, irq=0,
                frame=1000000, dmc=1000000, size=0, dma=0,
                irq_count=0, irq_enabled=0, timestamp=0, pc=PC):
        for index, c in enumerate(self.cores):
            # Fresh coherent audio/PPU state prevents previous event tests from
            # becoming the next test's preconditions. The public state loader
            # itself is exercised, and its ordinary invalidation is retained.
            initial = self.initial[index]
            c.m.write(0x83200000, initial)
            assert c.call(c.core[18], 0x83200000, len(initial)) == 1
            c.call(c.s['h1_nes_jit_frame_end'])
            c.m.word(c.s['Page'] + 31 * 4, self.pages[index])
            c.m.write(self.pages[index] + PC, self.original_loop[index])
            for offset, handler in enumerate(self.readers[index]):
                c.m.word(c.s['ARead'] + (PC + offset) * 4, handler)
            c.m.write(c.s['PRGIsRAM'] + 31, b'\0')
            c.m.word(c.s['MapIRQHook'], c.s['NamcoIRQHook'])
            for name in ('PPU_hook', 'InputScanlineHook', 'GameHBIRQHook', 'GameHBIRQHook2'):
                c.m.word(c.s[name], 0)
            c.m.word(c.s['overclocked'], 0)
            c.m.word(c.s['overclock_enabled'], 0)
            c.m.write(c.s['encryptOpcodes'], b'\0')
            c.m.word(c.s['timestamp'], timestamp)
            c.m.word(c.s['sound_timestamp'], timestamp)
            cpu = bytearray(28)
            struct.pack_into('<iH', cpu, 0, carry, pc)
            cpu[6:12] = bytes([0x35, 0xfe, 0x80, 0xfd, p, pi])
            struct.pack_into('<iI', cpu, 16, count, irq)
            cpu[24] = 0x59
            c.m.write(c.s['X'], bytes(cpu))
            ram = bytearray((i * 37 + 11) & 255 for i in range(2048))
            ram[0x62] = value
            # A zero-flag exit can execute the real RTS and reach harmless
            # guest RAM code, without borrowing unknown game stack contents.
            ram[0x1fe:0x200] = bytes.fromhex('ff 02')
            ram[0x300:0x303] = bytes.fromhex('4c 00 03')
            c.m.write(c.s['RAM'], bytes(ram))
            put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
            put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
            put(c, 'DMCSize', 'h1_apu_dmcsize', size)
            put(c, 'DMCHaveDMA', 'h1_apu_have_dma', dma, 1)
            put(c, 'IRQCount', 'h1_namco_irq_count', irq_count, 2)
            put(c, 'IRQa', 'h1_namco_irq_enabled', irq_enabled, 1)
            c.m.word(c.s['DMCAddress'], 0x4000)
            c.m.write(c.s['DMCFormat'], b'\x80')
            c.m.write(c.s['DMCHaveSample'], b'\0')
            c.m.write(c.s['DMCBitCount'], b'\x04')

    def run(self, label, expected=None, cycles=0):
        before = stats(self.new)
        for c in self.cores:
            c.call(c.s['X6502_Run'], cycles)
        self.check(label)
        after = stats(self.new)
        delta = tuple((b - a) & 0xffffffff for a, b in zip(before, after))
        if expected is True:
            assert delta[0] > 0 and delta[1] > 0, (label, 'HLE never ran', delta)
            self.hits += 1
        elif expected is False:
            assert delta == (0, 0, 0), (label, 'HLE should be rejected', delta)
            self.negative += 1
        return delta

    def check(self, label):
        left, right = self.cores
        for name, size in (('X', 28), ('RAM', 2048), ('timestamp', 4),
                           ('sound_timestamp', 4), ('h1_apu_clock', 13),
                           ('h1_namco_irq_count', 2), ('h1_namco_irq_enabled', 1),
                           ('Wave', 2560 * 4)):
            a, b = left.m.read(left.s[name], size), right.m.read(right.s[name], size)
            assert a == b, (label, name, [(i, x, y) for i, (x, y)
                                        in enumerate(zip(a, b)) if x != y][:16])
        a, b = snapshot(left), snapshot(right)
        assert a == b, (label, 'serialized', [(i, x, y) for i, (x, y)
                                            in enumerate(zip(a, b)) if x != y][:16])
        self.cases += 1

    def close(self):
        for c in self.cores:
            c.close()


def u32_at(c, address):
    return struct.unpack('<I', c.m.read(address, 4))[0]


def cpu_budgets(args):
    p = Pair(args)
    rng = random.Random(0x47401915)
    budgets = sorted(set([-3, 0, 1] + [x + delta for x in
        (144, 288, 432, 576, 720, 864, 1152, 1440, 2304)
        for delta in (-2, -1, 0, 1, 2)]))
    for count in budgets:
        for carry in (0, 1, 2, 255):
            value = rng.choice((1, 127, 128, 255))
            flags, previous = rng.randrange(256), rng.randrange(256)
            p.prepare(count=count, carry=carry, value=value, p=flags, pi=previous,
                      irq_enabled=count & 1, irq_count=123)
            p.run(('CPU budget', count, carry, value, flags, previous), count > 432)
    for count in (432, 433, 10000):
        p.prepare(count=count, timestamp=0xfffffff0, value=0x80, p=0xff, pi=0)
        p.run(('timestamp wrap', count), count > 432)
    for value in (0, 1, 0x80, 0xff):
        p.prepare(count=4000, value=value)
        p.run(('wait flag', value), value != 0)
    # Starting at the branch proves the original partial slice converges on
    # the helper's LDA boundary without assuming a fixed initial PC.
    for count in (1, 48, 96, 97, 432, 900, 1800):
        p.prepare(count=count, pc=PC + 2, p=0, pi=0)
        p.run(('entry at branch', count), count > 600)
    # The next invocation keeps real residual CPU count and branch carry.
    p.prepare(count=500, value=0x80)
    p.run('first short original slice', True)
    p.run('second short original slice', None, cycles=6)
    p.run('longer original slice', True, cycles=256)
    for c in p.cores:
        c.m.write(c.s['RAM'] + 0x62, b'\0')
    p.run('flag changes between actual slices', False, cycles=256)
    result = {'cases': p.cases, 'positive': p.hits, 'negative': p.negative}
    p.close()
    return result


def events(args):
    p = Pair(args)
    # Values straddle original LDA and BNE base-clock boundaries, with and
    # without the final branch's pending extra cycle from the preceding pair.
    for carry in (0, 1, 2):
        for horizon in (1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 17, 18, 19, 31):
            for delta in (-1, 0, 1):
                p.prepare(count=2000, carry=carry, frame=max(1, horizon * 48 + delta))
                p.run(('APU frame boundary', carry, horizon, delta))
            p.prepare(count=2000, carry=carry, dmc=horizon)
            p.run(('DMC boundary', carry, horizon))
    for enabled in (0, 1):
        for count in (0, 0x7ff5, 0x7ff9, 0x7ffa, 0x7ffb, 0x7ffc,
                      0x7ffd, 0x7ffe, 0x7fff, 0x8000, 0xfffe, 0xffff):
            for carry in (0, 1, 2):
                p.prepare(count=2000, carry=carry, irq_count=count,
                          irq_enabled=enabled, p=4, pi=4)
                p.run(('Namco IRQ boundary', enabled, count, carry))
    # A real pending DMC DMA performs four bus reads. The injected bus callback
    # changes the wait flag, making a missed DMA visible in CPU execution.
    for clear_flag in (False, True):
        p.prepare(count=2000, size=1, dma=0)
        reads = [0, 0]
        saved = []
        for index, c in enumerate(p.cores):
            saved.append(u32_at(c, c.s['ARead'] + 0xc000 * 4))
            def dma_read(address, *unused, c=c, index=index):
                reads[index] += 1
                if clear_flag:
                    c.m.write(c.s['RAM'] + 0x62, b'\0')
                return 0x55
            c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001b80, dma_read))
        p.run(('real pending DMA', clear_flag), False if clear_flag else None)
        assert reads == [4, 4], reads
        for c, handler in zip(p.cores, saved):
            c.m.word(c.s['ARead'] + 0xc000 * 4, handler)
    # Use actual 6502 interrupt entry and an actual RAM handler, not a Python
    # replacement for interrupt semantics. The handler clears $62 then RTIs.
    for irq in (1, 2, 0x20, 0x40, 0x80, 0x100, 0x200, 0x800):
        for flags in (0, 4):
            p.prepare(count=2200, irq=irq, p=flags, pi=flags)
            vectors = []
            for c in p.cores:
                page = u32_at(c, c.s['Page'] + 31 * 4)
                vectors.append(c.m.read(page + 0xfffa, 6))
                c.prg(0xfffa, struct.pack('<3H', 0x400, 0x400, 0x400))
                c.m.write(c.s['RAM'] + 0x400, bytes.fromhex('a9 00 85 62 40'))
            # TEMP is cleared by the original outer scheduler, so a masked
            # TEMP-only request can legitimately enter HLE later in the slice.
            p.run(('interrupt entry', irq, flags), None if irq == 0x800 else False)
            for c, data in zip(p.cores, vectors):
                c.prg(0xfffa, data)
    result = {'cases': p.cases, 'positive': p.hits, 'negative': p.negative}
    p.close()
    return result


def admission(args):
    p = Pair(args)
    for name, value, size in (('overclocked', 1, 4), ('overclock_enabled', 1, 4),
                             ('encryptOpcodes', 13, 1)):
        p.prepare(count=1800)
        for c in p.cores:
            c.m.write(c.s[name], value.to_bytes(size, 'little'))
        p.run(('runtime policy', name), False)
    # KIL/jammed is not created by host-code patching: mark the real CPU's
    # existing jam state and verify the candidate preserves original behavior.
    p.prepare(count=1800)
    for c in p.cores:
        c.m.write(c.s['X'] + 12, b'\1')
    p.run('jammed CPU', False)
    for hook in ('PPU_hook', 'InputScanlineHook', 'GameHBIRQHook', 'GameHBIRQHook2'):
        p.prepare(count=1800)
        for c in p.cores:
            c.m.word(c.s[hook], c.stub(0x80001ba0, lambda *unused: 0))
        p.run(('observer installed', hook), False)
    for uninstall in (False, True):
        p.prepare(count=1800)
        calls = [0, 0]
        for index, c in enumerate(p.cores):
            def mapper(cycles, *unused, c=c, index=index):
                calls[index] += 1
                c.m.write(c.s['RAM'] + 0x62, b'\0')
                if uninstall:
                    c.m.word(c.s['MapIRQHook'], c.s['NamcoIRQHook'])
                return 0
            c.m.word(c.s['MapIRQHook'], c.stub(0x80001ba0, mapper))
        p.run(('unknown mapper mutates flag', uninstall), False)
        assert calls[0] == calls[1] and calls[0] > 0, calls
    for offset in range(4):
        p.prepare(count=1800)
        reads = [0, 0]
        for index, c in enumerate(p.cores):
            def reader(address, *unused, c=c, index=index):
                reads[index] += 1
                page = u32_at(c, c.s['Page'] + (address >> 11) * 4)
                return c.m.read(page + address, 1)[0]
            c.m.word(c.s['ARead'] + (PC + offset) * 4, c.stub(0x80001ba0, reader))
        p.run(('custom opcode/operand reader', offset), False)
        assert reads[0] == reads[1] and reads[0] > 0, (offset, reads)
    # Same first opcode; change zero-page operand, branch sense or back edge.
    for offset, byte in ((1, 0x63), (2, 0xf0), (3, 0xfe)):
        p.prepare(count=1800)
        for c in p.cores:
            c.prg(PC + offset, bytes([byte]))
        p.run(('in-place later code mutation', offset, byte), False)
    p.prepare(count=1800)
    for c in p.cores:
        c.m.write(c.s['PRGIsRAM'] + 31, b'\1')
    p.run('writable PRG page', False)
    # Identical code at another physical page still must respect the admitted
    # page identity; this catches cached pointer assumptions independent of bytes.
    p.prepare(count=1800)
    for c in p.cores:
        page = u32_at(c, c.s['Page'] + 31 * 4)
        c.m.write(0x83400000, c.m.read(page + 0xf800, 2048))
        c.m.word(c.s['Page'] + 31 * 4, 0x83400000 - 0xf800)
    p.run('remapped identical-code physical bank', False)
    # Loading a coherent saved state reestablishes mapping and cannot retain
    # RAM flag/cycle values from the previous HLE invocation.
    p.prepare(count=1800, value=0x80)
    saved = [snapshot(c) for c in p.cores]
    p.run('before state restore', True)
    for c, state in zip(p.cores, saved):
        c.m.write(0x83200000, state)
        assert c.call(c.core[18], 0x83200000, len(state)) == 1
    p.run('after state restore', True)
    for c in p.cores:
        c.call(c.s['X6502_Power'])
    p.run('reset/power pending before first cycle', False)
    result = {'cases': p.cases, 'positive': p.hits, 'negative': p.negative}
    p.close()
    # A complete separate mapper-19 ROM with the same local code is excluded
    # by real loader identity, without spoofing GameInfo or private enable bits.
    rom = bytearray(make_rom(dmc=True))
    rom[6] = (rom[6] & 15) | 0x30
    rom[7] = 0x10
    prg_size = rom[4] * 16384
    offset = 16 + prg_size - 8192 + 0x1b28
    rom[offset:offset + len(LOOP)] = LOOP
    q = Pair(args, bytes(rom), admitted=False)
    q.prepare(count=1800)
    q.run('unrelated mapper19 with identical local wait loop', False)
    result['cases'] += q.cases
    result['negative'] += q.negative
    q.close()
    return result


def observables(args):
    p = Pair(args)
    for event in ('frame', 'dmc', 'namco'):
        for carry in (0, 1, 2):
            setup = {'count': 1800, 'carry': carry, 'value': 0x80,
                     'p': 0x45, 'pi': 0x12}
            if event == 'frame':
                setup['frame'] = (12 + carry) * 48
            elif event == 'dmc':
                setup['dmc'] = 12 + carry
            else:
                setup.update(irq_enabled=1, irq_count=0x7fff - 12 - carry)
            p.prepare(**setup)
            traces = [[], []]
            handles = []
            for index, c in enumerate(p.cores):
                name = 'NamcoIRQHook' if event == 'namco' else 'FCEU_SoundCPUHook'
                def observe(uc, address, size, unused, c=c, index=index):
                    cycles = uc.reg_read(UC_MIPS_REG_A0)
                    if event == 'namco':
                        count = struct.unpack('<H', c.m.read(c.s['h1_namco_irq_count'], 2))[0]
                        enabled = c.m.read(c.s['h1_namco_irq_enabled'], 1)[0]
                        occurs = enabled and ((count + cycles) & 0xffff) >= 0x7fff
                    else:
                        frame, dmc = struct.unpack('<ii', c.m.read(c.s['h1_apu_clock'], 8))
                        occurs = frame <= cycles * 48 if event == 'frame' else dmc <= cycles
                    if occurs:
                        traces[index].append((cycles, c.m.read(c.s['X'], 28).hex(),
                                              u32(c, 'timestamp'), u32(c, 'sound_timestamp')))
                handles.append((c, c.m.uc.hook_add(UC_HOOK_CODE, observe,
                                                  begin=c.s[name], end=c.s[name])))
            p.run(('event-observable intermediate CPU', event, carry), True)
            for c, handle in handles:
                c.m.uc.hook_del(handle)
            assert traces[0] and traces[0] == traces[1], (event, carry, traces)
            first_cpu = bytes.fromhex(traces[1][0][1])
            assert struct.unpack_from('<H', first_cpu, 4)[0] == PC + 2, (
                event, carry, 'first post-HLE event must observe last BNE PC', traces)
    result = {'cases': p.cases, 'positive': p.hits, 'negative': p.negative}
    p.close()
    return result


def lifecycle(args):
    p = Pair(args)
    p.prepare(count=1800)
    saved = snapshot(p.old)
    assert saved == snapshot(p.new)
    p.run('warm HLE before failed partial restore', True)
    corrupt = bytearray(saved)
    at = corrupt.index(b'RAM\0\x00\x08\x00\x00') + 8
    corrupt[at + 0x62] = 0
    # The complete CPU RAM field is restored before a later short chunk makes
    # the public loader fail. It must not cache the old nonzero wait flag.
    corrupt = bytes(corrupt[:at + 2048 + 3])
    for c in p.cores:
        c.m.write(0x83200000, corrupt)
        assert c.call(c.core[18], 0x83200000, len(corrupt)) == 0
        assert c.m.read(c.s['RAM'] + 0x62, 1) == b'\0'
    p.run('failed partial state changed wait flag', False, cycles=256)
    rom = bytearray(make_rom(dmc=True))
    rom[6] = (rom[6] & 15) | 0x30
    rom[7] = 0x10
    offset = 16 + rom[4] * 16384 - 8192 + 0x1b28
    rom[offset:offset + len(LOOP)] = LOOP
    for image, admitted, label in ((bytes(rom), False, 'different mapper19 after unload'),
                                   (args.rom.read_bytes(), True, 'original ROM after reload')):
        for c in p.cores:
            c.call(c.core[9])
            if c is p.new:
                assert u32(c, 'h1_hle_sgz2_enabled') == 0, 'Unload retained HLE identity'
            c.m.files['A:\\FC\\LOCAL.nes'] = image
            assert c.call(c.core[8], 0x83000040) == 1
            if c is p.old:
                c.call(c.s['h1_nes_jit_close'])
        assert bool(u32(p.new, 'h1_hle_sgz2_enabled')) == admitted, label
        p.initial = [snapshot(c) for c in p.cores]
        assert p.initial[0] == p.initial[1], label
        p.pages = [u32_at(c, c.s['Page'] + 31 * 4) for c in p.cores]
        p.readers = [[u32_at(c, c.s['ARead'] + (PC + i) * 4) for i in range(4)]
                     for c in p.cores]
        p.original_loop = [c.m.read(page + PC, len(LOOP)) for c, page in zip(p.cores, p.pages)]
        p.prepare(count=1800)
        p.run(label, admitted)
    p.prepare(count=1800)
    p.new.call(p.new.s['h1_nes_jit_close'])
    assert u32(p.new, 'h1_hle_sgz2_enabled') == 0
    p.run('explicit JIT close restores interpreter-only execution', False)
    result = {'cases': p.cases, 'positive': p.hits, 'negative': p.negative}
    p.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bda', 'elf', 'baseline-bda', 'baseline-elf', 'nm', 'rom', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--suites', nargs='+', choices=('cpu', 'events', 'admission', 'observables', 'lifecycle'),
                        default=('cpu', 'events', 'admission', 'observables', 'lifecycle'))
    args = parser.parse_args()
    assert hashlib.sha256(args.rom.read_bytes()).hexdigest() == ROM_SHA
    results = {}
    for name in args.suites:
        results[name] = {'cpu': cpu_budgets, 'events': events, 'admission': admission,
                         'observables': observables, 'lifecycle': lifecycle}[name](args)
        print(json.dumps({'suite': name, **results[name]}), flush=True)
    report = {
        'ok': True,
        'cases': sum(r['cases'] for r in results.values()),
        'positive_hle_cases': sum(r['positive'] for r in results.values()),
        'negative_hle_cases': sum(r['negative'] for r in results.values()),
        'suites': results,
        'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
        'baseline_sha256': hashlib.sha256(args.baseline_bda.read_bytes()).hexdigest(),
        'rom_sha256': ROM_SHA,
        'reference': 'Frozen ordinary BDA with JIT closed, actual pinned MIPS interpreter',
        'checks': ['exact CPU/RAM/timestamp/APU/Namco/Wave/serialized state',
                   'HLE counters assert accepted and rejected paths',
                   'short CPU budgets and original slice boundaries',
                   'real APU/DMC/IRQ/NMI/reset semantic paths',
                   'unknown callbacks and mutable code/mapping/handler guards',
                   'real ROM identity and save/load/reset lifecycle'],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
