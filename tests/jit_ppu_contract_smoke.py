"""Real-MIPS regression for exact-handler PPU-write credentials.

The reference is the same artifact with JIT closed. Original PPU handlers are
executed; test callbacks deliberately alter diagnostic PRG data and hook state.
No executable host instruction is patched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import UC_HOOK_CODE

from jit_contract_smoke import Pair, ROOT, entry, stamp, u32
from jit_event_smoke import snapshot


def pointer(c, table, index):
    return struct.unpack('<I', c.m.read(c.s[table] + index * 4, 4))[0]


def schedule_reuse(c):
    return struct.unpack('<4I', c.m.read(c.s['h1_jit_schedule_stats'], 16))[3]


def clear_ppu(c):
    c.m.write(c.s['PPU'], bytes(4))
    c.m.write(c.s['vtoggle'], b'\0')
    c.m.word(c.s['Pline'], 0)
    c.m.word(c.s['Plinef'], 0)
    c.m.word(c.s['PPU_hook'], 0)
    c.m.word(c.s['InputScanlineHook'], 0)


def trusted_writes(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    cases = []
    table = struct.unpack('<7I', n.m.read(n.s['h1_ppu_code_safe_writes'], 28))
    for address in (*range(0x2000, 0x2007), 0x2008, 0x3ffd):
        pc = 0x9000
        code = bytes((0xa9, 0x23, 0x8d, address & 255, address >> 8, 0x4c, 0x00, 0x90))
        for c in p.cores:
            clear_ppu(c)
        strict, gate = p.compile(pc, code)
        handler = pointer(n, 'BWrite', address)
        assert handler == table[address & 7]
        callbacks, gates = [], []

        def observe_handler(*unused):
            callbacks.append((u32(n, 'h1_jit_contract_epoch'), stamp(n, pc)))

        def observe_gate(*unused):
            gates.append((u32(n, 'h1_jit_contract_epoch'), stamp(n, pc)))

        hooks = [n.m.uc.hook_add(UC_HOOK_CODE, observe_handler, begin=handler, end=handler),
                 n.m.uc.hook_add(UC_HOOK_CODE, observe_gate, begin=gate, end=gate)]
        before = schedule_reuse(n)
        p.run(pc, code, 256, 'trusted exact PPU write and dispatcher reentry', setup=clear_ppu)
        for hook in hooks:
            n.m.uc.hook_del(hook)
        assert callbacks and all(active and active == saved for active, saved in callbacks), (hex(address), callbacks)
        assert any(active and active == saved for active, saved in gates), (hex(address), gates)
        assert schedule_reuse(n) > before, hex(address)
        assert entry(n, pc)[0] == strict
        cases.append({'address': hex(address), 'original_handler': hex(handler),
                      'calls': len(callbacks), 'native_gate_reuses': schedule_reuse(n) - before})
    count = p.cases
    p.close()
    return {'mapper': mapper, 'differential_cases': count,
            'checks': ['Exact B2000-B2006 and low/high mirrors preserve credentials through terminal-store C reentry'],
            'cases': cases}


def custom_write(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    writer, future = 0x9000, 0x9200
    writer_code = bytes.fromhex('a9 23 8d 05 20 4c 00 92')
    future_code = bytes.fromhex('a9 11 85 10 4c 00 90')
    observed = []
    for c in p.cores:
        def custom(address, value, *unused, core=c):
            if core is n:
                observed.append((u32(core, 'h1_jit_contract_epoch'),
                                 u32(core, 'h1_jit_contract_dirty'), stamp(core, future)))
            core.prg(future + 1, b'\x71')
            return 0

        c.m.word(c.s['BWrite'] + 0x2005 * 4, c.stub(0x80001ba0, custom))
    p.compile(writer, writer_code)
    p.compile(future, future_code)
    p.run(future, future_code, 256, 'custom BWrite in a PPU register slot')
    assert observed and observed[0][2] and all(active == 0 and dirty == 1
                                             for active, dirty, saved in observed), observed
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    p.close()
    return {'mapper': mapper, 'differential_cases': 1,
            'checks': ['Custom BWrite at a nominally safe PPU address revokes before changing validated future code']}


def input_hook_write(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    writer, future = 0x9000, 0x9200
    writer_code = bytes.fromhex('a9 23 8d 05 20 4c 00 92')
    future_code = bytes.fromhex('a9 11 85 10 4c 00 90')
    p.compile(writer, writer_code)
    p.compile(future, future_code)
    observed = []
    hook_pointers = {}
    for c in p.cores:
        def scanline(*unused, core=c):
            if core is n:
                observed.append((u32(core, 'h1_jit_contract_epoch'),
                                 u32(core, 'h1_jit_contract_dirty'), stamp(core, future)))
            core.prg(future + 1, b'\x71')
            core.m.word(core.s['InputScanlineHook'], 0)
            return 0

        hook_pointers[c] = c.stub(0x80001ba0, scanline)

    def active_line(c):
        clear_ppu(c)
        c.m.word(c.s['Pline'], 0x83110000)
        c.m.word(c.s['Plinef'], 0x83110000)
        c.m.word(c.s['firsttile'], 0)
        c.m.word(c.s['linestartts'], 0)
        c.m.word(c.s['sphitx'], 0x100)
        c.m.word(c.s['InputScanlineHook'], hook_pointers[c])

    before = struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4]
    p.run(future, future_code, 256, 'dynamic input hook during original B2005', setup=active_line)
    assert len(observed) == 1 and observed[0][0] == 0 and observed[0][1] == 1 and observed[0][2], observed
    assert struct.unpack('<8I', n.m.read(n.s['h1_jit_stats'], 32))[4] > before
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    for c in p.cores:
        clear_ppu(c)
    p.close()
    return {'mapper': mapper, 'differential_cases': 1,
            'checks': ['Input hook installed after compilation deopts a trusted store and runs callback after revocation']}


def dynamic_ppu_hook(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    writer, future = 0x9000, 0x9200
    writer_code = bytes.fromhex('ad 00 50 a9 23 8d 06 20 4c 00 92')
    future_code = bytes.fromhex('a9 11 85 10 4c 00 90')
    p.code(writer, writer_code)
    p.compile(future, future_code)
    observed = []
    for c in p.cores:
        installed = [False]

        def ppu(address, *unused, core=c):
            if core is n:
                observed.append(('ppu', u32(core, 'h1_jit_contract_epoch'),
                                 u32(core, 'h1_jit_contract_dirty'), stamp(core, future)))
            core.prg(future + 1, b'\x71')
            core.m.word(core.s['PPU_hook'], 0)
            return 0

        callback = c.stub(0x80001bc0, ppu)

        def install(address, *unused, core=c, once=installed, callback=callback):
            if not once[0]:
                once[0] = True
                if core is n:
                    observed.append(('install', u32(core, 'h1_jit_contract_epoch'),
                                     u32(core, 'h1_jit_contract_dirty'), stamp(core, future)))
                core.m.word(core.s['PPU_hook'], callback)
                core.m.write(core.s['vtoggle'], b'\x01')
            return 0

        c.m.word(c.s['ARead'] + 0x5000 * 4, c.stub(0x80001ba0, install))
    p.run(future, future_code, 256, 'dynamic PPU hook installed before original B2006', setup=clear_ppu)
    assert [item[0] for item in observed] == ['install', 'ppu'], observed
    assert all(active == 0 and dirty == 1 and saved for _, active, dirty, saved in observed), observed
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    p.close()
    return {'mapper': mapper, 'differential_cases': 1,
            'checks': ['Runtime PPU-hook installation and B2006 callback cannot retain a future-code credential']}


def nmi_boundary(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    code = bytes.fromhex('a9 80 8d 00 20 a9 55 85 10 4c 09 90')
    nmi_code = bytes.fromhex('a9 77 85 11 4c 00 96')
    p.compile(0x9000, code)
    p.code(0x9600, nmi_code)
    p.code(0xfffa, struct.pack('<H', 0x9600))
    transitions = []

    def observe_dispatch(*unused):
        value = struct.unpack('<I', n.m.read(n.s['X'] + 20, 4))[0]
        if not transitions or transitions[-1][0] != value:
            transitions.append((value, u32(n, 'timestamp')))

    # A write hook on IRQlow in TriggerNMI2's MIPS return delay slot causes
    # Unicorn to jump to PC=0. Observe the unchanged real dispatcher instead:
    # it sees NMI2 on native return, its promotion, and the vector completion.
    begin = n.s.get('h1_cpu_run_jit', n.s['X6502_Run'])
    end = min(address for address in n.s.values() if address > begin) - 1
    hook = n.m.uc.hook_add(UC_HOOK_CODE, observe_dispatch, begin=begin, end=end)

    def vblank(c):
        clear_ppu(c)
        c.m.write(c.s['PPU'] + 2, b'\x80')

    p.run(0x9000, code, 64, 'B2000 rising NMI enable during vblank', setup=vblank)
    n.m.uc.hook_del(hook)
    assert n.m.read(n.s['RAM'] + 0x10, 2) == b'\x5b\x77'
    assert (0x40, 6) in transitions and (0x80, 6) in transitions and (0, 15) in transitions, transitions
    p.close()
    return {'mapper': mapper, 'differential_cases': 1,
            'checks': ['B2000 NMI2 serves exactly one intervening opcode before the NMI vector'],
            'irq_transitions': transitions}


def excluded_write(args, mapper, address):
    p = Pair(args, mapper)
    n = p.new
    code = bytes((0xa9, 0, 0x8d, address & 255, address >> 8, 0x4c, 0, 0x90))
    p.compile(0x9000, code)
    observed = []
    handler = pointer(n, 'BWrite', address)

    def observe(*unused):
        observed.append((u32(n, 'h1_jit_contract_epoch'), u32(n, 'h1_jit_contract_dirty')))

    hook = n.m.uc.hook_add(UC_HOOK_CODE, observe, begin=handler, end=handler)
    p.run(0x9000, code, 9, 'excluded OAM DMA/APU write remains opaque', setup=clear_ppu)
    n.m.uc.hook_del(hook)
    assert observed and all(item == (0, 1) for item in observed), (hex(address), observed)
    p.close()
    return {'mapper': mapper, 'address': hex(address), 'differential_cases': 1,
            'checks': ['Original OAM DMA/APU callback executes after credential revocation']}


def alias_write(args, mapper, nametable):
    p = Pair(args, mapper)
    n = p.new
    writer, future = 0x9000, 0x9200
    writer_code = bytes.fromhex('a9 71 8d 07 20 4c 00 92')
    future_code = bytes.fromhex('a9 11 85 10 4c 00 90')
    p.compile(writer, writer_code)
    p.compile(future, future_code)
    originals = {}
    observed = []
    handler = pointer(n, 'BWrite', 0x2007)
    table = 'vnapage' if nametable else 'VPage'

    def alias(c):
        clear_ppu(c)
        originals[c] = pointer(c, table, 0)
        prg = pointer(c, 'Page', future >> 11) + future
        c.m.word(c.s[table], prg)
        c.m.word(c.s['RefreshAddr'], 0x2001 if nametable else 1)
        c.m.write(c.s['PPUNTARAM' if nametable else 'PPUCHRRAM'], b'\x01')

    def observe(*unused):
        observed.append((u32(n, 'h1_jit_contract_epoch'),
                         u32(n, 'h1_jit_contract_dirty'), stamp(n, future)))

    hook = n.m.uc.hook_add(UC_HOOK_CODE, observe, begin=handler, end=handler)
    # Stop after the first rewritten operand is executed, before a later
    # B2007 access advances through unrelated diagnostic code bytes.
    p.run(future, future_code, 69, 'B2007 VRAM alias writes immutable PRG', setup=alias)
    n.m.uc.hook_del(hook)
    assert observed and all(active == 0 and dirty == 1 and saved for active, dirty, saved in observed), observed
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    for c in p.cores:
        prg = pointer(c, 'Page', future >> 11) + future
        assert c.m.read(prg + 1, 1) == b'\x71'
        c.m.word(c.s[table], originals[c])
    p.close()
    return {'mapper': mapper, 'alias': table, 'differential_cases': 1,
            'checks': ['B2007 remains opaque even when VRAM/nametable backing aliases executable PRG']}


def oam_destination(args, mapper):
    """DMW keeps the exact ADDCYC ordering and the proved B2004 destination."""
    p = Pair(args, mapper)
    n = p.new
    code = bytes.fromhex('a9 11 ea ea 4c 00 90')
    p.compile(0x9000, code)
    has_deadline = 'h1_cpu_apu_deadline' in n.s
    native_before = n.native
    for c in p.cores:
        clear_ppu(c)
        c.call(c.s['h1_nes_jit_frame_begin'])
        c.run(0x9000, code, [35, 254, 128, 240, 4, 4], bytes(2048), 6)
    assert n.native == native_before, 'Six PPU cycles must stay below native admission'
    assert p.old.m.read(p.old.s['X'], 28) == n.m.read(n.s['X'], 28)
    assert snapshot(p.old) == snapshot(n)
    for c in p.cores:
        # Seven PPU cycles exceed the existing 96-unit admission gate.
        # The DMW assertion below needs a genuinely live native credential.
        c.run(0x9000, code, [35, 254, 128, 240, 4, 4], bytes(2048), 7)
        if has_deadline:
            c.m.word(c.s['h1_cpu_apu_deadline'], u32(c, 'timestamp') + 1000)
    assert n.native > native_before, 'OAM destination fixture never entered native code'
    active = u32(n, 'h1_jit_contract_epoch')
    deadline = u32(n, 'h1_cpu_apu_deadline') if has_deadline else None
    assert active and stamp(n, 0x9000) == active
    before = u32(n, 'timestamp')
    for c in p.cores:
        c.call(c.s['X6502_DMW'], 0x2004, 0x71)
    assert u32(n, 'timestamp') == before + 1
    assert u32(n, 'h1_jit_contract_epoch') == active
    if has_deadline:
        assert u32(n, 'h1_cpu_apu_deadline') == deadline
    assert n.m.read(n.s['SPRAM'], 1) == b'\x71'
    assert p.old.m.read(p.old.s['X'], 28) == n.m.read(n.s['X'], 28)
    assert snapshot(p.old) == snapshot(n)
    for c in p.cores:
        c.call(c.s['h1_nes_jit_frame_end'])
    p.close()
    return {'mapper': mapper, 'differential_cases': 1, 'apu_deadline_cache_present': has_deadline,
            'checks': ['Original OAM destination B2004 preserves code/APU credentials after the original one-cycle DMA increment']}


def oam_unknown_source(args, mapper):
    p = Pair(args, mapper)
    n = p.new
    writer, future = 0x9000, 0x9200
    writer_code = bytes.fromhex('a9 50 8d 14 40 4c 00 92')
    future_code = bytes.fromhex('a9 11 85 10 4c 00 90')
    p.compile(writer, writer_code)
    p.compile(future, future_code)
    observed = []
    for c in p.cores:
        def source(address, *unused, core=c):
            if core is n:
                observed.append((address, u32(core, 'h1_jit_contract_epoch'),
                                 u32(core, 'h1_jit_contract_dirty')))
            core.prg(future + 1, b'\x71')
            return address & 255

        callback = c.stub(0x80001ba0, source)
        c.call(c.s['SetReadHandler'], 0x5000, 0x50ff, callback)
    p.run(future, future_code, 1600, 'unknown OAM source mutates validated code', setup=clear_ppu)
    assert len(observed) == 256, len(observed)
    assert [address for address, _, _ in observed] == list(range(0x5000, 0x5100))
    assert all(active == 0 and dirty == 1 for _, active, dirty in observed)
    assert n.m.read(n.s['RAM'] + 0x10, 1) == b'\x71'
    assert p.old.m.read(p.old.s['SPRAM'], 256) == n.m.read(n.s['SPRAM'], 256)
    p.close()
    return {'mapper': mapper, 'differential_cases': 1, 'unknown_source_reads': len(observed),
            'checks': ['All 256 OAM source reads retain opaque callback revocation; destination optimization cannot conceal PRG mutation']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-ppu-contract.json')
    args = parser.parse_args()
    reports = []
    for mapper in (0, 19):
        reports.extend((trusted_writes(args, mapper), custom_write(args, mapper),
                        input_hook_write(args, mapper), dynamic_ppu_hook(args, mapper),
                        nmi_boundary(args, mapper)))
        reports.extend(excluded_write(args, mapper, address) for address in (0x4014, 0x4000, 0x4015))
        reports.extend(alias_write(args, mapper, nametable) for nametable in (False, True))
        reports.extend((oam_destination(args, mapper), oam_unknown_source(args, mapper)))
    report = {'ok': True, 'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'differential_cases': sum(item['differential_cases'] for item in reports), 'results': reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
