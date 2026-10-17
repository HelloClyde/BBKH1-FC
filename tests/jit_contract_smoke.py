"""Exercise versioned JIT entry credentials on the actual MIPS implementation.

Only diagnostic ROM data, deliberately injected boundary state, and firmware
callbacks are changed. Native instructions are never patched. The interpreter
reference is the same artifact with its public JIT lifecycle closed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1

from jit_smoke import Core, ROOT
from jit_event_smoke import put, snapshot
from make_test_rom import make_rom


def u32(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def prologue_bytes(c, address):
    """Validate actual generated pin instructions; legacy entries had 14 words."""
    if 'h1_jit_native_depth' not in c.s:
        return 14 * 4
    depth = c.s['h1_jit_native_depth']
    expected = ((15 << 26) | (15 << 16) | (depth >> 16),
                (13 << 26) | (15 << 21) | (15 << 16) | (depth & 65535),
                (35 << 26) | (15 << 21) | (8 << 16),
                (9 << 26) | (8 << 21) | (8 << 16) | 1,
                (43 << 26) | (15 << 21) | (8 << 16))
    actual = struct.unpack('<5I', c.m.read(address + 14 * 4, 5 * 4))
    assert actual == expected, ('native shared-frame pin ABI', hex(address), actual, expected)
    return 19 * 4


def entry(c, pc):
    return struct.unpack('<IIIBBBB', c.m.read(u32(c, 'h1_jit_entries') + pc * 16, 16))


def stamp_address(c, pc):
    return u32(c, 'h1_jit_contract_stamps') + pc * 4


def stamp(c, pc):
    return struct.unpack('<I', c.m.read(stamp_address(c, pc), 4))[0]


def clock(c):
    put(c, 'fhcnt', 'h1_apu_fhcnt', 1000000)
    put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)
    put(c, 'DMCSize', 'h1_apu_dmcsize', 0)
    put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
    c.m.word(c.s['timestamp'], 0)
    c.m.word(c.s['sound_timestamp'], 0)
    if 'h1_namco_irq_enabled' in c.s:
        put(c, 'IRQa', 'h1_namco_irq_enabled', 0, 1)
        put(c, 'IRQCount', 'h1_namco_irq_count', 0, 2)


def native_state(c, pc, count=10000):
    cpu = bytearray(28)
    struct.pack_into('<H', cpu, 4, pc)
    cpu[6:12] = bytes((0x35, 0xfe, 0x80, 0xf0, 0x45, 0x45))
    struct.pack_into('<i', cpu, 16, count)
    cpu[24] = 0x5a
    c.m.write(c.s['X'], bytes(cpu))
    c.m.write(c.s['RAM'], bytes((i * 37 + 11) & 255 for i in range(2048)))
    clock(c)
    return bytes(cpu)


class Pair:
    def __init__(self, args, mapper):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        self.old = Core(args.bda, args.elf, args.nm, bytes(rom))
        self.new = Core(args.bda, args.elf, args.nm, bytes(rom))
        self.old.call(self.old.s['h1_nes_jit_close'])
        self.old.arena = None
        self.cases = 0
        self.checks = []

    @property
    def cores(self):
        return (self.old, self.new)

    def code(self, pc, code):
        for c in self.cores:
            c.prg(pc, code)

    def compile(self, pc, code):
        self.code(pc, code)
        c = self.new
        page = struct.unpack('<I', c.m.read(c.s['Page'] + (pc >> 11) * 4, 4))[0]
        c.call(c.s['h1_nes_jit_compile'], pc, code[0], page, 0)
        e = entry(c, pc)
        assert e[0] and e[6] == 1, (hex(pc), e)
        private = struct.unpack('<I', c.m.read(e[0] - 4, 4))[0]
        assert private != e[0] and c.arena[0] <= private < c.arena[1]
        # The native linker enters after this shared O32 prologue.
        shared = prologue_bytes(c, e[0])
        assert prologue_bytes(c, private) == shared
        assert c.m.read(e[0], shared) == c.m.read(private, shared)
        return e[0], private

    def run(self, pc, code, cycles=256, label='', setup=None, regs=None, tcount=0):
        values = []
        for c in self.cores:
            clock(c)
            if setup:
                setup(c)
            values.append(c.run(pc, code, regs or [35, 254, 128, 240, 4, 4],
                                bytes((i * 37 + 11) & 255 for i in range(2048)), cycles, tcount=tcount))
        assert values[0] == values[1], (label, values[0][:28].hex(), values[1][:28].hex())
        assert snapshot(self.old) == snapshot(self.new), (label, 'serialized state')
        assert u32(self.new, 'h1_jit_contract_epoch') == 0, (label, 'slice exit credential')
        self.cases += 1

    def close(self):
        self.old.close()
        self.new.close()


def suite(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    left, right = 0x9000, 0x9200
    code_left = bytes.fromhex('a9 23 aa 4c 00 92')
    code_right = bytes.fromhex('e8 86 10 4c 00 90')
    strict_left, fast_left = p.compile(left, code_left)
    strict_right, fast_right = p.compile(right, code_right)
    shared = prologue_bytes(n, strict_left)
    observations = []

    def observe(uc, address, size, unused):
        observations.append((address, u32(n, 'h1_jit_contract_epoch'),
                             stamp(n, left), stamp(n, right)))

    # Observe both full function and shared-frame entries without altering code.
    watched = (strict_left, fast_left, strict_right, fast_right)
    hooks = [n.m.uc.hook_add(UC_HOOK_CODE, observe, begin=address + offset,
                             end=address + offset)
             for address in watched for offset in (0, shared)]
    reuse = struct.unpack('<4I', n.m.read(n.s['h1_jit_schedule_stats'], 16))[3]
    serial = u32(n, 'h1_jit_contract_serial')
    p.run(left, code_left, 2000, 'same-slice chain reuse')
    assert u32(n, 'h1_jit_contract_serial') > serial
    assert any(address in (fast_left, fast_left + shared, fast_right, fast_right + shared)
               for address, *_ in observations), observations[:12]
    assert struct.unpack('<4I', n.m.read(n.s['h1_jit_schedule_stats'], 16))[3] > reuse
    # The private entry is a credential-checking gate and is also used when
    # validation is required. Its reuse counter proves the guarded fast path.
    assert any(active and ((address in (fast_left, fast_left + shared) and ls == active) or
                           (address in (fast_right, fast_right + shared) and rs == active))
               for address, active, ls, rs in observations)
    if mapper == 0:
        observations.clear()

        def distant_events(c):
            put(c, 'fhcnt', 'h1_apu_fhcnt', 100000000)
            put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)

        # More than CHAIN_LIMIT native instructions forces a normal return to
        # the C dispatcher while the same subinterval remains valid.
        p.run(left, code_left, 300000, 'dispatcher reuse after chain limit', setup=distant_events)
        assert any(active and ((address == fast_left and ls == active) or
                               (address == fast_right and rs == active))
                   for address, active, ls, rs in observations), 'No validated C-dispatch entry'
    for hook in hooks:
        n.m.uc.hook_del(hook)
    p.checks.append(f'Shared {shared // 4}-word prologues; native chain reuses validated private entries')
    if mapper == 0:
        p.checks.append('C dispatcher reuses the private entry after the native chain instruction limit')

    # A retained stamp never validates a later scheduling slice, including raw
    # diagnostic ROM writes that do not go through a mapper invalidation API.
    before = struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4]
    changed = bytes.fromhex('a9 71 aa 4c 00 92')
    p.run(left, changed, 2000, 'cross-slice operand mutation')
    assert struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4] > before
    p.checks.append('A later operand mutation between CPU slices is found by exact guards')

    handler_pc = 0x9400
    handler_code = bytes.fromhex('a9 11 ad 00 10 aa 4c 00 94')
    p.compile(handler_pc, handler_code)
    p.run(handler_pc, handler_code, 128, 'warm RAM read')
    saved = {}
    for c in p.cores:
        saved[c] = struct.unpack('<I', c.m.read(c.s['ARead'] + 0x1000 * 4, 4))[0]
        c.m.word(c.s['ARead'] + 0x1000 * 4, c.stub(0x80001b80, lambda *a: 0x42))
    before = struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4]
    p.run(handler_pc, handler_code, 128, 'cross-slice read-handler mutation')
    assert struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4] > before
    for c in p.cores:
        c.m.word(c.s['ARead'] + 0x1000 * 4, saved[c])
    p.checks.append('A raw read-handler mutation between slices deoptimizes before execution')

    strict, fast = p.compile(left, code_left)
    n.m.word(n.s['h1_jit_contract_epoch'], 0x1234)
    n.m.word(stamp_address(n, left), 0x1234)
    n.prg(left, bytes.fromhex('a9 99 aa 4c 00 92'))
    cpu = native_state(n, left)
    result = n.call(strict, n.s['X'], n.s['RAM'])
    assert result == (0x80000000 | left), hex(result)
    assert n.m.read(n.s['X'], 28) == cpu
    assert entry(n, left)[2] == 0
    n.m.word(n.s['h1_jit_contract_epoch'], 0)
    p.checks.append('Public strict entry rejects stale bytes even when its credential is deliberately matched')

    strict, fast = p.compile(left, code_left)
    page_address = n.s['Page'] + (left >> 11) * 4
    original_page = struct.unpack('<I', n.m.read(page_address, 4))[0]
    n.m.word(page_address, original_page + 0x800)
    n.m.word(n.s['h1_jit_contract_epoch'], 0x1234)
    n.m.word(stamp_address(n, left), 0x1234)
    cpu = native_state(n, left)
    result = n.call(fast, n.s['X'], n.s['RAM'])
    assert result == left, hex(result)
    assert n.m.read(n.s['X'], 28) == cpu, 'Private Page miss corrupted CPU A/P'
    assert entry(n, left)[2] == u32(n, 'h1_jit_epoch')
    n.m.word(page_address, original_page)
    n.m.word(n.s['h1_jit_contract_epoch'], 0)
    p.checks.append('Private entry Page miss leaves complete CPU state and reusable bank version intact')

    n.m.word(stamp_address(n, left), 0x1234)
    p.compile(left, code_left)
    assert stamp(n, left) == 0
    # Restore a previous physical bank from L2 without inheriting the L1 stamp.
    first_pointer = entry(n, left)[0]
    n.m.word(page_address, original_page + 0x800)
    alternate = bytes.fromhex('a9 81 aa 4c 00 92')
    n.prg(left, alternate)
    n.call(n.s['h1_nes_jit_compile'], left, alternate[0], original_page + 0x800, 0)
    n.m.word(stamp_address(n, left), 0x5678)
    n.m.word(page_address, original_page)
    n.call(n.s['h1_nes_jit_bind'], left, code_left[0], original_page)
    assert entry(n, left)[0] == first_pointer and stamp(n, left) == 0
    # The alternate physical page overlaps the right diagnostic block.
    p.compile(right, code_right)
    p.checks.append('Forced recompilation and L2 bank restoration clear the current-PC credential')

    # Generation one is deliberately present before UINT32 wrap. The strict
    # entry grants a new credential even with no CPU budget, then returns.
    strict, _ = p.compile(left, code_left)
    stamps = u32(n, 'h1_jit_contract_stamps')
    n.m.write(stamps, struct.pack('<I', 1) * 65536)
    n.m.word(n.s['h1_jit_contract_serial'], 0xffffffff)
    n.m.word(n.s['h1_jit_contract_epoch'], 0)
    native_state(n, left, 0)
    n.call(strict, n.s['X'], n.s['RAM'])
    assert u32(n, 'h1_jit_contract_serial') == 1
    assert u32(n, 'h1_jit_contract_epoch') == 1
    expected_stamps = bytearray(65536 * 4)
    struct.pack_into('<I', expected_stamps, left * 4, 1)
    assert n.m.read(stamps, 65536 * 4) == expected_stamps
    n.m.word(n.s['h1_jit_contract_epoch'], 19)
    old_epoch = u32(n, 'h1_jit_epoch')
    n.call(n.s['h1_nes_jit_invalidate'])
    assert u32(n, 'h1_jit_contract_epoch') == 0
    assert u32(n, 'h1_jit_epoch') != old_epoch
    p.checks.append('UINT32 generation wrap clears every stamp; cache invalidation revokes the active credential')

    # An interpreted PHP before a native target must retire the credential
    # before the target enters, rather than only at the outer slice exit.
    strict, _ = p.compile(left, code_left)
    p.compile(right, code_right)
    seen = []
    hook = n.m.uc.hook_add(UC_HOOK_CODE,
                          lambda *a: seen.append(u32(n, 'h1_jit_contract_epoch')),
                          begin=strict + shared, end=strict + shared)
    before_serial = u32(n, 'h1_jit_contract_serial')
    before_reuse = struct.unpack('<4I', n.m.read(n.s['h1_jit_schedule_stats'], 16))[3]
    p.run(0x9600, bytes.fromhex('08 4c 00 90'), 256, 'interpreter-to-native transition')
    n.m.uc.hook_del(hook)
    assert seen and seen[0] == 0, seen
    assert u32(n, 'h1_jit_contract_serial') > before_serial
    assert struct.unpack('<4I', n.m.read(n.s['h1_jit_schedule_stats'], 16))[3] > before_reuse
    p.checks.append('Interpreter fallback revokes, then strict validation grants a fresh reusable subinterval')

    # Observe original callbacks, not test substitutes, so their behavior and
    # side effects are included in the interpreter differential.
    status = struct.unpack('<I', n.m.read(n.s['ARead'] + 0x2002 * 4, 4))[0]
    callback_seen = []
    hook = n.m.uc.hook_add(UC_HOOK_CODE,
                          lambda *a: callback_seen.append(u32(n, 'h1_jit_contract_epoch')),
                          begin=status, end=status)
    status_code = bytes.fromhex('a9 11 ad 02 20 aa 4c 00 98')
    p.compile(0x9800, status_code)
    p.run(0x9800, status_code, 128, 'trusted status callback')
    n.m.uc.hook_del(hook)
    assert callback_seen and all(callback_seen), callback_seen
    sound_seen = []
    hook = n.m.uc.hook_add(UC_HOOK_CODE,
                          lambda *a: sound_seen.append(u32(n, 'h1_jit_contract_epoch')),
                          begin=n.s['FCEU_SoundCPUHook'], end=n.s['FCEU_SoundCPUHook'])
    p.compile(left, code_left)
    p.run(left, code_left, 128, 'trusted sound event callback',
          setup=lambda c: put(c, 'fhcnt', 'h1_apu_fhcnt', 1))
    n.m.uc.hook_del(hook)
    assert sound_seen and all(sound_seen), sound_seen
    p.checks.append('Trusted PPU-status and APU events preserve validated credentials and exact state')

    # Keep memory-write instrumentation independent of the preceding native
    # entry code hooks, and of the intentionally injected serial/Page state.
    previous_cases, previous_checks = p.cases, p.checks
    p.close()
    p = Pair(args, mapper)
    n = p.new
    p.cases, p.checks = previous_cases, previous_checks
    # An interrupt arrives only after both loop blocks have live credentials.
    # Trigger at an instruction boundary shared by the scalar interpreter and
    # the region scheduler; vector reads then mutate a baked operand/handler.
    irq_code = bytes.fromhex('ad 00 10 aa 4c 00 92')
    for irq, vector in ((0x80, 0xfffa), (0x800, 0xfffe)):
        p.compile(left, irq_code)
        p.compile(right, code_right)
        handlers = {}
        vector_seen = []
        trigger_seen = []
        irq_hooks = []
        for c in p.cores:
            handlers[c] = [struct.unpack('<I', c.m.read(c.s['ARead'] + address * 4, 4))[0]
                           for address in (vector, 0x1001)]
            c.prg(vector, struct.pack('<H', left))
            once = [False]

            def trigger(uc, access, address, size, value, unused, core=c, fired=once):
                if value >= 17 and not fired[0]:
                    fired[0] = True
                    core.m.word(core.s['X'] + 20, irq)
                    if core is n:
                        trigger_seen.append(u32(core, 'h1_jit_contract_epoch'))

            physical_timestamp = c.s['timestamp'] & 0x1fffffff
            irq_hooks.append((c, c.m.uc.hook_add(UC_HOOK_MEM_WRITE, trigger,
                                                begin=physical_timestamp, end=physical_timestamp + 3)))

            def read_vector(address, *unused, core=c):
                if core is n:
                    vector_seen.append(u32(core, 'h1_jit_contract_epoch'))
                core.prg(left + 1, b'\x01')
                core.m.word(core.s['ARead'] + 0x1001 * 4,
                            core.stub(0x80001bc0, lambda *a: 0x42))
                return left & 255

            c.m.word(c.s['ARead'] + vector * 4, c.stub(0x80001ba0, read_vector))
        p.run(left, irq_code, 128, 'IRQ/NMI vector mutation', regs=[35, 254, 128, 240, 0, 0])
        for c, hook in irq_hooks:
            c.m.uc.hook_del(hook)
        assert trigger_seen and all(trigger_seen), (irq, trigger_seen)
        assert vector_seen and not any(vector_seen), (irq, vector_seen)
        assert n.m.read(n.s['X'] + 6, 1) == b'\x42'
        for c in p.cores:
            for address, pointer in zip((vector, 0x1001), handlers[c]):
                c.m.word(c.s['ARead'] + address * 4, pointer)
    p.checks.append('IRQ/NMI vector side effects revoke live credentials before operand/handler mutations')

    # Public mapping and handler mutation paths revoke even for idempotent
    # writes. ResetCartMapping is last because it intentionally removes maps.
    read = struct.unpack('<I', n.m.read(n.s['ARead'] + 0x1000 * 4, 4))[0]
    write = struct.unpack('<I', n.m.read(n.s['BWrite'] + 0x1000 * 4, 4))[0]
    for name, arguments in (
            ('SetReadHandler', (0x1000, 0x1000, read)),
            ('SetWriteHandler', (0x1000, 0x1000, write)),
            ('FlushGenieRW', ()),
            ('SetupCartPRGMapping', (31, 0x83110000, 0x2000, 0)),
            ('setprg8', (0x8000, 0)),
            ('CartBW', (0x8000, 0x33)),
            ('ResetCartMapping', ())):
        n.m.word(n.s['h1_jit_contract_epoch'], 0x7711)
        n.call(n.s[name], *arguments)
        assert u32(n, 'h1_jit_contract_epoch') == 0, name
    p.checks.append('Handler, Genie, PRG setup/page mapping, writable cartridge and reset APIs revoke credentials')
    report = {'mapper': mapper, 'differential_cases': p.cases, 'checks': p.checks}
    p.close()
    return report


def frame_packet(c):
    """Run the real frontend frame with exact unmodified PCM/video capture."""
    audio, video = [], []

    def receive_audio(data, length, *unused):
        audio.append(c.m.read(data, length * 4))
        return length

    def receive_video(data, width, height, pitch, *unused):
        video.append((width, height, pitch,
                      b''.join(c.m.read(data + y * pitch, width * 2) for y in range(height))
                      if data else b''))
        return 0

    c.m.callbacks[0x80001b00] = receive_video
    c.m.callbacks[0x80001b20] = receive_audio
    c.call(c.core[10])
    return (c.m.read(c.s['X'], 28), c.m.read(c.s['RAM'], 2048), snapshot(c),
            tuple(audio), tuple(video))


def frame_boundaries(args, mapper):
    checks = []
    frame_results = []
    for dynamic, status_read in ((False, False), (True, False), (True, True)):
        p = Pair(args, mapper)
        n = p.new
        initial = bytes.fromhex('ad 00 50 4c 00 92')
        looping = bytes.fromhex('ad 02 20 a9 11 85 10 ea 4c 00 92' if status_read else
                                'a9 11 85 10 ea 4c 00 92')
        operand = 0x9204 if status_read else 0x9201
        p.compile(0x9200, looping)
        p.code(0x9000, initial)
        if status_read:
            # The block was previously admitted with no input hook. Installing
            # one must reject its strict entry even though byte/PRG guards pass.
            p.run(0x9200, looping, 7, 'warm status block before dynamic input hook')
            assert stamp(n, 0x9200)
        callbacks = []
        status_callbacks = []
        slices = []
        hooks = []
        for c in p.cores:
            native_state(c, 0x9000 if dynamic else 0x9200, 0)
            c.m.write(c.s['PPU'], b'\0')
            c.m.write(c.s['ppudead'], b'\0')
            if dynamic:
                installed = [False]

                def scanline(*unused, core=c):
                    if core is n:
                        callbacks.append(('scanline', u32(core, 'h1_jit_contract_epoch'),
                                          stamp(core, 0x9200), u32(core, 'h1_jit_contract_frame'),
                                          u32(core, 'h1_jit_contract_dirty')))
                    # This raw mutation deliberately bypasses cart APIs. The
                    # unknown installer must already have disabled frame scope.
                    core.prg(operand, b'\x71')
                    core.m.word(core.s['InputScanlineHook'], 0)
                    return 0

                scanline_pointer = c.stub(0x80001bc0, scanline)

                def install_hook(address, *unused, core=c, once=installed, pointer=scanline_pointer):
                    if not once[0]:
                        once[0] = True
                        if core is n:
                            callbacks.append(('install', u32(core, 'h1_jit_contract_epoch'),
                                              stamp(core, 0x9200), u32(core, 'h1_jit_contract_frame'),
                                              u32(core, 'h1_jit_contract_dirty')))
                        core.m.word(core.s['InputScanlineHook'], pointer)
                    return 0

                c.m.word(c.s['ARead'] + 0x5000 * 4, c.stub(0x80001ba0, install_hook))
                if status_read and c is n:
                    status = struct.unpack('<I', n.m.read(n.s['ARead'] + 0x2002 * 4, 4))[0]

                    def observe_status(*unused):
                        if u32(n, 'InputScanlineHook'):
                            status_callbacks.append((u32(n, 'h1_jit_contract_epoch'),
                                                     u32(n, 'h1_jit_contract_dirty')))

                    hooks.append(n.m.uc.hook_add(UC_HOOK_CODE, observe_status, begin=status, end=status))
            else:
                # The ordinary callback-free frame should carry a validated
                # block credential over multiple CPU scheduling calls.
                if c is n:
                    def observe_slice(*unused):
                        slices.append((u32(n, 'h1_jit_contract_epoch'), stamp(n, 0x9200),
                                       u32(n, 'h1_jit_contract_frame')))
                    hooks.append(n.m.uc.hook_add(UC_HOOK_CODE, observe_slice,
                                                begin=n.s['X6502_Run'], end=n.s['X6502_Run']))
        before_deopts = struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4]
        packets = [frame_packet(c) for c in p.cores]
        assert packets[0] == packets[1], (mapper, dynamic, status_read, 'complete frontend frame',
                [i for i, values in enumerate(zip(*packets)) if values[0] != values[1]])
        assert packets[1][3] and packets[1][4], 'Frame did not deliver PCM and video'
        assert u32(n, 'h1_jit_contract_epoch') == 0
        assert u32(n, 'h1_jit_contract_frame') == 0
        if dynamic:
            installs = [item for item in callbacks if item[0] == 'install']
            scanlines = [item for item in callbacks if item[0] == 'scanline']
            assert len(installs) == len(scanlines) == 1, callbacks
            assert installs[0][1] == 0 and installs[0][3:] == (1, 1), callbacks
            assert scanlines[0][1] == 0 and scanlines[0][3] == 0, callbacks
            assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
            assert struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4] > before_deopts
            if status_read:
                assert installs[0][2] and scanlines[0][2] == 0, callbacks
                assert status_callbacks and all(active == 0 and dirty == 1
                                               for active, dirty in status_callbacks), status_callbacks[:8]
                checks.append('A2002 block admitted before dynamic input-hook installation strictly falls back before callback mutation')
            else:
                assert scanlines[0][2], callbacks
                checks.append('Unknown reader installs an input hook: scope retires before pre-render raw ROM mutation')
        else:
            assert sum(bool(active and active == saved and frame) for active, saved, frame in slices) > 2, slices[:8]
            assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x11'
            checks.append('Eligible frame preserves block credentials across CPU calls and clears them at frame exit')
        for hook in hooks:
            n.m.uc.hook_del(hook)
        frame_results.append({'dynamic_hook': dynamic, 'status_read': status_read,
                              'pcm_frames': sum(len(x) // 4 for x in packets[1][3]),
                              'video_calls': len(packets[1][4]), 'callbacks': callbacks,
                              'status_callbacks_with_hook': len(status_callbacks)})
        p.close()
    return {'mapper': mapper, 'checks': checks, 'frames': frame_results}


def dmc_boundary(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    left, right = 0x9000, 0x9200
    first = bytes.fromhex('a9 23 aa 4c 00 92')
    second = bytes.fromhex('a9 11 85 10 ea 4c 00 90')
    p.compile(left, first)
    p.compile(right, second)
    observed, triggered, hooks, event_observations = [], [], [], []
    for c in p.cores:
        once = [False]
        event_observations.append([])
        event_log = event_observations[-1]

        def trigger(uc, address, size, unused, core=c, fired=once, log=event_log):
            value = u32(core, 'timestamp')
            if value >= 17 and not fired[0]:
                assert value == 17, ('DMC injection missed the shared event boundary', value)
                cycles = uc.reg_read(UC_MIPS_REG_A0)
                frame = struct.unpack('<i',core.m.read(core.s['h1_apu_clock'],4))[0]
                assert frame <= cycles * 48, ('Not a frame deadline', frame, cycles)
                log.append((value, frame, cycles))
                fired[0] = True
                if core is n:
                    triggered.append((u32(core, 'h1_jit_contract_epoch'), stamp(core, right)))
                core.m.word(core.s['DMCAddress'], 0x4000)
                put(core, 'DMCSize', 'h1_apu_dmcsize', 1)
                put(core, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
                put(core, 'DMCacc', 'h1_apu_dmcacc', 1)

        def dma_read(address, *unused, core=c):
            if core is n:
                observed.append((u32(core, 'h1_jit_contract_epoch'),
                                 u32(core, 'h1_jit_contract_dirty'), stamp(core, right)))
            core.prg(right + 1, b'\x71')
            return 0x80

        c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001ba0, dma_read))
        # A timestamp store inside an event-free region is an implementation
        # detail: injecting asynchronous DMA between that store and its APU
        # commit would split an atomic emulated interval. Force a real frame
        # deadline and inject at the original event handler instead, after
        # both backends reach the same observable instruction boundary.
        sound_hook = c.s['FCEU_SoundCPUHook']
        hooks.append((c, c.m.uc.hook_add(UC_HOOK_CODE, trigger,
                                       begin=sound_hook, end=sound_hook)))
    def event_at_seventeen(c):
        put(c, 'fhcnt', 'h1_apu_fhcnt', 17 * 48)
    p.run(left, first, 256, 'unknown DMC read mutates validated future block',
          setup=event_at_seventeen)
    assert all(len(log) == 1 for log in event_observations), event_observations
    assert event_observations[0] == event_observations[1], event_observations
    assert triggered and all(active and active == saved for active, saved in triggered), triggered
    assert len(observed) == 4 and all(active == 0 and dirty == 1 and saved
                                    for active, dirty, saved in observed), observed
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    for c, hook in hooks:
        c.m.uc.hook_del(hook)
    p.close()
    return {'mapper': mapper, 'differential_cases': 1,
            'checks': ['Unknown DMC fetch revokes/marks dirty before mutating a previously validated operand'],
            'callback_observations': observed, 'event_observations': event_observations}


def dmc_instruction_boundaries(args, mapper):
    results = []
    # Keep the one-instruction C boundary and independently prove the native
    # entry guard above its 96-unit admission budget. Pending DMA plus LDA
    # consumes six CPU clocks, so both budgets still stop after that one LDA.
    for inline_backedge, budget in ((False, 1), (False, 7), (True, 256)):
        p = Pair(args, mapper)
        n = p.new
        pc = 0x9000
        code = bytes.fromhex('a9 11 aa e8 86 10 4c 00 90')
        p.compile(pc, code)
        observed = []
        for c in p.cores:
            def dma_read(address, *unused, core=c):
                if core is n:
                    observed.append((u32(core, 'h1_jit_contract_epoch'),
                                     u32(core, 'h1_jit_contract_dirty'), stamp(core, pc)))
                core.prg(pc + 1, b'\x71')
                return 0x80

            c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001ba0, dma_read))

        def pending_dma(c):
            c.m.word(c.s['DMCAddress'], 0x4000)
            put(c, 'DMCSize', 'h1_apu_dmcsize', 1)
            put(c, 'DMCHaveDMA', 'h1_apu_have_dma', int(inline_backedge), 1)
            put(c, 'DMCacc', 'h1_apu_dmcacc', 1)
            c.m.write(c.s['DMCBitCount'], bytes([7 if inline_backedge else 0]))
            c.m.write(c.s['DMCHaveSample'], b'\0')

        native_before = n.native
        deopts_before = struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4]
        p.run(pc, code, budget,
              'DMC mutates inline-backedge operand' if inline_backedge else 'DMC mutates current LDA operand',
              setup=pending_dma)
        if budget > 6:
            assert n.native > native_before, 'Precompiled native entry was not attempted'
        else:
            assert n.native == native_before, 'Short budget unexpectedly entered native'
        assert len(observed) == 4 and all(active == 0 and dirty == 1 for active, dirty, saved in observed)
        assert n.m.read(n.s['X'] + 6, 1) == b'\x71'
        if inline_backedge:
            assert all(saved for active, dirty, saved in observed), observed
            assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x72'
            assert struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4] > deopts_before
        else:
            # One LDA (2 CPU clocks) and four original DMA reads. A native
            # timing commit before fallback would incorrectly count twice.
            assert u32(n, 'timestamp') == 6
        results.append({'inline_backedge': inline_backedge, 'ppu_budget': budget,
                        'callback_observations': observed})
        p.close()
    return {'mapper': mapper, 'differential_cases': len(results),
            'checks': ['Pending DMA falls back before timing and reads the current modified immediate operand',
                       'DMC byte-boundary DMA mutates an earlier operand revisited by an inline back edge'],
            'cases': results}


def fetched_opcode_budget(args, mapper, budget):
    p = Pair(args, mapper)
    n = p.new
    pc = 0x9000
    code = bytes.fromhex('a9 11 aa e8 86 10 4c 00 90')
    changed = b'\xa2\x71' + code[2:]
    p.compile(pc, code)
    observations = []
    for c in p.cores:
        def dma_read(address, *unused, core=c):
            if core is n:
                observations.append((u32(core, 'h1_jit_contract_epoch'),
                                     u32(core, 'h1_jit_contract_dirty')))
            core.prg(pc, changed)
            return 0x80

        c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001ba0, dma_read))

    def pending(c):
        c.m.word(c.s['DMCAddress'], 0x4000)
        put(c, 'DMCSize', 'h1_apu_dmcsize', 1)
        put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
        put(c, 'DMCacc', 'h1_apu_dmcacc', 1)

    native_before = n.native
    p.run(pc, code, budget, 'DMA changes fetched A9 to A2 and changes its operand', setup=pending)
    if budget > 6:
        assert n.native > native_before, 'Fetched-opcode native entry was not attempted'
    else:
        assert n.native == native_before, 'Short budget unexpectedly entered native'
    # Opcode A9 was fetched before DMA. Its operand is read after DMA, so only
    # A changes now. A fresh instruction boundary sees the new opcode A2.
    assert n.m.read(n.s['X'] + 6, 2) == b'\x71\xfe'
    assert u32(n, 'timestamp') == 6
    assert len(observations) == 4 and all(item == (0, 1) for item in observations)
    p.run(pc, changed, 1, 'new instruction fetch observes DMA-modified A2')
    assert n.m.read(n.s['X'] + 6, 2) == b'\x23\x71'
    assert u32(n, 'timestamp') == 2
    p.close()
    return {'ppu_budget': budget, 'differential_cases': 2}


def fetched_opcode_boundary(args, mapper):
    # DMA steals enough clocks that admission at seven PPU cycles still stops
    # after A9. Retain the original one-cycle case for the short C path too.
    cases = [fetched_opcode_budget(args, mapper, budget) for budget in (1, 7)]
    return {'mapper': mapper,
            'differential_cases': sum(case['differential_cases'] for case in cases),
            'checks': ['Already fetched A9 executes with the new operand; the following fetch observes A2',
                       'Short C slice and admitted native guard preserve the same DMA fetch ordering'],
            'cases': cases}


def event_branch_boundaries(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    pc = 0x80fa
    cases = []
    for taken, displacement in ((False, 5), (True, 1), (True, 5)):
        code = bytes((0xea, 0xd0, displacement))
        p.compile(pc, code)
        for carry in (0, 2):
            completions = []
            dma_reads = []

            def complete(uc, address, size, unused):
                completions.append((uc.reg_read(UC_MIPS_REG_A0), uc.reg_read(UC_MIPS_REG_A1)))

            hook = n.m.uc.hook_add(UC_HOOK_CODE, complete,
                                  begin=n.s['h1_cpu_jit_complete'], end=n.s['h1_cpu_jit_complete'])
            dma_hook = n.m.uc.hook_add(UC_HOOK_CODE, lambda *unused: dma_reads.append(1),
                                      begin=n.s['X6502_DMR'], end=n.s['X6502_DMR'])

            def event_at_branch(c):
                # NOP remains on the fast path. At BNE's exact base-cycle
                # boundary, tester() consumes the buffered sample and makes
                # DMA pending. This is the case requiring semantic completion.
                put(c, 'DMCSize', 'h1_apu_dmcsize', 1)
                put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 1, 1)
                put(c, 'DMCacc', 'h1_apu_dmcacc', carry + 4)
                c.m.write(c.s['DMCBitCount'], b'\x07')
                c.m.write(c.s['DMCHaveSample'], b'\0')

            p.run(pc, code, 9, 'DMC byte boundary at BNE with branch cycle carry', setup=event_at_branch,
                  regs=[35, 254, 128, 240, 4 if taken else 6, 4 if taken else 6], tcount=carry)
            n.m.uc.hook_del(hook)
            n.m.uc.hook_del(dma_hook)
            extra = (1 + int(displacement == 5)) if taken else 0
            expected_pc = pc + 3 + (displacement if taken else 0)
            assert completions == [(pc + 1, 0xd0)], completions
            assert not dma_reads, 'Budget must end at BNE before the next instruction performs DMA'
            assert n.m.read(n.s['h1_apu_clock'] + 8, 5) == b'\x01\0\0\0\0'
            assert u32(n, 'timestamp') == 4 + extra
            assert struct.unpack('<I', n.m.read(n.s['X'], 4))[0] == extra
            assert struct.unpack('<H', n.m.read(n.s['X'] + 4, 2))[0] == expected_pc
            cases.append({'taken': taken, 'page_cross': bool(taken and displacement == 5),
                          'incoming_carry': carry, 'remaining_tcount': extra,
                          'timestamp': 4 + extra})
    p.close()
    return {'mapper': mapper, 'differential_cases': len(cases),
            'checks': ['Newly pending DMC DMA completes BNE once, preserves branch tcount, and waits until the next instruction'],
            'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-contract.json')
    args = parser.parse_args()
    reports = [suite(args, mapper) for mapper in (0, 19)]
    report = {'ok': True, 'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'mappers': reports,
              'frame_boundaries': [frame_boundaries(args, mapper) for mapper in (0, 19)],
              'dmc_boundaries': [dmc_boundary(args, mapper) for mapper in (0, 19)],
              'dmc_instruction_boundaries': [dmc_instruction_boundaries(args, mapper) for mapper in (0, 19)],
              'fetched_opcode_boundaries': [fetched_opcode_boundary(args, mapper) for mapper in (0, 19)],
              'event_branch_boundaries': [event_branch_boundaries(args, mapper) for mapper in (0, 19)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
