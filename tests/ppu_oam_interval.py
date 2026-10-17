"""Actual MIPS OAM interval invalidation, including failed state reads/hooks.

Direct test-owned OAM changes use the explicit invalidation API, except the
frame-boundary case, which deliberately relies on the public frame entry.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from jit_smoke import Core
from make_test_rom import make_rom
from h1_mock import ROOT


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for option in ('baseline-bda', 'baseline-elf', 'bda', 'elf', 'output'):
        p.add_argument('--' + option, type=Path, required=True)
    p.add_argument('--nm', type=Path, default=ROOT.parent/'.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    a = p.parse_args()
    old = Core(a.baseline_bda, a.baseline_elf, a.nm, make_rom(sprites=True))
    new = Core(a.bda, a.elf, a.nm, make_rom(sprites=True))
    cores = (old, new)
    audio = {id(c):bytearray() for c in cores}
    for c in cores:
        def capture(ptr, count, *unused, c=c):
            audio[id(c)].extend(c.m.read(ptr, count*4))
            return count
        c.stub(0x80001b20, capture)
    records = []
    def word(c, name): return struct.unpack('<I', c.m.read(c.s[name], 4))[0]
    def fetch(label, line=30):
        values = []
        for c in cores:
            audio[id(c)].clear()
            c.call(c.core[10])
            size = c.call(c.core[16])
            assert c.call(c.core[17], 0x83200000, size) == 1
            pixels = b''.join(c.m.read(word(c,n), 256*240) for n in ('XBuf','XDBuf'))
            values.append(c.m.read(0x83200000, size) + pixels +
                          c.m.read(c.s['SPRBUF'],256) + bytes(audio[id(c)]))
        assert values[0] == values[1], label
        records.append(label)
    initial = bytes(v for i in range(64) for v in (i*3, i, i*3 & 255, i*4))
    for index in range(5): fetch(f'warmup-{index}')
    for c in cores:
        c.m.write(c.s['SPRAM'], initial)
        c.m.write(c.s['PPU'], b'\0\x18\0\0')
    new.call(new.s['H1_PPU_InvalidateOAM'])
    fetch('initial-dirty'); fetch('initial-index-build'); fetch('clean-reuse')
    # Emulate a frontend/debugger changing exposed memory between frame calls,
    # without a register writer. The public frame entry must revoke its cache.
    for c in cores: c.m.write(c.s['SPRAM'], bytes([60,3,0,70])+bytes([240,0,0,0])*63)
    new.m.word(new.s['h1_sprite_oam_dirty'], 0)
    fetch('raw-between-frame-write'); fetch('raw-write-stable')
    for index in range(256):
        new.m.word(new.s['h1_sprite_oam_dirty'], 0)
        for c in cores:
            c.call(c.s['B2003'], 0x2003, index)
            c.call(c.s['B2004'], 0x2004, (index*17+5)&255)
        assert word(new, 'h1_sprite_oam_dirty') == 1
        if index % 64 == 63: fetch(f'registers-through-{index}')
    # The original DMA writer must reach the same invalidation through B2004.
    new.m.word(new.s['h1_sprite_oam_dirty'], 0)
    for c in cores:
        c.m.write(c.s['RAM']+512, initial)
        c.call(c.s['B2003'], 0x2003, 0)
        c.call(c.s['B4014'], 0x4014, 2)
    assert word(new, 'h1_sprite_oam_dirty') == 1
    fetch('dma'); fetch('dma-stable')
    # An observer can mutate OAM and remove itself after a fetch. Its prior
    # presence must keep the cache untrusted even when its pointer becomes null.
    for hook in ('InputScanlineHook', 'GameHBIRQHook', 'GameHBIRQHook2'):
        for c in cores:
            c.m.write(c.s['SPRAM'], bytes([40,3,0,40])+bytes([240,0,0,0])*63)
        new.call(new.s['H1_PPU_InvalidateOAM'])
        fetch(hook+'-prime-index')
        calls = {id(c):0 for c in cores}
        for c in cores:
            def callback(*unused, c=c, hook=hook):
                calls[id(c)] += 1
                # The first scanline callback follows the next-frame dirty
                # refresh. Defer mutation to a later line to require the
                # callback interval's own invalidation, not the frame guard.
                if calls[id(c)] == 8:
                    c.m.write(c.s['SPRAM'], bytes([30,3,0,20])+bytes([240,0,0,0])*63)
                    c.m.word(c.s[hook], 0)
                return 0
            c.m.word(c.s[hook], c.stub(0x80001b80, callback))
        fetch(hook+'-mutate-and-remove')
        assert all(n == 8 for n in calls.values()), (hook, calls)
        fetch(hook+'-stable')
    # Malformed bytes after the complete SPRA field leave partially loaded
    # OAM; normal successful FCEUPPU_LoadState is never called.
    saved = []
    for c in cores:
        size = c.call(c.core[16]); assert c.call(c.core[17], 0x83200000, size) == 1
        saved.append(c.m.read(0x83200000, size))
    assert saved[0] == saved[1]
    corrupt = bytearray(saved[0])
    at = corrupt.index(b'SPRA\x00\x01\x00\x00') + 8
    corrupt[at:at+256] = initial
    # Truncate inside the following field while keeping the declared length.
    corrupt = bytes(corrupt[:at+259])
    new.m.word(new.s['h1_sprite_oam_dirty'], 0)
    for c in cores:
        c.m.write(0x83200000, corrupt)
        assert c.call(c.core[18], 0x83200000, len(corrupt)) == 0
        assert c.m.read(c.s['SPRAM'],256) == initial
    assert word(new, 'h1_sprite_oam_dirty') == 1
    fetch('partial-failed-state'); fetch('partial-failed-state-stable')
    # Standalone reset is not linked by this frontend; Power contains its
    # inlined original FCEUPPU_Reset call and exercises both invalidations.
    for operation in ('FCEUPPU_Power',):
        new.m.word(new.s['h1_sprite_oam_dirty'], 0)
        for c in cores: c.call(c.s[operation])
        assert word(new, 'h1_sprite_oam_dirty') == 1
        fetch(operation); fetch(operation+'-stable')
    old.close(); new.close()
    report = dict(ok=True, cases=len(records), records=records,
                  baseline_sha256=hashlib.sha256(a.baseline_bda.read_bytes()).hexdigest(),
                  candidate_sha256=hashlib.sha256(a.bda.read_bytes()).hexdigest(),
                  scope='Actual MIPS complete state, XBuf/XDBuf, SPRBUF and PCM exact; real B2004, DMA, Power/inlined Reset, raw frame-boundary writes, partial failed state and midframe observer self-removal')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='records'},indent=2))


if __name__ == '__main__': main()
