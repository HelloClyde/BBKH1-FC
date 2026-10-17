"""Observe native dispatch returns without modifying guest code or state.

Return categories describe observable exit state, not every internal branch
reason. In particular, 'other' includes IO boundaries, link misses and safe-run
rejections. This diagnostic is separate from the performance comparison.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import struct

from unicorn.mips_const import (UC_MIPS_REG_RA, UC_MIPS_REG_A0,
                               UC_MIPS_REG_A1, UC_MIPS_REG_V0)
from jit_app_long_benchmark import LongApplication, LOG_PATH, ROOT


class DispatchCensus(LongApplication):
    def __init__(self, *args, from_frame=1, **kwargs):
        self.from_frame = from_frame
        self.pending = {}
        self.entries = Counter()
        self.exits = Counter()
        self.opcodes = Counter()
        self.lengths = Counter()
        self.calls = self.native_ops = 0
        super().__init__(*args, **kwargs)
        start = self.syms['h1_cpu_run_jit']
        end = min(v for v in self.syms.values() if v > start)
        self.dispatch_sites = []
        for address in range(start, end, 4):
            word = struct.unpack('<I', self.read(address, 4))[0]
            # JALR rs, ra. Other callbacks in this function are ignored at run
            # time by checking the actual target and the CPU/RAM arguments.
            if word >> 26 == 0 and word & 63 == 9 and (word >> 11) & 31 == 31:
                self.dispatch_sites.append(address)
        assert self.dispatch_sites, 'No native dispatch call site found'
        self.return_sites = {v + 8 for v in self.dispatch_sites}

    def count_block(self, uc, address, size, user):
        super().count_block(uc, address, size, user)
        if self.frame < self.from_frame:
            return
        if address in self.pending:
            self.after_call(uc, address, size, user)
            return
        if self.pending or not 0x81000000 <= address < 0x83000000:
            return
        return_site = uc.reg_read(UC_MIPS_REG_RA)
        if return_site not in self.return_sites:
            return
        if uc.reg_read(UC_MIPS_REG_A0) != self.syms['X']:
            return
        if uc.reg_read(UC_MIPS_REG_A1) != self.syms['RAM']:
            return
        # The current shared O32 prologue installs its compile-time PC in
        # S2 at word 12. X.PC may still describe the previous interpreter
        # boundary, so it must not be used as this call's entry identity.
        pc_word = struct.unpack('<I', self.read(address + 12 * 4, 4))[0]
        assert pc_word & 0xffff0000 == 0x34120000, 'Unknown native prologue'
        pc = pc_word & 65535
        self.pending[return_site] = pc

    def after_call(self, uc, address, size, user):
        entry = self.pending.pop(address, None)
        if entry is None:
            return
        result = uc.reg_read(UC_MIPS_REG_V0)
        executed = (result >> 16) & 0x7fff
        pc = result & 65535
        cpu = self.read(self.syms['X'], 28)
        count = struct.unpack_from('<i', cpu, 16)[0]
        irq = struct.unpack_from('<I', cpu, 20)[0]
        self.calls += 1
        self.native_ops += executed
        self.entries[f'{entry:04x}'] += 1
        self.lengths[executed] += 1
        if result & 0x80000000:
            reason = 'invalidated'
        elif count <= 0:
            reason = 'cpu_budget_exhausted'
        elif irq:
            reason = 'irq_present_not_necessarily_serviceable'
        else:
            reason = 'other_boundary_or_link_miss'
        self.exits[reason] += 1
        if pc >= 0x8000:
            handler = struct.unpack('<I', self.read(self.syms['ARead'] + pc * 4, 4))[0]
            if handler == self.syms['CartBR']:
                page = struct.unpack('<I', self.read(self.syms['Page'] + (pc >> 11) * 4, 4))[0]
                opcode = self.read((page + pc) & 0xffffffff, 1)[0]
                supported = self.read(self.syms['h1_jit_modes'] + opcode, 1)[0]
                self.opcodes[f'{opcode:02x}:mode{supported}'] += 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    p.add_argument('--rom', type=Path, required=True)
    p.add_argument('--nm', type=Path, default=ROOT.parent / '.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    p.add_argument('--frames', type=int, default=360)
    p.add_argument('--from-frame', type=int, default=241)
    p.add_argument('--input-script', type=Path)
    p.add_argument('--menu-exit', action='store_true',
                   help='Open the pause menu with P and confirm Exit, including on older builds')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    assert 1 <= a.from_frame <= a.frames
    schedule = json.loads(a.input_script.read_text()) if a.input_script else []
    out = a.output.parent / a.output.stem
    machine = DispatchCensus(a.bda, a.elf, a.nm, a.rom.read_bytes(), a.frames,
                             schedule, out, False, menu_exit=a.menu_exit,
                             from_frame=a.from_frame)
    seconds = machine.run_long()
    assert machine.calls, 'Native dispatch hooks did not observe calls'
    assert not machine.pending
    log = machine.files[LOG_PATH].decode('gbk')
    if a.from_frame == 1:
        close = re.search(r'JIT_CLOSE .*hits=(\d+) .*native_ops=(\d+)', log)
        assert close and (machine.calls, machine.native_ops) == tuple(map(int, close.groups()))
    out.mkdir(parents=True, exist_ok=True)
    (out / 'h1fc.log').write_bytes(machine.files[LOG_PATH])
    result = {'ok': True, 'bda_sha256': hashlib.sha256(a.bda.read_bytes()).hexdigest(),
              'elf_sha256': hashlib.sha256(a.elf.read_bytes()).hexdigest(),
              'rom_sha256': hashlib.sha256(a.rom.read_bytes()).hexdigest(),
              'frames': a.frames, 'from_frame': a.from_frame, 'events': schedule,
              'exit_protocol': 'p-menu-confirm' if a.menu_exit else 'legacy-direct',
              'seconds_diagnostic_only': seconds,
              'native_calls': machine.calls, 'native_ops': machine.native_ops,
              'mean_ops_per_call': machine.native_ops / machine.calls,
              'zero_op_calls': machine.lengths[0],
              'return_categories': dict(machine.exits),
              'top_return_opcodes': machine.opcodes.most_common(20),
              'top_entry_pcs': machine.entries.most_common(20),
              'top_call_lengths': machine.lengths.most_common(20),
              'dispatch_sites': [hex(v) for v in machine.dispatch_sites],
              'limitation': 'Read-only guest observation; categories are exit-state classifications, not a proof of each internal exit branch. Host time includes diagnostic hooks and is not a performance metric.'}
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
