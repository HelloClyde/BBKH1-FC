"""Actual MIPS LQ oscillators vs frozen scalar core, including exact phase/PCM.

Only test-owned audio state and documented register writes are supplied. No
host executable instructions, APU callbacks or DMA/IRQ behavior are replaced.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
from unicorn import UC_HOOK_BLOCK
from jit_smoke import Core, ROOT
from jit_event_smoke import put
from make_test_rom import make_rom


def word(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def sound_state(c):
    result = bytearray()
    for index in range(128):
        record = c.m.read(c.s['FCEUSND_STATEINFO'] + index * 16, 16)
        pointer, size = struct.unpack('<II', record[:8])
        if not pointer:
            break
        size &= 0xffffff
        assert 0 < size < 200000
        result += record[8:12] + struct.pack('<I', size) + c.m.read(pointer, size)
    return bytes(result) + c.m.read(c.s['Wave'], 2560 * 4) + c.m.read(c.s['X'], 28)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-bda', type=Path, default=ROOT/'build/optimization-v020-baseline/build/release-jit/H1FC.bda')
    parser.add_argument('--baseline-elf', type=Path, default=ROOT/'build/optimization-v020-baseline/build/release-jit/H1FC.elf')
    parser.add_argument('--bda', type=Path, default=ROOT/'build/test-23-jit/H1FC.bda')
    parser.add_argument('--elf', type=Path, default=ROOT/'build/test-23-jit/H1FC.elf')
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'build/verification/apu-lq-v020.json')
    a = parser.parse_args()
    old = Core(a.baseline_bda, a.baseline_elf, a.nm, make_rom(dmc=True))
    new = Core(a.bda, a.elf, a.nm, make_rom(dmc=True))
    cores = (old, new)
    rng = random.Random(2205032000)
    counts = {c: 0 for c in cores}
    for c in cores:
        def block(uc, address, size, user, core=c):
            counts[core] += size // 4
        c.m.uc.hook_add(UC_HOOK_BLOCK, block)
    costs = {}
    cases = 0
    rate_step = word(old, 'nesincsize')
    rate_time = word(old, 'soundtsinc')
    assert (rate_step, rate_time) == (word(new, 'nesincsize'), word(new, 'soundtsinc'))

    def compare(label):
        nonlocal cases
        one, two = sound_state(old), sound_state(new)
        assert one == two, (label, len(one), len(two),
                            [(i, x, y) for i, (x, y) in enumerate(zip(one, two)) if x != y][:12])
        cases += 1

    def invoke(name, *args, group=None):
        values = []
        for c in cores:
            before = counts[c]
            c.call(c.s[name], *args)
            values.append(counts[c] - before)
        if group:
            item = costs.setdefault(group, {'baseline': 0, 'candidate': 0, 'calls': 0})
            item['baseline'] += values[0]; item['candidate'] += values[1]; item['calls'] += 1
        compare((name, args, group))

    def state(seed, *, mask=None, start=None, length=None, phase=None):
        # Independent output cells initially contain real signed contributions
        # from other channels, so this checks addition as well as fresh output.
        wave = struct.pack('<2560i', *(rng.randrange(-100000, 100001) for _ in range(2560)))
        psg = bytearray(rng.randrange(256) for _ in range(16))
        psg[0] = (seed % 4) << 6
        psg[4] = ((seed // 4) % 4) << 6
        psg[1] = psg[5] = 8  # Include sweep-negate and sweep-overflow in separate cases below.
        periods = (0, 3, 4, 5, 7, 8, 15, 31, 255, 1023, 2047)
        tri = periods[seed % len(periods)]
        psg[10], psg[11] = tri & 255, tri >> 8
        psg[14] = (seed & 15) | (0x80 if seed & 16 else 0)
        if mask is None: mask = seed & 15
        if start is None: start = (0, 1, 14, 15, 16, 17, 31, 63)[seed % 8]
        if length is None: length = (0, 1, 2, 15, 16, 17, 31, 32, 127, 511, 2048)[seed % 11]
        if phase is None:
            choices = (-rate_step, -1, 0, 1, rate_step - 1, rate_step, rate_step + 1,
                       rate_step * 16 - 1, rate_step * 16, rate_step * 16 + 1, 30000000)
            phase = choices[seed % len(choices)]
        period0, period1 = (7, 8, 15, 31, 255, 1023, 2047, 2048)[seed % 8], periods[(seed // 3) % 11]
        duty = ((seed % 32) - 8, (seed // 4) % 16)  # Also exercise masked restored duty indices.
        volumes = (0, 1, 64, 128, 255, 256)
        volume = [volumes[(seed // (i + 1)) % 6] for i in range(5)]
        env = b''.join(struct.pack('<BBBBI', (seed + i * 7) & 15, (seed + i) & 3,
                                   1, (seed * 3 + i) & 15, 0) for i in range(3))
        values = {'Wave': wave, 'PSG': psg, 'EnvUnits': env,
                  'lengthcount': struct.pack('<4i', *(10 if mask & (1 << i) else 0 for i in range(4))),
                  'curfreq': struct.pack('<2i', period0, period1),
                  'RectDutyCount': struct.pack('<2i', *duty),
                  'sqacc': struct.pack('<2i', phase, phase + 1),
                  'lq_triacc': struct.pack('<i', phase), 'lq_noiseacc': struct.pack('<i', phase + 1),
                  'tristep': struct.pack('<i', seed & 31),
                  'lq_tcout': struct.pack('<I', (seed % 16) * 3),
                  'nreg': struct.pack('<H', (0, 1, 0x4000, 0x7fff, 0x1234)[seed % 5]),
                  'TriCount': bytes([seed % 3]), 'RawDALatch': bytes([(0, 1, 63, 64, 126, 127, 255)[seed % 7]]),
                  'ChannelBC': struct.pack('<5I', start, 0, start, 0, 0),
                  'sound_timestamp': struct.pack('<I', start + length),
                  'soundtsoffs': bytes(4), 'soundtsinc': struct.pack('<I', 65536),
                  'EnabledChannels': b'\x1f', 'PAL': bytes([seed & 1])}
        for c in cores:
            for name, value in values.items(): c.m.write(c.s[name], bytes(value))
            c.m.write(c.s['FSettings'] + 8, struct.pack('<5i', *volume))
        return start, start + length

    # Full channel masks, duty modes, envelope/fixed volumes, NTSC/PAL and both
    # noise taps; before/on/after output-cell and oscillator phase boundaries.
    for seed in range(352):
        state(seed)
        invoke('RDoSQLQ', group='square_boundary_random')
        invoke('RDoTriangleNoisePCMLQ', group='triangle_noise_dac_boundary_random')
    for mask in range(16):
        for phase in (-1, 0, 1, rate_step-1, rate_step, rate_step+1, rate_step*16):
            state(mask + 71, mask=mask, start=15, length=97, phase=phase)
            invoke('RDoSQLQ', group='square_boundary_random')
            invoke('RDoTriangleNoisePCMLQ', group='triangle_noise_dac_boundary_random')

    # A long stable tone measures useful batching separately from tiny or
    # intentionally pathological chunks; per-channel volume is unchanged.
    for seed in range(32):
        state(seed + 100, mask=15, start=0, length=7000, phase=1)
        for c in cores:
            c.m.write(c.s['FSettings'] + 8, struct.pack('<5i', *([256]*5)))
            c.m.write(c.s['curfreq'], struct.pack('<2i', 250 + seed, 180 + seed))
            c.m.write(c.s['PSG'] + 10, bytes([180 + seed, 0]))
            c.m.write(c.s['PSG'] + 14, bytes([10 | (0x80 if seed & 1 else 0)]))
            c.m.write(c.s['TriCount'], b'\x05')
        invoke('RDoSQLQ', group='square_stable_tone')
        invoke('RDoTriangleNoisePCMLQ', group='triangle_noise_dac_stable_tone')

    # Continue the same oscillator across arbitrary flush partitioning and
    # interleave real register writes; every call compares phases plus all PCM
    # cells, not only the final frame output.
    state(333, mask=15, start=0, length=0, phase=1)
    for c in cores: c.m.word(c.s['soundtsinc'], rate_time)
    writes = [(0x4000, 0x3f), (0x4001, 0x87), (0x4002, 0x12), (0x4003, 0x08),
              (0x4004, 0xbf), (0x4005, 0x8f), (0x4006, 0xff), (0x4007, 0x09),
              (0x4008, 0x81), (0x400a, 0x04), (0x400b, 0x08),
              (0x400c, 0x3f), (0x400e, 0x80), (0x400f, 0x08),
              (0x4011, 0x00), (0x4011, 0x7f), (0x4011, 0x40),
              (0x4010, 0x8f), (0x4012, 0x00), (0x4013, 0x00),
              (0x4015, 0x00), (0x4015, 0x1f), (0x4017, 0xc0)]
    for i in range(92):
        timestamp = (i + 1) * 113
        for c in cores: c.m.word(c.s['sound_timestamp'], timestamp)
        if i & 1:
            invoke('RDoTriangleNoisePCMLQ', group='segmented_flush')
            invoke('RDoSQLQ', group='segmented_flush')
        address, value = writes[i % len(writes)]
        for c in cores:
            handler = struct.unpack('<I', c.m.read(c.s['BWrite'] + address * 4, 4))[0]
            c.call(handler, address, value)
        compare(('register_write', timestamp, hex(address), value))

    # Actual DMA/IRQ + DAC bit transitions still use the real sound hook. This
    # supplements the separate scheduler suite rather than approximating DMC.
    for c in cores:
        c.m.word(c.s['sound_timestamp'], 11000)
        put(c, 'fhcnt', 'h1_apu_fhcnt', 96)
        put(c, 'DMCacc', 'h1_apu_dmcacc', 1)
    for cycles in (1, 2, 3, 48, 53, 54, 55, 96, 432, 1000):
        for c in cores: c.m.word(c.s['sound_timestamp'], word(c, 'sound_timestamp') + cycles)
        invoke('FCEU_SoundCPUHook', cycles, group='dmc_frame_events')
    for entry in costs.values():
        entry['instruction_reduction_percent'] = (1 - entry['candidate'] / entry['baseline']) * 100
    result = {'ok': True, 'cases': cases, 'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'baseline_bda_sha256': hashlib.sha256(a.baseline_bda.read_bytes()).hexdigest(),
              'source_step': rate_step, 'source_time_step': rate_time,
              'costs': costs,
              'checks': ['Exact complete Wave buffer and every declared APU field plus CPU state after each call',
                         'All channel masks, duty modes, NTSC/PAL noise taps, envelope/fixed and scaled volumes',
                         'Output-bin edges, negative/zero phases, simultaneous and multiple transitions per sample',
                         'Real register changes and differently partitioned flushes, including DMC DAC writes',
                         'Real frame/DMC DMA/IRQ hook boundaries; no hook replacement'],
              'limitation': 'Function MIPS instruction counts, not complete-application speed or physical H1 cycles'}
    old.close(); new.close()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
