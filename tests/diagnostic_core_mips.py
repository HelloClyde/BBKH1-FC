"""Actual-MIPS diagnostic mode/state lifecycle, not hardware performance.

Uses the real compiled cartridge adapter, CPU modes, save-state code, PPU/APU
and producer callbacks. Only H1 frontend/allocator services are mocked. The
allocator reuses freed blocks because the older generic harness is bump-only;
without reuse, repeated 8 MiB JIT opens would manufacture an artificial OOM.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from unittest.mock import patch

from h1_mock import Machine, ROOT
from jit_event_smoke import put, snapshot
from make_test_rom import make_rom
import profile_detail_mips as frame_module


ROM_SHA = '7b4fcd1c0be33e69943dcf9c81cca67bac634a9de2881aa2f3d8f9b6a3864117'


class RecyclingMachine(Machine):
    def __init__(self, *args, **kwargs):
        self.free_blocks = []
        self.allocated_sizes = {}
        self.fail_alloc = False
        super().__init__(*args, **kwargs)

    def alloc(self, size, *unused):
        if self.fail_alloc:
            return 0
        size = (size + 15) & ~15
        for index, (address, available) in enumerate(self.free_blocks):
            if available >= size:
                self.free_blocks.pop(index)
                if available > size:
                    self.free_blocks.append((address + size, available - size))
                self.allocations.add(address)
                self.allocated_sizes[address] = size
                return address
        result = super().alloc(size)
        self.allocated_sizes[result] = size
        return result

    def free(self, address, *unused):
        assert address in self.allocated_sizes, hex(address)
        self.free_blocks.append((address, self.allocated_sizes.pop(address)))
        self.free_blocks.sort()
        merged = []
        for start, size in self.free_blocks:
            if merged and merged[-1][0] + merged[-1][1] == start:
                old_start, old_size = merged.pop()
                merged.append((old_start, old_size + size))
            else:
                merged.append((start, size))
        self.free_blocks = merged
        return super().free(address)


def words(core, name, count):
    return struct.unpack('<' + 'I' * count, core.m.read(core.s[name], count * 4))


def real_input_mask(core):
    # The production environment advertises GET_INPUT_BITMASKS. The older
    # FrameCore fixture answered only individual button IDs, so ID 256 would
    # always read zero. Explicitly exercise the advertised bitmask contract.
    core.input_seen = []
    def read_input(port, device, index, button, *unused):
        if port or index:
            return 0
        if button == 256:
            core.input_seen.append(core.buttons)
            return core.buttons
        return (core.buttons >> button) & 1 if button < 16 else 0
    core.call(core.core[5], core.stub(0x80001b60, read_input))


def restore(core, state):
    core.m.write(0x83200000, state)
    assert core.call(core.core[18], 0x83200000, len(state)) == 1


def reset_video(core):
    if 'h1_nes_diag_video_reset' in core.s:
        core.call(core.s['h1_nes_diag_video_reset'])
    else:
        core.call(core.s['h1_nes_set_frameskip'], 0)
        core.call(core.s['h1_nes_set_frameskip'], 1)


def select(core, mode):
    before = snapshot(core)
    assert core.call(core.s['h1_nes_diag_mode'], mode) == 1
    assert snapshot(core) == before, ('mode-mutated-serialized-state', mode)
    assert core.word('h1_hle_sgz2_enabled') == int(mode == 2)
    assert bool(core.word('h1_jit_entries')) == bool(mode)
    assert core.call(core.s['h1_nes_diag_jit_bytes']) == 0
    assert words(core, 'h1_hle_stats', 3) == (0, 0, 0)
    counters = words(core, 'h1_jit_stats', 8)
    assert counters[:6] == (0,) * 6
    assert counters[7] == 0


def prepare_wait(core, state):
    restore(core, state)
    cpu = bytearray(28)
    struct.pack_into('<iH', cpu, 0, 0, 0xfb28)
    cpu[6:12] = bytes([0x35, 0xfe, 0x80, 0xfd, 4, 4])
    struct.pack_into('<iI', cpu, 16, 5000, 0)
    core.m.write(core.s['X'], bytes(cpu))
    ram = bytearray(core.m.read(core.s['RAM'], 2048))
    ram[0x62] = 1
    core.m.write(core.s['RAM'], bytes(ram))
    core.m.word(core.s['timestamp'], 0)
    core.m.word(core.s['sound_timestamp'], 0)
    put(core, 'fhcnt', 'h1_apu_fhcnt', 1000000)
    put(core, 'DMCacc', 'h1_apu_dmcacc', 1000000)
    put(core, 'DMCSize', 'h1_apu_dmcsize', 0)
    put(core, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
    put(core, 'IRQCount', 'h1_namco_irq_count', 0, 2)
    put(core, 'IRQa', 'h1_namco_irq_enabled', 1, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-bda', 'baseline-elf', 'bda', 'elf', 'nm', 'rom', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--frames', type=int, default=12)
    args = parser.parse_args()
    assert args.frames >= 8
    binary_hashes = {name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest()
                     for name in ('bda', 'elf', 'baseline_bda', 'baseline_elf')}
    rom = args.rom.read_bytes()
    assert hashlib.sha256(rom).hexdigest() == ROM_SHA
    reports = []
    with patch.object(frame_module, 'Machine', RecyclingMachine):
        old = frame_module.FrameCore(args.baseline_bda, args.baseline_elf, args.nm, rom, 1)
        new = frame_module.FrameCore(args.bda, args.elf, args.nm, rom, 1)
        real_input_mask(old);real_input_mask(new)
        old.call(old.s['h1_nes_jit_close'])
        assert new.call(new.s['h1_nes_diag_identity']) == 1
        for frame in range(8):
            assert old.frame() == new.frame(), ('boot-warmup', frame)
        initial = snapshot(new)
        assert initial == snapshot(old)
        expected_allocs = {}
        # Restore exactly the same guest snapshot and input after modes have
        # already run, so stale cache, HLE admission and display phase matter.
        for group, mode in enumerate((2, 0, 1, 0, 2, 1), 1):
            restore(old, initial); restore(new, initial)
            select(new, mode); reset_video(old); reset_video(new)
            live_allocations = len(new.m.allocations)
            assert expected_allocs.setdefault(mode, live_allocations) == live_allocations
            outputs = []
            for frame in range(args.frames):
                old.buttons = new.buttons = (1 << 8) if frame < 4 else 0
                left, right = old.frame(), new.frame()
                assert left == right, ('mode-frame-diff', group, mode, frame,
                                       [i for i, values in enumerate(zip(left, right)) if values[0] != values[1]])
                outputs.append(hashlib.sha256(right[0] + b''.join(right[1])).hexdigest())
            stats = words(new, 'h1_jit_stats', 8)
            if mode == 0:
                assert stats[0] == stats[3] == 0
                assert words(new, 'h1_hle_stats', 3) == (0, 0, 0)
            else:
                assert stats[0] > 0 and stats[3] > 0, ('native-path-not-covered', group, mode, stats)
            reports.append(dict(group=group, mode=mode, frames=args.frames,
                                compiled=stats[0], native_ops=stats[3],
                                hle=list(words(new, 'h1_hle_stats', 3)),
                                live_allocations=live_allocations, output_sha256=outputs))
        assert all(r['output_sha256'] == reports[0]['output_sha256'] for r in reports)
        assert (1 << 8) in old.input_seen and (1 << 8) in new.input_seen
        print('mode replay: six groups, byte-exact state/PCM/video/SRAM and real button masks passed', flush=True)
        # Directly exercise the real ROM wait loop, proving HLE admission does
        # not accidentally remain enabled in JIT-only/interpreter groups.
        wait_results = []
        for mode in (0, 1, 2, 0):
            select(new, mode)
            for repeat in range(4):
                prepare_wait(old, initial); prepare_wait(new, initial)
                old.call(old.s['X6502_Run'], 0);new.call(new.s['X6502_Run'], 0)
                assert old.m.read(old.s['X'], 28) == new.m.read(new.s['X'], 28)
                assert snapshot(old) == snapshot(new), ('wait-serialized', mode, repeat)
            hle = words(new, 'h1_hle_stats', 3)
            assert (hle[0] > 0) == (mode == 2), ('HLE-coverage', mode, hle)
            if mode == 1:
                assert words(new, 'h1_jit_stats', 8)[3] > 0
            wait_results.append(dict(mode=mode, hle=list(hle)))
        # Invalid mode must not silently select a different engine or free
        # the live arena. Explicit allocation failure must return failure.
        select(new, 2)
        before = (snapshot(new), new.word('h1_jit_entries'), set(new.m.allocations))
        assert new.call(new.s['h1_nes_diag_mode'], 3) == 0
        assert before == (snapshot(new), new.word('h1_jit_entries'), set(new.m.allocations))
        new.m.fail_alloc = True
        assert new.call(new.s['h1_nes_diag_mode'], 1) == 0
        assert new.word('h1_jit_entries') == new.word('h1_hle_sgz2_enabled') == 0
        new.m.fail_alloc = False;select(new, 0);select(new, 2)
        old.close();new.close()
        assert not old.m.allocated_sizes and not new.m.allocated_sizes
        rejected = []
        pal = bytearray(rom);pal[9] ^= 1
        changed = bytearray(rom);changed[0x100] ^= 1
        for name, candidate in (('header-region', bytes(pal)), ('trailing-byte', rom + b'\0'),
                                ('different-prg', bytes(changed)), ('original-nrom-fixture', make_rom())):
            core = frame_module.FrameCore(args.bda, args.elf, args.nm, candidate, 1)
            assert core.call(core.s['h1_nes_diag_identity']) == 0, name
            before = (core.word('h1_jit_entries'), set(core.m.allocations), snapshot(core))
            for mode in (0, 1, 2):
                assert core.call(core.s['h1_nes_diag_mode'], mode) == 0, (name, mode)
                assert before == (core.word('h1_jit_entries'), set(core.m.allocations), snapshot(core))
            core.close();assert not core.m.allocated_sizes
            rejected.append(name)
    report = dict(ok=True, bda_sha256=hashlib.sha256(args.bda.read_bytes()).hexdigest(),
                  baseline_sha256=hashlib.sha256(args.baseline_bda.read_bytes()).hexdigest(),
                  rom_sha256=ROM_SHA, frame_groups=reports, wait_modes=wait_results,
                  rejected_rom_profiles=rejected,
                  checks=['real MIPS mode entry and cold caches', 'public state restore and fixed input',
                          'byte-exact state/PCM/video/save buffers versus original interpreter',
                          'positive native and HLE coverage with negative disabled-mode assertions',
                          'repeat allocation balance, explicit OOM, invalid-mode non-mutation',
                          'canonical header/content identity negative controls'],
                  limits='Mock firmware services; no physical timing or throughput claim.')
    assert binary_hashes == {name: hashlib.sha256(getattr(args, name).read_bytes()).hexdigest()
                             for name in binary_hashes}, 'Input binaries changed during test'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': True, 'frame_groups': len(reports), 'frames': len(reports)*args.frames,
                      'wait_cases': 16, 'rejected_roms': rejected, 'output': str(args.output)}))


if __name__ == '__main__':
    main()
