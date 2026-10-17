"""Execute the bank cache's ownership/invalidation boundaries on actual MIPS.

The reference is the same pinned interpreter with its JIT closed through the
public lifecycle API. Only original diagnostic PRG data, mapper page pointers,
and deliberately injected failure/boundary state are changed; no executable
host instructions are patched. Run this once for each 1/4 MiB candidate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_smoke import Core, ROOT
from jit_event_smoke import snapshot, put
from make_test_rom import make_rom


def u32(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def counters(c):
    return struct.unpack('<8I', c.m.read(c.s['h1_jit_stats'], 32))


def banks(c):
    return struct.unpack('<8I', c.m.read(c.s['h1_jit_bank_stats'], 32))


def entry(c, pc):
    base = u32(c, 'h1_jit_entries')
    return struct.unpack('<IIIBBBB', c.m.read(base + pc * 16, 16))


class Pair:
    def __init__(self, args, mapper):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        self.mapper = mapper
        self.old = Core(args.bda, args.elf, args.nm, bytes(rom))
        self.new = Core(args.bda, args.elf, args.nm, bytes(rom))
        self.old.call(self.old.s['h1_nes_jit_close'])
        self.old.arena = None
        assert not u32(self.old, 'h1_jit_entries')
        assert u32(self.new, 'h1_jit_entries')
        self.original = {c: [struct.unpack('<I', c.m.read(c.s['Page'] + i * 4, 4))[0]
                             for i in range(32)] for c in self.cores}
        self.cases = 0
        self.checks = []

    @property
    def cores(self):
        return (self.old, self.new)

    def map(self, pc, bank):
        # Two real physical PRG pages. Mapping the same page at a different CPU
        # address changes its biased Page pointer, as in the actual mapper.
        for c in self.cores:
            physical = self.original[c][18] + 0x9000 + bank * 0x800
            c.m.word(c.s['Page'] + (pc >> 11) * 4,
                     (physical - (pc & ~0x7ff)) & 0xffffffff)

    def clock(self):
        for c in self.cores:
            put(c, 'fhcnt', 'h1_apu_fhcnt', 1000000)
            put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)
            put(c, 'DMCSize', 'h1_apu_dmcsize', 0)
            put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
            c.m.word(c.s['timestamp'], 0)
            c.m.word(c.s['sound_timestamp'], 0)
            if self.mapper == 19:
                put(c, 'IRQa', 'h1_namco_irq_enabled', 0, 1)
                put(c, 'IRQCount', 'h1_namco_irq_count', 0, 2)

    def run(self, pc, code, cycles=7, *, force=False, label=''):
        # Seven PPU cycles give 112 budget units, just above the existing
        # >96 native-admission gate. One-cycle calls cannot heat or enter JIT.
        self.clock()
        regs = [35, 254, 128, 240, 4, 4]
        ram = bytes((i * 37 + 11) & 255 for i in range(2048))
        first = self.old.run(pc, code, regs, ram, cycles)
        second = self.new.run(pc, code, regs, ram, cycles, force=force)
        assert first == second, (label, hex(pc), first[:28].hex(), second[:28].hex())
        one, two = snapshot(self.old), snapshot(self.new)
        assert one == two, (label, 'serialized state', len(one), len(two),
                            [(i, x, y) for i, (x, y) in enumerate(zip(one, two)) if x != y][:16])
        self.cases += 1

    def warm(self, pc, code, count=8):
        for _ in range(count):
            self.run(pc, code, label='admission')

    def compile(self, pc, code):
        self.warm(pc, code)
        result = entry(self.new, pc)
        assert result[0] and result[2] == u32(self.new, 'h1_jit_epoch'), (hex(pc), result)
        return result[0]

    def invalidate(self):
        self.new.call(self.new.s['h1_nes_jit_invalidate'])

    def close(self):
        self.old.close()
        self.new.close()


def boundary_cases(a, mapper):
    p = Pair(a, mapper)
    n = p.new
    pc = 0x9000
    code_a = bytes.fromhex('a9 23 aa e8 86 10 4c 00 90')
    code_b = bytes.fromhex('a9 91 aa e8 86 10 4c 00 90')
    p.map(pc, 0)
    before_short = counters(n)
    native_short = n.native
    p.run(pc, code_a, 1, label='single-op budget remains interpreter-only')
    assert counters(n) == before_short and n.native == native_short
    assert not entry(n, pc)[0] and entry(n, pc)[4] == 0
    p.checks.append('One-cycle slice matches interpreter and does not heat or enter native cache')
    p.map(pc, 0); run_a = p.compile(pc, code_a)
    p.map(pc, 1); run_b = p.compile(pc, code_b)
    assert run_a != run_b
    compiled = counters(n)[0]
    reused = banks(n)[5]
    for bank, code, pointer in ((0, code_a, run_a), (1, code_b, run_b),
                                (0, code_a, run_a), (1, code_b, run_b)):
        p.map(pc, bank)
        p.run(pc, code, label='A/B/A physical page reuse')
        assert entry(n, pc)[0] == pointer
        assert counters(n)[0] == compiled, 'Previously compiled bank recompiled'
    assert banks(n)[5] >= reused + 4
    p.checks.append('A/B/A same PC and first opcode, different operands: exact native pointer reused')

    # Same physical bytes at different CPU addresses must not reuse a native
    # entry with embedded PC/branch targets from the other mapping.
    for address in (0x9200, 0xa200):
        p.map(address, 0)
        p.compile(address, bytes.fromhex('a9 31 aa e8 d0 fa'))
        p.run(address, bytes.fromhex('a9 31 aa e8 d0 fa'), 96,
              label='same physical page at distinct CPU PC')
    assert entry(n, 0x9200)[0] != entry(n, 0xa200)[0]
    p.checks.append('Same physical bank at different CPU addresses retains PC and branch semantics')

    # A later-byte mutation invalidates the sole live owner. Returning through
    # another bank must not resurrect the stale native copy from L2.
    p.invalidate(); p.map(pc, 0); stale = p.compile(pc, code_a)
    p.map(pc, 1); p.compile(pc, code_b)
    p.map(pc, 0); p.run(pc, code_a)
    mutated = bytes.fromhex('a9 5c aa e8 86 10 4c 00 90')
    before = counters(n)[4]
    p.run(pc, mutated, label='mutated operand guard')
    assert counters(n)[4] == before + 1
    for _ in range(12):
        p.map(pc, 1); p.run(pc, code_b)
        p.map(pc, 0); p.run(pc, mutated)
        assert entry(n, pc)[0] != stale or entry(n, pc)[2] != u32(n, 'h1_jit_epoch')
    assert counters(n)[4] == before + 1, 'L2 repeatedly resurrected a stale operand specialization'
    assert entry(n, pc)[0]
    p.checks.append('Later operand mutation cannot be resurrected after a bank round trip')

    # A chain can encounter a different bank before the C dispatcher has seen
    # the new mapping. Its old, still-valid version must survive the page miss.
    p.invalidate()
    first_pc = 0xc000
    first = bytes.fromhex('a9 11 aa 4c 00 90')
    last_a = bytes.fromhex('a9 23 85 10 4c 00 c0')
    last_b = bytes.fromhex('a9 91 85 10 4c 00 c0')
    for c in p.cores:
        c.m.word(c.s['Page'] + (first_pc >> 11) * 4, p.original[c][first_pc >> 11])
    p.compile(first_pc, first)
    p.map(pc, 0); chain_a = p.compile(pc, last_a)
    p.map(pc, 1); chain_b = p.compile(pc, last_b)
    p.map(pc, 0); p.run(pc, last_a)
    deopts = counters(n)[4]
    links = u32(n, 'h1_jit_links')
    for bank, code, pointer in ((1, last_b, chain_b), (0, last_a, chain_a)):
        p.map(pc, bank)
        for c in p.cores:
            c.prg(pc, code)
        p.run(first_pc, first, 96, label='native chain page miss')
        assert entry(n, pc)[0] == pointer, 'Chain page miss discarded the still-valid old bank'
    assert u32(n, 'h1_jit_links') > links
    assert counters(n)[4] == deopts
    p.checks.append('Native chain page miss preserves both banks and executes equivalent state')

    # Same chain, but the target bytes actually changed: it must evict target,
    # not source, and never revive the invalid version during another swap.
    first_run = entry(n, first_pc)[0]
    before = counters(n)[4]
    p.map(pc, 0)
    last_mutated = bytes.fromhex('a9 62 85 10 4c 00 c0')
    for c in p.cores:
        c.prg(pc, last_mutated)
    p.run(first_pc, first, 96, label='native chain stale target')
    assert counters(n)[4] > before
    assert (entry(n, first_pc)[0] == first_run and
            entry(n, first_pc)[2] == u32(n, 'h1_jit_epoch')), 'Target stale guard evicted source owner'
    for _ in range(10):
        p.map(pc, 1); p.run(pc, last_b)
        p.map(pc, 0); p.run(pc, last_mutated)
    after = counters(n)[4]
    for _ in range(3):
        p.map(pc, 1); p.run(pc, last_b)
        p.map(pc, 0); p.run(pc, last_mutated)
    assert counters(n)[4] == after
    p.checks.append('Chained stale target evicts the target owner without poisoning source/L2')

    # Changing a read handler deoptimizes at the entry guard before that later
    # read executes. Refused recompilation is remembered per bank.
    p.invalidate(); guard_pc = 0x9400
    guard_code = bytes.fromhex('a9 11 ad 00 10 aa 4c 00 94')
    p.map(guard_pc, 0); p.compile(guard_pc, guard_code)
    saved = {}
    for c in p.cores:
        saved[c] = struct.unpack('<I', c.m.read(c.s['ARead'] + 0x1000 * 4, 4))[0]
        c.m.word(c.s['ARead'] + 0x1000 * 4, c.stub(0x80001b80, lambda *args: 0x42))
    deopts = counters(n)[4]
    p.run(guard_pc, guard_code, label='handler guard')
    assert counters(n)[4] == deopts + 1
    for _ in range(12):
        p.map(guard_pc, 1); p.run(guard_pc, guard_code)
        p.map(guard_pc, 0); p.run(guard_pc, guard_code)
    assert counters(n)[4] == deopts + 1
    attempts = counters(n)[2]
    for _ in range(12):
        p.map(guard_pc, 1); p.run(guard_pc, guard_code, 48)
        p.map(guard_pc, 0); p.run(guard_pc, guard_code, 48)
    # Longer runs can admit other PCs, so only check the rejected primary entry.
    assert entry(n, guard_pc)[4] == 255 and not entry(n, guard_pc)[0]
    assert counters(n)[2] >= attempts
    for c in p.cores:
        c.m.word(c.s['ARead'] + 0x1000 * 4, saved[c])
    p.checks.append('Read-handler guard and rejected versions survive bank swaps without stale resurrection')

    p.invalidate(); p.map(pc, 0); p.compile(pc, code_a)
    deopts = counters(n)[4]
    for c in p.cores:
        c.m.word(c.s['overclocked'], 1)
    p.run(pc, code_a, label='overclock specialization guard')
    assert counters(n)[4] == deopts + 1
    for bank, code in ((1, code_b), (0, code_a)):
        p.map(pc, bank); p.compile(pc, code)
        p.run(pc, code, 96)
    assert counters(n)[4] == deopts + 1
    for c in p.cores:
        c.m.word(c.s['overclocked'], 0)
    p.checks.append('Overclock specialization changes deopt once and preserve interpreter timing')

    p.invalidate(); p.map(pc, 0); p.compile(pc, code_a)
    before = n.native
    for c in p.cores:
        c.m.write(c.s['PRGIsRAM'] + (pc >> 11), b'\x01')
    p.run(pc, mutated, 96, label='ROM mapped as writable RAM')
    assert n.native == before
    for c in p.cores:
        c.m.write(c.s['PRGIsRAM'] + (pc >> 11), b'\x00')
    deopts = counters(n)[4]
    p.run(pc, mutated, label='RAM mapped back as ROM with modified bytes')
    assert counters(n)[4] == deopts + 1
    p.checks.append('ROM/RAM transitions run interpreter while writable and revalidate changed bytes')

    # Preserve cold heat and a permanent rejection independently for each bank.
    p.invalidate(); cold_pc = 0x9600
    for bank in (0, 1):
        p.map(cold_pc, bank); p.warm(cold_pc, code_a, 3)
    p.map(cold_pc, 0); p.run(cold_pc, code_a)
    assert entry(n, cold_pc)[4] == 4 and not entry(n, cold_pc)[0]
    p.warm(cold_pc, code_a, 4); assert entry(n, cold_pc)[0]
    p.invalidate(); rejected = bytes.fromhex('ea 00')
    p.map(cold_pc, 0); p.warm(cold_pc, rejected)
    assert entry(n, cold_pc)[4] == 255 and not entry(n, cold_pc)[0]
    p.map(cold_pc, 1); p.compile(cold_pc, code_b)
    attempts = counters(n)[2]
    for _ in range(12):
        p.map(cold_pc, 0); p.run(cold_pc, rejected)
        p.map(cold_pc, 1); p.run(cold_pc, code_b)
    assert counters(n)[2] == attempts, 'Rejected bank retried after each swap'
    assert banks(n)[6] > 0
    p.checks.append('Cold heat progresses across swaps; rejected bank does not repeatedly compile')

    # Deliberately overflow one four-way L2 set with six valid versions. Hash
    # knowledge only selects the adversarial input; correctness is checked by
    # executing every surviving/reheated version against the interpreter.
    p.invalidate()
    physical = p.original[n][18] + 0x9000
    groups = {}
    collision_pcs = None
    for address in range(0x8000, 0x10000):
        offset = address & 0x7ff
        if offset > 0x7e0:
            continue
        page = (physical - (address & ~0x7ff)) & 0xffffffff
        value = (page >> 11) ^ (address * 0x9e37)
        bucket = (value ^ (value >> 11)) & 2047
        group = groups.setdefault(bucket, [])
        if all(abs(offset - (previous & 0x7ff)) >= 16 for previous in group):
            group.append(address)
        if len(group) == 6:
            collision_pcs = group
            break
    assert collision_pcs, 'Unable to create six non-overlapping colliding versions'
    collision_code = bytes.fromhex('a9 36 aa ea ea 00')
    evictions = banks(n)[4]
    for address in collision_pcs:
        p.map(address, 0); p.compile(address, collision_code)
        p.map(address, 1); p.run(address, collision_code)
    assert banks(n)[4] > evictions, 'Set-associative eviction was not exercised'
    reheated = 0
    for address in collision_pcs:
        p.map(address, 0); p.run(address, collision_code, label='colliding bank version recovery')
        if not entry(n, address)[0]:
            reheated += 1
            p.warm(address, collision_code, 7)
        assert entry(n, address)[0]
        p.run(address, collision_code, 12, label='colliding bank native execution')
    assert reheated, 'No evicted version followed the normal admission path'
    p.checks.append('Six colliding versions overflow a four-way L2 set; eviction safely reheats exact code')

    # Invalidating via reset and by epoch overflow must erase both cache levels.
    for overflow in (False, True):
        if overflow:
            # Populate epoch 1 deliberately, so the second wrap would make
            # stale tags current again if either cache level escaped clearing.
            n.m.word(n.s['h1_jit_epoch'], 0xffffffff)
        p.invalidate(); p.map(pc, 0); p.compile(pc, code_a)
        p.map(pc, 1); p.compile(pc, code_b)
        if overflow:
            n.m.word(n.s['h1_jit_epoch'], 0xffffffff)
            p.invalidate()
            assert u32(n, 'h1_jit_epoch') == 1
        else:
            for c in p.cores:
                c.call(c.s['X6502_Power'])
        for bank, code in ((0, code_a), (1, code_b)):
            p.map(pc, bank)
            native = n.native
            p.run(pc, code, label='epoch/reset invalidates both cache levels')
            assert n.native == native and not entry(n, pc)[0]
            p.warm(pc, code, 7); assert entry(n, pc)[0]
    p.checks.append('CPU reset and 32-bit epoch wrap prohibit revival of old native code')

    # Inject only the arena-full boundary, then perform a real compiler reclaim.
    # This avoids thousands of redundant compilations in the 4 MiB test while
    # exercising the actual reclamation, epoch changes, and subsequent execution.
    p.invalidate(); p.map(pc, 0); p.compile(pc, code_a)
    p.map(pc, 1); p.compile(pc, code_b)
    wraps = u32(n, 'h1_jit_wraps')
    n.m.word(n.s['used_bytes'], u32(n, 'cache_bytes'))
    p.run(0xc100, bytes.fromhex('a9 82 aa ea 00'), force=True, label='arena reclamation')
    assert u32(n, 'h1_jit_wraps') == wraps + 1
    for bank, code in ((0, code_a), (1, code_b)):
        p.map(pc, bank); native = n.native
        p.run(pc, code, label='reclaimed arena cannot recover stale native')
        assert n.native == native and not entry(n, pc)[0]
        p.warm(pc, code, 7); assert entry(n, pc)[0]
    p.checks.append('Actual arena reclaim invalidates L1/L2 before overwritten native bytes can execute')

    # Allocation failure must preserve a working interpreter; a later successful
    # open gets empty cache levels and executes native code again.
    final_bank_counters = banks(n)
    final_jit_counters = counters(n)
    n.call(n.s['h1_nes_jit_close']); n.arena = None
    allocator = n.m.alloc
    address = next(address for address, fn in n.m.callbacks.items() if fn == allocator)
    n.m.callbacks[address] = lambda *args: 0
    n.call(n.s['h1_nes_jit_open'], mapper)
    assert not u32(n, 'h1_jit_entries') and not u32(n, 'cache')
    # Fail the JIT allocation only. State serialization itself allocates a
    # memstream, which must remain available to make this differential valid.
    n.m.callbacks[address] = allocator
    native = n.native
    p.map(pc, 0); p.run(pc, code_a, 96, label='allocation failure interpreter fallback')
    assert n.native == native
    n.call(n.s['h1_nes_jit_open'], mapper)
    cache = u32(n, 'cache'); size = u32(n, 'cache_bytes')
    n.arena = (cache, cache + size)
    assert cache and not entry(n, pc)[0]
    p.compile(pc, code_a); assert n.native > native
    p.checks.append('Close/reopen and failed allocation fall back safely; fresh open recompiles')
    result = {'mapper': mapper, 'cases': p.cases, 'cache_bytes': size,
              'checks': p.checks, 'native_block_entries': n.native,
              'bank_counters_before_reopen': dict(zip(
                  ('lookup', 'hit', 'miss', 'store', 'eviction', 'native_reuse',
                   'reject_reuse', 'epoch_miss'), final_bank_counters)),
              'jit_counters_before_reopen': dict(zip(
                  ('compiled', 'hit', 'miss', 'native_ops', 'deopt', 'reject',
                   'invalidations', 'max_words'), final_jit_counters))}
    p.close()
    return result


def frame_cases(a, mapper, count):
    p = Pair(a, mapper)
    streams = {c: [] for c in p.cores}
    videos = {c: [] for c in p.cores}
    for c in p.cores:
        def audio(ptr, length, *args, core=c):
            streams[core].append(core.m.read(ptr, length * 4))
            return length
        def video(ptr, width, height, pitch, *args, core=c):
            videos[core].append(b''.join(core.m.read(ptr + y * pitch, width * 2)
                                        for y in range(height)) if ptr else b'')
            return 0
        c.call(c.core[3], c.stub(0x80001b20, audio))
        c.call(c.core[2], c.stub(0x80001b00, video))
    for frame in range(count):
        for c in p.cores:
            c.call(c.core[10])
        assert snapshot(p.old) == snapshot(p.new), (mapper, frame, 'complete frame state')
        assert streams[p.old] == streams[p.new], (mapper, frame, 'PCM')
        assert videos[p.old] == videos[p.new], (mapper, frame, 'video')
    pcm = b''.join(streams[p.new])
    assert len(pcm) and any(pcm), 'Tone/DMC fixture produced no nonzero PCM'
    assert p.new.native > 0
    result = {'mapper': mapper, 'frames': count, 'pcm_bytes': len(pcm),
              'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
              'native_block_entries': p.new.native,
              'checks': ['Every frame: exact serialized state, video and nonzero source PCM vs interpreter']}
    p.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--expect-cache-mib', type=int, choices=(1, 4, 8), required=True)
    parser.add_argument('--frames', type=int, default=16)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-bank-cache.json')
    a = parser.parse_args()
    boundaries = []
    for mapper in (0, 19):
        boundaries.append(boundary_cases(a, mapper))
        print(f'PASS bank boundaries mapper={mapper} cases={boundaries[-1]["cases"]}', flush=True)
    assert all(r['cache_bytes'] == a.expect_cache_mib * 1024 * 1024 for r in boundaries)
    frames = []
    for mapper in (0, 19):
        frames.append(frame_cases(a, mapper, a.frames))
        print(f'PASS complete frames mapper={mapper} frames={a.frames}', flush=True)
    result = {'ok': True, 'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'reference': 'same pinned interpreter with JIT closed via public lifecycle API',
              'cases': sum(r['cases'] for r in boundaries), 'boundaries': boundaries, 'frames': frames}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
