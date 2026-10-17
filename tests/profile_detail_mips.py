"""Active sampled scopes on actual MIPS, compared with a normal core build.

Only the profile hardware-clock reader pc_now is replaced by a small MIPS
counter. GCC inlines h1_profile_now into push/pop, so replacing that public
symbol alone would not exercise active scopes without real TCU hardware.
CPU/JIT/PPU/APU code, the original ROMs and their execution are unchanged.
This validates semantics and accounting, never physical time or performance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2,
    UC_MIPS_REG_A3, UC_MIPS_REG_RA, UC_MIPS_REG_V0)
from h1_mock import Machine, ROOT
from make_test_rom import make_rom


KINDS = ('core', 'legacy_ppu', 'apu_finalize', 'screen', 'pcm', 'input', 'rom_io',
         'jit_compile', 'save', 'wait', 'other', 'cpu', 'native', 'slow_event',
         'completion', 'apu_event', 'apu_synth', 'mapper', 'ppu_loop', 'ppu_bg',
         'ppu_sprite', 'ppu_pixel')
MODES = (('control', 0), ('coarse', 1), ('cpu', 7), ('ppu', 9), ('sound', 21))
CALLS_OFFSET = 16 * len(KINDS) + 40
WINDOW_BYTES = 0x310


def symbol_info(elf, nm):
    output = subprocess.run([str(nm), '-n', '-S', str(elf)], check=True,
                            capture_output=True, text=True).stdout
    matches = re.findall(r'^([0-9a-f]+)\s+([0-9a-f]+)\s+\w\s+(\S+)$', output, re.M)
    return {name: (int(address, 16), int(size, 16)) for address, size, name in matches}


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FrameCore:
    """Run real frames with only frontend callbacks, without instruction hooks."""
    def __init__(self, bda, elf, nm, rom, skip):
        self.info = symbol_info(elf, nm)
        self.s = {name: value[0] for name, value in self.info.items()}
        path = 'A:\\FC\\LOCAL.nes'
        self.m = Machine(bda, files={path: rom}, path=path)
        self.core = struct.unpack('<19I', self.m.read(self.s['h1_nes_core'], 76))
        self.audio, self.video, self.buttons = [], [], 0
        self.call(self.core[1], self.s['environment'])
        self.call(self.core[2], self.stub(0x80001b00, self.receive_video))
        self.call(self.core[3], self.stub(0x80001b20, self.receive_audio))
        self.call(self.core[4], self.stub(0x80001b40, lambda *unused: 0))
        self.call(self.core[5], self.stub(0x80001b60,
                  lambda port, device, index, button, *unused: (self.buttons >> button) & 1))
        self.call(self.core[6])
        self.m.write(0x83000000, path.encode() + b'\0')
        self.m.write(0x83000040, struct.pack('<4I', 0x83000000, 0, 0, 0))
        assert self.call(self.core[8], 0x83000040) == 1
        self.call(self.s['h1_nes_set_frameskip'], skip)
        self.state_bytes = self.call(self.core[16])
        assert 0 < self.state_bytes < 1000000

    def stub(self, address, fn):
        self.m.callbacks[address] = fn
        self.m.write(address, struct.pack('<II', 0x03e00008, 0))
        return address

    def call(self, address, *args):
        self.m.finished = False
        for reg, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1,
                               UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.m.uc.reg_write(reg, value)
        self.m.uc.reg_write(UC_MIPS_REG_RA, 0x80001ff0)
        self.m.uc.emu_start(address, 0, timeout=30_000_000, count=100_000_000)
        assert self.m.finished, hex(address)
        return self.m.uc.reg_read(UC_MIPS_REG_V0)

    def receive_audio(self, data, length, *unused):
        self.audio.append(self.m.read(data, length * 4))
        return length

    def receive_video(self, data, width, height, pitch, *unused):
        self.video.append((width, height, pitch, b''.join(
            self.m.read(data + y * pitch, width * 2) for y in range(height)) if data else b''))
        return 0

    def word(self, name):
        return struct.unpack('<I', self.m.read(self.s[name], 4))[0]

    def frame(self, profiled=False):
        self.audio, self.video = [], []
        if profiled:
            self.call(self.s['h1_profile_push'], 0)
        self.call(self.core[10])
        if profiled:
            self.call(self.s['h1_profile_pop'])
        assert self.call(self.core[17], 0x83200000, self.state_bytes) == 1
        state = self.m.read(0x83200000, self.state_bytes)
        # Refresh the frontend save buffer from core SRAM before inspecting
        # the fixture's sprite-status marker, as core_equivalence does.
        self.call(self.core[15])
        save_size = self.call(self.core[13], 0)
        save_ptr = self.call(self.core[12], 0)
        return (state, tuple(self.audio), tuple(self.video),
                self.m.read(save_ptr, save_size) if save_size else b'')

    def install_clock(self):
        assert self.info['current'][1] == WINDOW_BYTES, self.info['current']
        assert self.info['pc_now'][1] >= 28
        # lui t0,hi; ori t0,lo; lw v0,0(t0); addiu v0,1; sw v0,0(t0); jr ra; nop.
        # The unmodified h1_profile_now/inlined equivalent increments
        # clock_reads itself. This replaces only the hardware source below it.
        address = self.s['pc_total']
        instructions = ((15 << 26) | (8 << 16) | (address >> 16),
                        (13 << 26) | (8 << 21) | (8 << 16) | (address & 65535),
                        (35 << 26) | (8 << 21) | (2 << 16),
                        (9 << 26) | (2 << 21) | (2 << 16) | 1,
                        (43 << 26) | (8 << 21) | (2 << 16), 0x03e00008, 0)
        self.m.write(self.s['pc_now'], struct.pack('<7I', *instructions))
        self.m.uc.ctl_flush_tb()

    def begin_mode(self, mask):
        assert self.word('depth') == 0
        self.m.write(self.s['current'], bytes(WINDOW_BYTES))
        for name, value in (('enabled', 1), ('frequency', 32768), ('fault', 0),
                            ('pc_fault', 0), ('pc_total', 0), ('clock_reads', 0),
                            ('last', 0), ('h1_profile_detail_mask', mask),
                            ('h1_profile_ppu_sample', 0)):
            self.m.word(self.s[name], value)

    def accounting(self, mask, frames):
        assert self.word('depth') == self.word('fault') == self.word('pc_fault') == 0
        assert self.word('enabled') == 1 and self.word('h1_profile_detail_mask') == mask
        n = len(KINDS)
        raw = self.m.read(self.s['current'], WINDOW_BYTES)
        ticks = struct.unpack_from('<' + 'Q' * n, raw, 0)
        inclusive = struct.unpack_from('<' + 'Q' * n, raw, n * 8)
        calls = struct.unpack_from('<' + 'I' * n, raw, CALLS_OFFSET)
        allowed = {0, 2, 7}
        if mask & 1: allowed.update((11, 18))
        if mask & 2: allowed.add(12)
        if mask & 4: allowed.update((13, 14, 15, 17))
        if mask & 8: allowed.update((19, 20, 21))
        if mask & 16: allowed.add(16)
        for kind in range(n):
            if kind not in allowed:
                assert calls[kind] == ticks[kind] == inclusive[kind] == 0, (mask, KINDS[kind])
            if kind >= 11:
                assert inclusive[kind] >= ticks[kind], (mask, KINDS[kind], 'inclusive')
        assert calls[0] == frames
        assert calls[2] == frames
        if mask & 1: assert calls[11] > 0 and calls[18] > 0
        if mask & 2: assert calls[12] > 0
        if mask & 4: assert calls[13] > 0 and calls[15] > 0
        if mask & 8: assert calls[18] > 0
        if mask & 16: assert calls[16] > 0
        # Every root push costs one deterministic tick outside all scopes;
        # every other clock step belongs to exactly one exclusive scope.
        assert self.word('clock_reads') == self.word('pc_total')
        assert sum(ticks) == self.word('pc_total') - frames
        return dict(mask=mask, frames=frames, depth=0, fault=0,
                    clock_reads=self.word('clock_reads'),
                    calls={KINDS[i]: value for i, value in enumerate(calls) if value},
                    exclusive_ticks=dict(zip(KINDS, ticks)),
                    inclusive_ticks=dict(zip(KINDS, inclusive)))

    def close(self):
        if 'h1_profile_detail_mask' in self.s:
            self.m.word(self.s['h1_profile_detail_mask'], 0)
            self.m.word(self.s['enabled'], 0)
        self.call(self.core[7])
        assert not self.m.allocations and not self.m.handles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-bda', 'baseline-elf', 'bda', 'elf', 'nm', 'rom', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--frames-per-mode', type=int, default=4)
    parser.add_argument('--warmup-frames', type=int, default=8)
    args = parser.parse_args()
    assert args.frames_per_mode >= 2 and args.warmup_frames >= 8
    reports = []
    fixture = make_rom(dmc=True, sprites=True)
    for label, rom, skip in (('sgz2', args.rom.read_bytes(), 0),
                              ('original-dmc-sprites', fixture, 0),
                              ('original-dmc-sprites-skip1', fixture, 1)):
        old = FrameCore(args.baseline_bda, args.baseline_elf, args.nm, rom, skip)
        new = FrameCore(args.bda, args.elf, args.nm, rom, skip)
        for frame in range(args.warmup_frames):
            assert old.frame() == new.frame(), (label, 'warmup', frame)
        new.install_clock()
        modes = []
        sprite_seen = False
        for mode, mask in MODES:
            new.begin_mode(mask)
            hashes = []
            for frame in range(args.frames_per_mode):
                old.buttons = new.buttons = 1 if frame & 1 else 0
                before = old.frame()
                after = new.frame(profiled=True)
                assert before == after, (label, mode, frame,
                    [i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]])
                assert new.word('depth') == 0 and new.word('fault') == 0
                if len(before[3]) > 6:
                    sprite_seen |= (before[3][6] & 0x60) == 0x60
                hashes.append({'state': digest(after[0]), 'pcm': digest(b''.join(after[1])),
                    'video': digest(b''.join(v[3] for v in after[2])), 'save': digest(after[3])})
            record = new.accounting(mask, args.frames_per_mode)
            record.update(mode=mode, frame_hashes=hashes)
            modes.append(record)
            print('PASS', label, mode, record['calls'], flush=True)
        if label.startswith('original'):
            assert sprite_seen, (label, 'sprite-zero hit and overflow were not exercised')
            assert struct.unpack('<i', new.m.read(new.s['h1_apu_clock'] + 8, 4))[0] > 0, 'DMC not running'
        reports.append(dict(name=label, rom_sha256=digest(rom), skip=skip,
                            warmup_frames=args.warmup_frames, modes=modes))
        old.close(); new.close()
    report = dict(ok=True, baseline_bda_sha256=digest(args.baseline_bda.read_bytes()),
                  bda_sha256=digest(args.bda.read_bytes()), tests=reports,
                  modification='Only pc_now hardware-clock reader replaced with a seven-instruction MIPS counter; profiler enabled/frequency/mask initialized by test.',
                  limitations=['Synthetic ticks validate scope balance and exclusive/inclusive accounting, not physical time or speed.',
                               'Masks are selected explicitly to exercise every active mode; natural 155-frame rotation is covered separately.',
                               'SGZ2 coverage is boot/intro frames, not the 1800-frame city benchmark.',
                               'Core execution uses frontend callbacks; firmware pacing, TCU MMIO and full application IO are outside this test.'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('PASS active MIPS profile differential', args.output)


if __name__ == '__main__':
    main()
