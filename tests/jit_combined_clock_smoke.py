"""Actual MIPS C-fallback mapper/APU ordering and shared-deadline regression.

The reference closes the same artifact's JIT and runs its original interpreter.
Mode 13 keeps opcodes unchanged while forbidding native execution. Only test ROM
state and diagnostic bus/mapper callbacks are supplied; core code is untouched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_A0
from jit_smoke import Core, ROOT
from jit_event_smoke import put, snapshot
from jit_contract_smoke import frame_packet
from make_test_rom import make_rom


def word(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def count(c):
    return struct.unpack('<H', c.m.read(c.s['h1_namco_irq_count'], 2))[0]


def observed(c):
    return b''.join(c.m.read(c.s[name], size) for name, size in (
        ('X', 28), ('timestamp', 4), ('sound_timestamp', 4), ('h1_apu_clock', 13),
        ('h1_namco_irq_count', 2), ('h1_namco_irq_enabled', 1), ('overclocked', 4)))


class Pair:
    def __init__(self, args, mapper=19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        self.cores = [Core(args.bda, args.elf, args.nm, bytes(rom)) for _ in range(2)]
        self.cores[0].call(self.cores[0].s['h1_nes_jit_close'])
        self.cores[0].arena = None
        self.mapper = mapper
        self.cases = 0

    def prepare(self, counter=0, enabled=1, frame=1000000, dmc=1000000,
                dma=0, size=0, scope=False, overclocked=0):
        for c in self.cores:
            c.call(c.s['h1_nes_jit_frame_end'])
            c.m.word(c.s['MapIRQHook'], c.s['NamcoIRQHook'] if self.mapper == 19 else 0)
            for name in ('PPU_hook', 'InputScanlineHook', 'GameHBIRQHook', 'GameHBIRQHook2'):
                c.m.word(c.s[name], 0)
            c.m.word(c.s['overclocked'], overclocked)
            c.m.word(c.s['overclock_enabled'], 0)
            c.m.word(c.s['timestamp'], 0)
            c.m.word(c.s['sound_timestamp'], 0)
            put(c, 'IRQCount', 'h1_namco_irq_count', counter, 2)
            put(c, 'IRQa', 'h1_namco_irq_enabled', enabled, 1)
            put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
            put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
            put(c, 'DMCSize', 'h1_apu_dmcsize', size)
            put(c, 'DMCHaveDMA', 'h1_apu_have_dma', dma, 1)
            c.m.word(c.s['DMCAddress'], 0x4000)
            c.m.write(c.s['DMCFormat'], b'\x80')
            c.m.write(c.s['DMCHaveSample'], b'\0')
            c.m.write(c.s['DMCBitCount'], b'\x04')
            if scope:
                c.call(c.s['h1_nes_jit_frame_begin'])
        if scope:
            assert word(self.cores[1], 'h1_jit_contract_frame') == 1

    def check(self, label):
        old, new = self.cores
        assert observed(old) == observed(new), (label, 'CPU/APU/mapper/timestamps')
        assert snapshot(old) == snapshot(new), (label, 'serialized state')
        assert old.m.read(old.s['RAM'], 2048) == new.m.read(new.s['RAM'], 2048), label
        assert old.m.read(old.s['Wave'], 2560 * 4) == new.m.read(new.s['Wave'], 2560 * 4), (label, 'PCM accumulation')
        assert struct.unpack('<8I', new.m.read(new.s['h1_jit_stats'], 32))[3] == 0, 'Native path must remain unused'
        self.cases += 1

    def run(self, label, code=b'\x08\xea\xea', cycles=1, carry=0):
        for c in self.cores:
            c.run(0x9000, code, [35, 254, 128, 240, 4, 4], bytes(2048),
                  cycles=cycles, encrypted=13, tcount=carry & 0xffffffff)
        self.check(label)

    def close(self):
        for c in self.cores:
            c.m.word(c.s['overclocked'], 0)
            c.m.word(c.s['MapIRQHook'], c.s['NamcoIRQHook'] if self.mapper == 19 else 0)
            c.close()


def namco_boundaries(args):
    p = Pair(args)
    for enabled in (0, 1):
        for initial in (0, 0x7ff9, 0x7ffa, 0x7ffb, 0x7ffc, 0x7ffd,
                        0x7ffe, 0x7fff, 0x8000, 0xfffc, 0xfffd, 0xfffe, 0xffff):
            for carry in (-3, 0, 2):
                p.prepare(initial, enabled)
                label = ('Namco single PHP', enabled, initial, carry)
                p.run(label, carry=carry)
                value = ((initial + 3 + carry) & 65535) if enabled else initial
                fires = enabled and value >= 0x7fff
                expected = 0x7fff if fires else value
                for c in p.cores:
                    assert count(c) == expected, (label, count(c), expected)
                    assert c.m.read(c.s['h1_namco_irq_enabled'], 1)[0] == (0 if fires else enabled)
                    irq = struct.unpack('<I', c.m.read(c.s['X'] + 20, 4))[0]
                    assert bool(irq & 1) == bool(fires), (label, irq)
    loop = bytes.fromhex('08 48 68 28 d0 fa')
    for initial in (0, 0x7f80, 0x7ff0, 0x7fff, 0xfff0):
        for scope in (False, True):
            p.prepare(initial, scope=scope, frame=481, dmc=13)
            p.run(('multi-op initial', initial, scope), loop, cycles=70)
            for step in range(4):
                for c in p.cores:
                    c.call(c.s['X6502_Run'], 70)
                p.check(('multi-op continuation', initial, scope, step))
    # Actual mapper register writes enable/disable IRQ inside the C span.
    for value in (0, 0x80, 0xff):
        p.prepare(0x7ff0, enabled=0, scope=True)
        code = bytes.fromhex('08 48 a9') + bytes([value]) + bytes.fromhex('8d 00 58 68 28 ea ea ea')
        p.run(('Namco register mutation', value), code, cycles=65)
    total = p.cases
    p.close()
    return total


def simultaneous_events(args):
    p = Pair(args)
    examples = []
    for carry in (0, 2):
        temp = 3 + carry
        for dma in (0, 1):
            p.prepare(0x7fff - temp, frame=temp * 48, dmc=temp, size=1, dma=dma)
            traces, hooks, saved = [[], []], [], []
            for index, c in enumerate(p.cores):
                c.m.write(c.s['fcnt'], b'\x03')
                c.m.write(c.s['IRQFrameMode'], b'\0')
                trace = traces[index]

                def record(kind, value, core=c, log=trace):
                    log.append((kind, value, word(core, 'timestamp'),
                                struct.unpack('<3i', core.m.read(core.s['h1_apu_clock'], 12)),
                                count(core), struct.unpack('<I', core.m.read(core.s['X'] + 20, 4))[0]))

                def observe(uc, address, size, unused, core=c, emit=record):
                    names = {core.s['X6502_IRQBegin']: 'irq', core.s['FCEU_SoundCPUHook']: 'sound',
                             core.s['FrameSoundUpdate']: 'frame'}
                    emit(names[address], uc.reg_read(UC_MIPS_REG_A0) if names[address] != 'frame' else 0)

                for name in ('X6502_IRQBegin', 'FCEU_SoundCPUHook', 'FrameSoundUpdate'):
                    hooks.append((c, c.m.uc.hook_add(UC_HOOK_CODE, observe, begin=c.s[name], end=c.s[name])))

                def read_dma(address, *unused, emit=record):
                    emit('dma', address)
                    return 0x80

                slot = c.s['ARead'] + 0xc000 * 4
                saved.append((slot, c.m.read(slot, 4)))
                c.m.word(slot, c.stub(0x80001b80, read_dma))
                c.m.uc.ctl_flush_tb()
            p.run(('simultaneous mapper/frame/DMC', carry, dma), carry=carry)
            assert traces[0] == traces[1], (carry, dma, traces)
            events = [(kind, value) for kind, value, *unused in traces[0]]
            expected = [('irq', 1), ('sound', temp), ('frame', 0), ('irq', 0x200)]
            if not dma:
                expected += [('dma', 0xc000)] * 4 + [('irq', 0x100)]
            assert events == expected, (carry, dma, events)
            examples.append({'carry': carry, 'dma_already_buffered': dma, 'events': traces[0]})
            for c, hook in hooks:
                c.m.uc.hook_del(hook)
            for c, (slot, value) in zip(p.cores, saved):
                c.m.write(slot, value)
    total = p.cases
    p.close()
    return total, examples


def unknown_mapper(args):
    p = Pair(args)
    for initial_overclocked in (0, 1):
        for changed in (0, 1):
            p.prepare(frame=1, dmc=1, size=1, overclocked=initial_overclocked)
            traces = [[], []]
            for index, c in enumerate(p.cores):
                def mutate(temp, *unused, core=c, log=traces[index], value=changed):
                    log.append((temp, observed(core)))
                    core.m.word(core.s['overclocked'], value)
                    core.m.word(core.s['MapIRQHook'], core.s['NamcoIRQHook'])
                    return 0
                c.m.word(c.s['MapIRQHook'], c.stub(0x80001b80, mutate))
            p.run(('unknown mapper changes overclocked', initial_overclocked, changed))
            assert traces[0] == traces[1] and len(traces[0]) == 1
            for c in p.cores:
                assert word(c, 'overclocked') == changed
                size = struct.unpack('<i', c.m.read(c.s['h1_apu_clock'] + 8, 4))[0]
                assert size == (1 if changed else 0), (initial_overclocked, changed, size)

    # MapIRQ is installed by a real opaque operand read. Its later callback
    # recursively executes actual MIPS X6502_Run, then changes event counters.
    for scope in (False, True):
        p.prepare(scope=scope, enabled=0)
        traces, saved = [[], []], []
        for index, c in enumerate(p.cores):
            c.prg(0x9200, bytes.fromhex('08 48 68 28 4c 00 92'))
            log = traces[index]

            def before(temp, *unused, core=c, trace=log):
                trace.append(('before', temp, observed(core)))
                core.m.word(core.s['MapIRQHook'], 0)
                core.m.write(core.s['X'] + 4, struct.pack('<H', 0x9200))
                return 0

            def after(*unused, core=c, trace=log):
                trace.append(('after', observed(core)))
                put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
                put(core, 'IRQCount', 'h1_namco_irq_count', 0x7ffd, 2)
                put(core, 'IRQa', 'h1_namco_irq_enabled', 1, 1)
                core.m.word(core.s['MapIRQHook'], core.s['NamcoIRQHook'])
                return 0

            c.stub(0x80001b80, before)
            c.stub(0x80001ba0, after)
            jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
            words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0, 0x34040001,
                     jal(c.s['X6502_Run']), 0, jal(0x80001ba0), 0,
                     0x8fbf001c, 0x03e00008, 0x27bd0020]
            c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))

            def install(address, *unused, core=c):
                core.m.word(core.s['MapIRQHook'], 0x80001c00)
                return 0x35

            slot = c.s['ARead'] + 0x5000 * 4
            saved.append((slot, c.m.read(slot, 4)))
            c.m.word(slot, c.stub(0x80001bc0, install))
        p.run(('nested MapIRQ callback', scope), bytes.fromhex('08 48 ad 00 50 68 28 ea ea ea'), cycles=80)
        assert len(traces[0]) == 2 and traces[0] == traces[1], (scope, traces)
        for c in p.cores:
            c.call(c.s['X6502_Run'], 20)
        p.check(('nested callback following CPU slice', scope))
        for c, (slot, value) in zip(p.cores, saved):
            c.m.write(slot, value)
    total = p.cases
    p.close()
    return total


def pending_mapper_dma(args):
    """A real mapper DMR leaves one cycle in X.tcount after mapper timing.

    The following opcode must include that cycle in its APU/IRQ event check.
    Timestamp alone has already included it, so a freshly cached absolute
    deadline cannot ignore this outstanding hook debt.
    """
    p = Pair(args)
    for event in ('frame', 'dmc', 'namco', 'all'):
        for scope in (False, True):
            p.prepare(counter=0x7ffc if event in ('namco', 'all') else 0,
                      frame=288 if event in ('frame', 'all') else 1000000,
                      dmc=6 if event in ('dmc', 'all') else 1000000, scope=scope)
            traces = [[], []]
            for index, c in enumerate(p.cores):
                trace = traces[index]

                def before(temp, *unused, core=c, log=trace):
                    log.append(('before', temp, observed(core)))
                    core.m.word(core.s['MapIRQHook'], core.s['NamcoIRQHook'])
                    return 0

                def after(*unused, core=c, log=trace):
                    log.append(('after', observed(core)))
                    assert word(core, 'X') == 1, 'Real X6502_DMR must leave pending tcount'
                    return 0

                c.stub(0x80001b80, before)
                c.stub(0x80001ba0, after)
                jal = lambda address: 0x0c000000 | ((address >> 2) & 0x03ffffff)
                words = [0x27bdffe0, 0xafbf001c, jal(0x80001b80), 0, 0x3404c000,
                         jal(c.s['X6502_DMR']), 0, jal(0x80001ba0), 0,
                         0x8fbf001c, 0x03e00008, 0x27bd0020]
                c.m.write(0x80001c00, struct.pack('<' + 'I' * len(words), *words))
                c.m.word(c.s['MapIRQHook'], 0x80001c00)
                c.run(0x9000, bytes.fromhex('08 ea ea ea'), [35, 254, 128, 240, 4, 4],
                      bytes(2048), cycles=13, encrypted=13)
            assert traces[0] == traces[1] and len(traces[0]) == 2, (event, scope, 'DMR observer')
            print('pending DMR', event, scope,
                  [list(struct.unpack('<3i', c.m.read(c.s['h1_apu_clock'], 12))) for c in p.cores],
                  [count(c) for c in p.cores], flush=True)
            p.check(('mapper real DMR pending carry', event, scope))
    total = p.cases
    p.close()
    return total


def dynamic_frame(args, mapper, kind):
    p = Pair(args, mapper)
    p.prepare(enabled=0)
    traces, packets = [], []
    for c in p.cores:
        trace = {'mapper': [], 'hook': [], 'dma': []}
        traces.append(trace)
        cpu = bytearray(28)
        struct.pack_into('<H', cpu, 4, 0x9000)
        cpu[6:12] = bytes((35, 254, 128, 240, 4, 4))
        c.m.write(c.s['X'], bytes(cpu))
        c.m.write(c.s['RAM'], bytes(2048))
        c.prg(0x9000, bytes.fromhex('ad 00 50 4c 00 92'))
        c.prg(0x9200, bytes.fromhex('08 48 68 28 4c 00 92'))
        c.m.write(c.s['encryptOpcodes'], b'\x0d')
        c.m.write(c.s['PPU'], b'\0\0')
        c.m.write(c.s['ppudead'], b'\0')
        slot = c.s['InputScanlineHook' if kind == 'input' else 'PPU_hook']

        def hook(*unused, core=c, log=trace, hook_slot=slot):
            if log['hook']:
                return 0
            log['hook'].append(word(core, 'timestamp'))
            put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
            put(core, 'DMCSize', 'h1_apu_dmcsize', 1)
            put(core, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
            if kind == 'input':
                core.m.word(hook_slot, 0)
            else:
                core.m.write(core.s['PPU'] + 1, b'\0')
            return 0

        hook_pointer = c.stub(0x80001bc0, hook)

        def mapper_callback(temp, *unused, core=c, log=trace, pointer=hook_pointer, hook_slot=slot):
            log['mapper'].append((temp, word(core, 'timestamp')))
            core.m.word(hook_slot, pointer)
            core.m.word(core.s['MapIRQHook'], core.s['NamcoIRQHook'] if mapper == 19 else 0)
            if kind == 'ppu':
                core.m.write(core.s['PPU'] + 1, b'\x08')
            return 0

        mapper_pointer = c.stub(0x80001ba0, mapper_callback)

        def install(address, *unused, core=c, pointer=mapper_pointer):
            core.m.word(core.s['MapIRQHook'], pointer)
            return 0

        def dma(address, *unused, core=c, log=trace):
            log['dma'].append((address, word(core, 'timestamp')))
            return 0x80

        c.m.word(c.s['ARead'] + 0x5000 * 4, c.stub(0x80001b80, install))
        c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001be0, dma))
        packets.append(frame_packet(c))
    assert packets[0] == packets[1], (mapper, kind, 'complete frame CPU/RAM/state/PCM/video')
    assert packets[0][3] and packets[0][4]
    assert traces[0] == traces[1] and len(traces[0]['mapper']) == len(traces[0]['hook']) == 1
    assert len(traces[0]['dma']) == 4, (mapper, kind, traces)
    p.check(('dynamic mapper installs PPU callback', mapper, kind))
    p.close()
    return {'mapper': mapper, 'hook': kind, 'events': traces[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--pending-only', action='store_true', help='Run only real mapper DMR pending-cycle cases')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-combined-clock.json')
    args = parser.parse_args()
    groups = {'pending_mapper_dma': pending_mapper_dma(args)}
    ordering, frames = [], []
    if not args.pending_only:
        groups['namco'] = namco_boundaries(args)
        groups['simultaneous'], ordering = simultaneous_events(args)
        groups['unknown_mapper'] = unknown_mapper(args)
        frames = [dynamic_frame(args, mapper, kind) for mapper in (0, 19) for kind in ('input', 'ppu')]
    groups['dynamic_frames'] = len(frames)
    checks = ['Exact CPU/RAM/APU/Namco counters, timestamp and serialized state',
              'Exact PCM accumulation',
              'Real mapper X6502_DMR leaves pending tcount for the next opcode event boundary']
    if not args.pending_only:
        checks += ['Complete-frame PCM/video',
                   '16-bit Namco wrap, threshold equality/crossing, zero temp and prior carry',
                   'Mapper IRQ before frame IRQ before four DMC reads and DMC IRQ',
                   'Unknown mapper changing overclocked and real MIPS nested CPU execution',
                   'Unknown mapper installing one-shot PPU/input callbacks across dirty CPU slices']
    report = {'ok': True, 'cases': sum(groups.values()), 'groups': groups,
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'reference': 'Same artifact original interpreter, JIT closed',
              'native_ops': 0, 'simultaneous_events': ordering, 'dynamic_frames': frames,
              'checks': checks}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
