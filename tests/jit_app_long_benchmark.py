"""Deterministic complete-release-BDA comparison, including audible scenes.

The application is not patched: symbol hooks only observe executed code, and
the normal firmware event queue supplies keys and exits on the requested frame.
All core, input, video/scaling, PCM, pacing, save and teardown code executes.
Firmware services are mocked. Counts describe MIPS block instruction entries,
not physical H1 cycles/FPS. Local ROMs and screenshots are never distributed.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time
import zlib

from PIL import Image
from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE
from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1,
                               UC_MIPS_REG_PC, UC_MIPS_REG_SP, UC_MIPS_REG_V0)
from core_equivalence import symbols
from h1_mock import Machine, ROOT

SCHEMA = 1
ROM_PATH = 'A:\\FC\\BENCH.nes'
LOG_PATH = 'A:\\FC\\h1fc.log'
BATTERY_SLOTS = (ROM_PATH + '.s0', ROM_PATH + '.s1')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_observation(files):
    """Read and validate actual persisted slots, retaining generation evidence."""
    slots = {}
    for path, data in files.items():
        if not path.endswith(('.s0', '.s1')):
            continue
        assert len(data) >= 28, ('Truncated save header', path)
        magic, version, rom_id, generation, size, crc, header_crc = struct.unpack('<7I', data[:28])
        assert magic == 0x31534748 and version == 1, ('Invalid save header', path)
        assert len(data) == 28 + size, ('Invalid save length', path)
        assert zlib.crc32(data[:24]) == header_crc, ('Invalid header CRC', path)
        assert zlib.crc32(data[28:]) == crc, ('Invalid payload CRC', path)
        slots[path] = {'generation': generation, 'rom_id': rom_id,
                       'bytes': size, 'payload': digest(data[28:])}
    return slots


def latest_save(result):
    """Find this ROM's final battery slot, excluding config/state save domains."""
    payloads = {path: payload for path, payload in result['save_payloads'].items()
                if path in BATTERY_SLOTS}
    if 'save_slots' in result:
        slots = {path: slot for path, slot in result['save_slots'].items()
                 if path in BATTERY_SLOTS}
        if not slots:
            return {'payload': None, 'evidence': 'validated slot headers; no saves'}
        # A run starts from an empty filesystem and cannot wrap 2**32 writes.
        path = max(slots, key=lambda p: slots[p]['generation'])
        return {'path': path, **slots[path], 'evidence': 'validated slot headers'}
    # Schema-1 caches retained only payload hashes. The pinned save code starts
    # at slot=-1/generation=0 and alternates slots after each verified success.
    # The complete success log can therefore establish the latest slot without
    # assuming that either hash happened to match the candidate's final state.
    log = result['log']
    assert len(re.findall(r'SAVE_LOAD_END ', log)) == 1, 'Multiple save sessions in cache'
    assert re.search(r'SAVE_LOAD_END status=0 slot=-1 generation=0\b', log), 'Unknown initial save generation'
    assert not re.search(r'SAVE_CHECKPOINT_DUMP .*omitted=[1-9]\d*', log), 'Save log omitted generations'
    checkpoints = [int(v) for v in re.findall(r'SAVE_CHECKPOINT_END status=(-?\d+)', log)]
    exits = [int(v) for v in re.findall(r'SAVE_EXIT_END status=(-?\d+)', log)]
    assert len(exits) == 1 and all(v >= 0 for v in checkpoints + exits), 'Incomplete/failed save history'
    writes = sum(v == 1 for v in checkpoints + exits)
    if not writes:
        assert not payloads, 'Unexplained persisted battery save'
        return {'payload': None, 'evidence': 'complete verified-write log; no saves'}
    slot = (writes - 1) & 1
    path = BATTERY_SLOTS[slot]
    assert path in payloads, 'Final battery save slot cannot be established'
    return {'path': path, 'generation': writes, 'payload': payloads[path],
            'evidence': 'schema-1 cache: complete verified-write log and alternating slots'}


def compare_storage_audio(old, new):
    """Explicit policy for changes to host playback/storage scheduling only.

    Core execution is always exact. Display differences are accepted only when
    a real presentation counter proves that at least one side held the LCD.
    Legacy hashes alone cannot prove a hold: unresolved differences fail while
    retaining their evidence and measured costs in the report.
    """
    failures, lcd_differences = [], []
    assert len(old['frames']) == len(new['frames']), 'Frame count differs'
    for index, (first, second) in enumerate(zip(old['frames'], new['frames'])):
        assert first['frame'] == second['frame'] == index + 1
        for field in ('pcm', 'pcm_frames', 'cpu_ppu_state', 'declared_state'):
            assert field in first and field in second, ('Required core observation missing', field)
            if first[field] != second[field]:
                failures.append({'frame': index + 1, 'field': field, 'kind': 'core_mismatch'})
        for side, records, item in (('baseline', old['frames'], first), ('current', new['frames'], second)):
            if item.get('presented') is False and index and item['lcd'] != records[index - 1]['lcd']:
                failures.append({'frame': index + 1, 'field': 'lcd', 'side': side,
                                 'kind': 'LCD changed without presentation'})
        if first['lcd'] == second['lcd']:
            continue
        presented = [first.get('presented'), second.get('presented')]
        if presented == [True, True]:
            kind = 'shared_presentation_mismatch'
        elif False in presented:
            kind = 'observed_display_cadence_difference'
        else:
            kind = 'unresolved_visual_difference'
        evidence = {'frame': index + 1, 'kind': kind,
                    'baseline_presented': presented[0], 'current_presented': presented[1],
                    'baseline_lcd': first['lcd'], 'current_lcd': second['lcd']}
        lcd_differences.append(evidence)
        if kind != 'observed_display_cadence_difference':
            failures.append(evidence)
    for field in ('core_pcm', 'pcm_frames'):
        if old[field] != new[field]:
            failures.append({'field': field, 'kind': 'core_mismatch'})
    latest = {}
    for side, result in (('baseline', old), ('current', new)):
        try:
            latest[side] = latest_save(result)
        except AssertionError as error:
            failures.append({'field': 'latest_save', 'side': side, 'kind': 'unresolved_save_history',
                             'reason': str(error)})
    if len(latest) == 2 and latest['baseline']['payload'] != latest['current']['payload']:
        failures.append({'field': 'latest_save', 'kind': 'final_save_mismatch'})
    other_saves = [{path: payload for path, payload in result['save_payloads'].items()
                    if path not in BATTERY_SLOTS} for result in (old, new)]
    if other_saves[0] != other_saves[1]:
        failures.append({'field': 'save_payloads', 'kind': 'other_save_domain_mismatch',
                         'baseline': other_saves[0], 'current': other_saves[1]})
    if old['frames'][-1]['lcd'] == new['frames'][-1]['lcd'] and old['screen'] != new['screen']:
        failures.append({'field': 'screen', 'kind': 'final_screen_mismatch'})
    fields = ('screen', 'save_payloads', 'rendered_frames', 'audio_catchup_frames',
              'submitted_ring_pcm', 'observed_ring_pcm', 'gameplay_submitted_ring_pcm',
              'gameplay_observed_ring_pcm', 'exit_screen')
    differences = {field: {'baseline': old.get(field), 'current': new.get(field)}
                   for field in fields if old.get(field) != new.get(field)}
    return {'ok': not failures, 'policy': 'storage-audio-v1', 'failures': failures,
            'lcd_differences': lcd_differences, 'differences': differences, 'latest_save': latest,
            'core_observations_exact': not any(f['kind'] == 'core_mismatch' for f in failures),
            'cost_scope': 'Complete app costs include changes in display cadence, playback/ring handling, '
                          'save timing and teardown; this is not a fixed-work CPU speedup.',
            'display_limit': 'Unchanged legacy LCD hashes prove identical observed output, not presentation. '
                             'Different hashes require observed holds or exact shared-frame presentation; '
                             'unknown presentation status is a failed, unresolved visual comparison.'}


def identity(rom, frames, schedule, census, declared_state=False,
             allow_audio_catchup=False, menu_exit=False, skip=None):
    result = {'schema': SCHEMA, 'rom_sha256': digest(rom), 'frames': frames,
            'settings': {'scale': 1, 'skip': 0, 'sound': 1, 'volume': 100,
                         'save': 'new, empty initial filesystem'},
            'events': schedule, 'cost_census': census,
            'clock': 'Mock firmware milliseconds: 32 PCM samples consumed per timer query',
            'stop': 'Escape key-down through firmware queue at final input_frame entry'}
    if declared_state:
        result['state_observation'] = 'Read-only SFORMAT tables, without save callbacks'
    if allow_audio_catchup:
        result['render_policy'] = 'Allow audio catch-up skips; require exact baseline render/catch-up counts'
    if menu_exit:
        result['stop'] = ('P key-down at final input_frame; confirm Exit in the pause menu. '
                          'Freeze final gameplay observation at first audio pause/close; '
                          'total cost includes actual menu/exit, teardown reported separately')
    if skip is not None:
        result['settings']['skip'] = skip
        result['initial_config'] = 'CRC-protected version-2 ROM config, loaded by unmodified application'
    return result


class LongApplication(Machine):
    def __init__(self, bda, elf, nm, rom, frames, schedule, output_dir, census,
                 declared_state=False, menu_exit=False, count_instructions=True, skip=None):
        self.syms = symbols(elf, nm)
        self.target_frames = frames
        self.schedule = schedule
        self.output_dir = output_dir
        self.records = []
        self.frame = 0
        self.instructions = 0
        self.startup = self.frame_start = 0
        self.audio_this_frame = bytearray()
        self.audio_all = hashlib.sha256()
        self.audio_samples_total = 0
        self.audio_nonzero_total = 0
        self.last_video_count = 0
        self.function_costs = {}
        self.cost_census = census
        self.count_instructions = count_instructions
        assert count_instructions or not census, 'Cost census requires instruction counting'
        self.declared_state = declared_state
        self.menu_exit = menu_exit
        self.benchmark_exit_actions = [(10, 31), (9, 37), (10, 37), (9, 39)]
        self.exit_menu_observed = False
        self.final_gameplay_screen = None
        self.gameplay_submitted_ring_pcm = self.gameplay_observed_ring_pcm = None
        if census:
            listing = subprocess.run([str(nm), '-n', str(elf)], check=True,
                                     capture_output=True, text=True).stdout
            self.functions = [(int(a, 16), n) for a, n in
                              re.findall(r'^([0-9a-f]+)\s+[Tt]\s+(\S+)$', listing, re.M)]
            self.function_starts = [f[0] for f in self.functions]
        files = {ROM_PATH: rom}
        if skip is not None:
            from options_smoke import config_slot
            files[ROM_PATH + '.cfg.s0'] = config_slot(rom, skip)
        super().__init__(bda, files=files, path=ROM_PATH)
        if count_instructions:
            self.uc.hook_add(UC_HOOK_BLOCK, self.count_block)
        for name, callback in [('input_frame', self.begin_frame),
                               ('gba_audio_write', self.capture_audio),
                               ('gba_audio_close', self.before_close)]:
            addr = self.syms[name]
            self.uc.hook_add(UC_HOOK_CODE, callback, begin=addr, end=addr)
        if menu_exit:
            addr = self.syms['gba_audio_pause']
            self.uc.hook_add(UC_HOOK_CODE, self.before_close, begin=addr, end=addr)

    def count_block(self, uc, address, size, user):
        amount = size // 4
        self.instructions += amount
        if self.cost_census:
            if 0x81000000 <= address < 0x83000000:
                name = '[generated MIPS]'
            elif address < 0x83c00020:
                name = '[firmware stubs]'
            else:
                name = self.functions[bisect.bisect_right(self.function_starts, address) - 1][1]
            self.function_costs[name] = self.function_costs.get(name, 0) + amount

    def begin_frame(self, uc, address, size, user):
        if self.frame:
            self.finish_frame()
        else:
            self.startup = self.instructions
        self.frame += 1
        assert self.frame <= self.target_frames, ('Frame bound failed', self.frame)
        self.frame_start = self.instructions
        for event in self.schedule:
            if event['frame'] == self.frame:
                self.events.append((9 if event['down'] else 10, event['key']))
        if self.frame == self.target_frames:
            # P opens the application menu; Escape remains only for replaying
            # historical direct-exit benchmarks. Neither path patches state.
            self.events.append((9, 31 if self.menu_exit else 24))

    def capture_audio(self, uc, address, size, user):
        pointer = uc.reg_read(UC_MIPS_REG_A0)
        frames = uc.reg_read(UC_MIPS_REG_A1)
        self.audio_this_frame.extend(self.read(pointer, frames * 4))

    def before_close(self, uc, address, size, user):
        if self.frame and len(self.records) < self.frame:
            assert self.frame == self.target_frames
            self.finish_frame()
            if self.menu_exit:
                self.capture_game_buffer()
                self.final_gameplay_screen = self.screen
                self.gameplay_submitted_ring_pcm = digest(b''.join(self.audio_submissions))
                self.gameplay_observed_ring_pcm = digest(b''.join(self.audio_observed))

    def declared_state_digest(self):
        """Observe all pinned FCEUmm state tables without executing guest code.

        This is intentionally not a serialized savestate: FCEUPPU_SaveState and
        mapper pre/post-save callbacks are not called, so their shadow fields
        retain the application's current values. Direct APU, mapper and input
        entries nevertheless extend the basic CPU/PPU regression observations.
        Exclude pointers, whose addresses can change between linked builds.
        """
        result = hashlib.sha256()

        def observe(address, depth=0):
            assert depth < 8
            for index in range(1024):
                entry = self.read(address + index * 16, 16)
                pointer, size = struct.unpack('<II', entry[:8])
                if not pointer:
                    return
                if size == 0xffffffff:
                    observe(pointer, depth + 1)
                else:
                    count = size & 0x00ffffff
                    assert count <= 1_000_000
                    result.update(entry[8:12])
                    result.update(struct.pack('<I', count))
                    result.update(self.read(pointer, count))
            raise AssertionError('Unterminated SFORMAT table')

        for name in ('SFCPU', 'SFCPUC', 'FCEUPPU_STATEINFO',
                     'FCEUCTRL_STATEINFO', 'FCEUSND_STATEINFO', 'SFMDATA'):
            result.update(name.encode('ascii') + b'\0')
            observe(self.syms[name])
        return result.hexdigest()

    def finish_frame(self):
        assert len(self.records) == self.frame - 1
        lcd = self.read(0x82000000, 480 * 272 * 4)
        pcm = bytes(self.audio_this_frame)
        self.audio_all.update(pcm)
        samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
        nonzero = sum(s != 0 for s in samples)
        self.audio_samples_total += len(pcm) // 4
        self.audio_nonzero_total += nonzero
        state = b''.join(self.read(self.syms[name], size) for name, size in
                         [('RAM', 2048), ('X', 28), ('PPU', 4), ('NTARAM', 2048),
                          ('PALRAM', 32), ('SPRAM', 256)])
        self.records.append({'frame': self.frame,
                             'lcd': digest(lcd), 'pcm': digest(pcm),
                             'pcm_frames': len(pcm) // 4, 'pcm_nonzero_samples': nonzero,
                             'cpu_ppu_state': digest(state)})
        if self.count_instructions:
            self.records[-1]['block_instructions'] = self.instructions - self.frame_start
        # Read-only extra evidence, deliberately outside the scenario identity:
        # existing schema-1 execution/cache semantics remain reusable.
        video_count = struct.unpack('<I', self.read(self.syms['videos'], 4))[0]
        presentations = video_count - self.last_video_count
        assert presentations in (0, 1), ('Unexpected presentations per core frame', self.frame, presentations)
        self.records[-1].update(presented=bool(presentations), video_count=video_count)
        self.last_video_count = video_count
        if self.declared_state:
            self.records[-1]['declared_state'] = self.declared_state_digest()
        self.audio_this_frame.clear()
        if self.frame % 60 == 0 or self.frame == self.target_frames:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            image = Image.frombytes('RGB', (480, 272), lcd, 'raw', 'BGRX')
            image.save(self.output_dir / ('frame-%04d.png' % self.frame))
            progress = {'progress_frame': self.frame,
                        'pcm_nonzero_samples': self.audio_nonzero_total}
            if self.count_instructions:
                progress['instructions'] = self.instructions
            print(json.dumps(progress), flush=True)

    def event(self, code, key, *args):
        if self.frame == self.target_frames and not self.events:
            log = self.files.get(LOG_PATH, b'')
            paused = log.rfind(b'PAUSE_MENU_BEGIN') > log.rfind(b'PAUSE_MENU_END')
            if paused:
                assert self.menu_exit, 'This BDA opens a pause menu; pass --menu-exit'
                self.exit_menu_observed = True
                assert self.benchmark_exit_actions, 'Exit menu did not close'
                self.events.append(self.benchmark_exit_actions.pop(0))
        event = self.events.pop(0) if self.events else (-1, -1)
        self.word(code, event[0]); self.word(key, event[1])
        return 0

    def clock(self, *args):
        # Same guest clock and playback cursor as Machine.clock; coalesced host
        # reads avoid 32 Python/Unicorn calls per millisecond of captured PCM.
        self.ticks += 1
        if self.audio_active and self.audio_queue and self.audio_consumes:
            first = min(32, self.audio_samples - self.audio_cursor)
            self.audio_observed.append(self.read(self.audio_base + self.audio_cursor * 2, first * 2))
            if first < 32:
                self.audio_observed.append(self.read(self.audio_base, (32 - first) * 2))
            self.audio_cursor = (self.audio_cursor + 32) % self.audio_samples
            self.word(0x80006108, self.audio_base + self.audio_cursor * 2)
        return self.ticks

    def run_long(self):
        started = time.monotonic()
        # One uninterrupted execution avoids a host budget cutting a counted
        # MIPS basic block halfway through and counting its tail again after a
        # resume. This is still bounded, and the command runner can poll it.
        self.uc.emu_start(0x83c00020, 0,
                          timeout=max(300, self.target_frames * 4) * 1_000_000,
                          count=max(500_000_000, self.target_frames * 20_000_000))
        assert self.finished, ('MIPS did not return', hex(self.uc.reg_read(UC_MIPS_REG_PC)))
        assert self.uc.reg_read(UC_MIPS_REG_V0) == 0
        assert self.uc.reg_read(UC_MIPS_REG_SP) == 0x83aff000
        for i, reg in enumerate(self.saved_regs):
            assert self.uc.reg_read(reg) == 0x12340000 + i
        assert not self.handles and not self.allocations
        assert not self.gui_active and self.gui_opens == self.gui_closes
        assert not self.audio_active and not self.audio_initialized and not self.audio_queue
        assert not self.dialogs
        assert len(self.records) == self.target_frames
        return time.monotonic() - started


def execute(bda, elf, nm, rom, frames, schedule, output_dir, census=False,
            declared_state=False, allow_audio_catchup=False, menu_exit=False, skip=None):
    machine = LongApplication(bda, elf, nm, rom, frames, schedule, output_dir,
                              census, declared_state, menu_exit, skip=skip)
    seconds = machine.run_long()
    log = machine.files[LOG_PATH].decode('gbk')
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'h1fc.log').write_text(log, encoding='utf-8')
    assert 'APP_END result=0' in log
    expected_skip = 0 if skip is None else skip
    assert f'scale=1 skip={expected_skip} sound=1 volume=100' in log
    catchup = re.search(r'AUDIO_CATCHUP frames=(\d+) .*config_skip=' + str(expected_skip), log)
    video = re.search(r'STOP video=(\d+) errors=0', log)
    assert catchup and video, log
    catchup_frames, rendered_frames = int(catchup.group(1)), int(video.group(1))
    assert 0 < rendered_frames <= frames and 0 <= catchup_frames <= frames, log
    if not allow_audio_catchup:
        assert catchup_frames == 0 and rendered_frames == (frames + expected_skip) // (expected_skip + 1), log
    assert re.search(r'AUDIO_CLOSE_BEGIN .*dropped=0 failures=0 underruns=0', log), log
    native = re.search(r'JIT_CLOSE.*native_ops=(\d+)', log)
    save = {p: digest(data[28:]) for p, data in machine.files.items()
            if p.endswith(('.s0', '.s1'))}
    segments = []
    for start in range(0, frames, 60):
        window = machine.records[start:start + 60]
        segments.append({'start_frame': start + 1, 'end_frame': start + len(window),
                         'block_instructions': sum(r['block_instructions'] for r in window),
                         'pcm_nonzero_samples': sum(r['pcm_nonzero_samples'] for r in window)})
    result = {'bda_sha256': digest(bda.read_bytes()), 'elf_sha256': digest(elf.read_bytes()),
              'block_instructions': machine.instructions, 'frames': machine.records,
              'startup_instructions': machine.startup,
              'teardown_instructions': machine.instructions - machine.startup -
                  sum(r['block_instructions'] for r in machine.records),
              'segments': segments, 'seconds_host': seconds,
              'screen': digest(machine.final_gameplay_screen if menu_exit else machine.screen),
              'save_payloads': save,
              'save_slots': save_observation(machine.files),
              'core_pcm': machine.audio_all.hexdigest(),
              'pcm_frames': machine.audio_samples_total,
              'pcm_nonzero_samples': machine.audio_nonzero_total,
              'rendered_frames': rendered_frames, 'audio_catchup_frames': catchup_frames,
              'submitted_ring_pcm': digest(b''.join(machine.audio_submissions)),
              'observed_ring_pcm': digest(b''.join(machine.audio_observed)),
              'native_ops': int(native.group(1)) if native else 0, 'log': log}
    if menu_exit:
        assert machine.final_gameplay_screen is not None
        assert machine.exit_menu_observed and 'PAUSE_MENU_END action=2' in log
        result.update(exit_method='p-menu-confirm',
                      exit_screen=digest(machine.screen),
                      gameplay_submitted_ring_pcm=machine.gameplay_submitted_ring_pcm,
                      gameplay_observed_ring_pcm=machine.gameplay_observed_ring_pcm)
    if census:
        result['function_costs'] = dict(sorted(machine.function_costs.items(), key=lambda p: -p[1]))
    if hasattr(machine, 'counter_source_sha256'):
        result['counter_backend'] = {'kind': 'host-C UC_HOOK_BLOCK size/4',
                                     'source_sha256': machine.counter_source_sha256}
    return result


def main():
    global LongApplication
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-bda', type=Path, default=ROOT / 'build/jit-v3-baseline/release-jit/H1FC.bda')
    parser.add_argument('--baseline-elf', type=Path, default=ROOT / 'build/jit-v3-baseline/release-jit/H1FC.elf')
    parser.add_argument('--bda', type=Path, default=ROOT / 'dist/H1FC-jit.bda')
    parser.add_argument('--elf', type=Path, default=ROOT / 'build/release-jit/H1FC.elf')
    parser.add_argument('--nm', type=Path, default=ROOT.parent / '.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    parser.add_argument('--rom', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=600)
    parser.add_argument('--input-script', type=Path, help='JSON list of {frame,key,down} firmware events')
    parser.add_argument('--skip', type=int, choices=(0, 1, 2),
                        help='Load an actual per-ROM config with this skip setting into both builds; '
                             'omitted preserves historical empty-filesystem skip-0 scenarios')
    parser.add_argument('--cost-census', action='store_true')
    parser.add_argument('--native-counter', action='store_true',
                        help='Use parity-checked host-C block counting to reduce test host overhead; requires host gcc')
    parser.add_argument('--declared-state', action='store_true',
                        help='Read-only hash of CPU/PPU/APU/input/mapper SFORMAT tables; no guest save callbacks')
    parser.add_argument('--allow-audio-catchup', action='store_true',
                        help='For longer scenarios with native audio catch-up; baseline/candidate must match exactly')
    parser.add_argument('--menu-exit', action='store_true',
                        help='Open the menu with P and confirm Exit, including on older baselines. '
                             'Uses a separate cache identity; final gameplay observation precedes menu/teardown')
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--baseline-cache', type=Path)
    parser.add_argument('--current-cache', type=Path,
                        help='Reuse a verified current/baseline result to compare against another baseline')
    parser.add_argument('--allow-silent', action='store_true', help='Only for short harness regression checks')
    parser.add_argument('--max-ratio', type=float)
    parser.add_argument('--comparison-policy', choices=('strict', 'storage-audio-v1'), default='strict',
                        help='Explicit storage/playback policy; source PCM and declared core state remain exact. '
                             'Unresolved LCD/save evidence writes an ok=false report and exits nonzero')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/jit-app-long-sgz2.json')
    args = parser.parse_args()
    if args.native_counter:
        if args.cost_census:
            parser.error('--native-counter does not support --cost-census')
        from native_block_counter import application_class
        LongApplication = application_class(LongApplication)
    assert args.frames > 0
    if args.comparison_policy == 'storage-audio-v1':
        assert args.declared_state, 'storage-audio-v1 requires --declared-state'
    rom = args.rom.read_bytes()
    events = json.loads(args.input_script.read_text()) if args.input_script else []
    for event in events:
        assert set(event) == {'frame', 'key', 'down'}
        assert 1 <= event['frame'] < args.frames and 1 <= event['key'] <= 42
        assert isinstance(event['down'], bool)
    comparison = identity(rom, args.frames, events, args.cost_census,
                          args.declared_state, args.allow_audio_catchup, args.menu_exit, args.skip)
    capture_dir = args.output.parent / args.output.stem
    if args.baseline_cache and args.baseline_cache.exists():
        cache = json.loads(args.baseline_cache.read_text())
        assert cache['comparison'] == comparison, 'Cached scenario does not match'
        old = cache['baseline']
        assert old['bda_sha256'] == digest(args.baseline_bda.read_bytes())
        assert old['elf_sha256'] == digest(args.baseline_elf.read_bytes())
    else:
        old = execute(args.baseline_bda, args.baseline_elf, args.nm, rom, args.frames,
                      events, capture_dir / 'baseline', args.cost_census,
                      args.declared_state, args.allow_audio_catchup, args.menu_exit, args.skip)
    if not args.allow_silent:
        assert old['pcm_nonzero_samples'] > 0, 'Scenario never reached audible game content'
        assert len({r['lcd'] for r in old['frames'][min(20, args.frames - 1):]}) > 1
    report = {'ok': True, 'comparison': comparison, 'baseline': old,
              'scope': 'Complete actual release MIPS app from startup to normal Escape exit; firmware mocked',
              'frame_cost_boundaries': 'input_frame entry to following input_frame; final frame ends at audio close',
              'limitation': 'MIPS block instruction entries and host time do not establish physical H1 cycles/FPS. Scene coverage requires inspecting saved screenshots.'}
    if args.menu_exit:
        report['scope'] = ('Complete actual release MIPS app, including P to open the menu and confirmation '
                           'of Exit; firmware mocked. Total-cost comparison includes exit UI costs')
        report['frame_cost_boundaries'] = ('input_frame entry to following input_frame; final gameplay frame '
                                           'ends at first audio pause/close. Menu and later teardown are excluded '
                                           'from frame costs but included in block_instructions')
        report['output_comparison'] = ('Compare gameplay LCD/core/PCM and PCM ring observations through first '
                                       'audio pause/close, plus persisted save payloads. Actual final menu LCD and '
                                       'audio teardown are recorded separately')
    if not args.baseline_only:
        if args.current_cache:
            cache = json.loads(args.current_cache.read_text())
            assert cache['comparison'] == comparison, 'Cached current scenario does not match'
            new = cache.get('current', cache['baseline'])
            assert new['bda_sha256'] == digest(args.bda.read_bytes())
            assert new['elf_sha256'] == digest(args.elf.read_bytes())
        else:
            new = execute(args.bda, args.elf, args.nm, rom, args.frames, events,
                          capture_dir / 'current', args.cost_census,
                          args.declared_state, args.allow_audio_catchup, args.menu_exit, args.skip)
        if args.comparison_policy == 'strict':
            for first, second in zip(old['frames'], new['frames']):
                for field in ('lcd', 'pcm', 'pcm_frames', 'cpu_ppu_state'):
                    assert first[field] == second[field], (first['frame'], field)
                if args.declared_state:
                    assert first['declared_state'] == second['declared_state'], (first['frame'], 'declared_state')
            pcm_fields = (('gameplay_submitted_ring_pcm', 'gameplay_observed_ring_pcm') if args.menu_exit else
                          ('submitted_ring_pcm', 'observed_ring_pcm'))
            for field in ('screen', 'save_payloads', 'core_pcm', 'pcm_frames') + pcm_fields:
                assert old[field] == new[field], field
            if args.allow_audio_catchup:
                for field in ('rendered_frames', 'audio_catchup_frames'):
                    assert old[field] == new[field], field
        else:
            audit = compare_storage_audio(old, new)
            report.update(ok=audit['ok'], output_comparison=audit)
        ratio = new['block_instructions'] / old['block_instructions']
        report.update(current=new, ratio=ratio, reduction_percent=(1 - ratio) * 100,
                      segment_ratios=[dict(start_frame=a['start_frame'], end_frame=a['end_frame'],
                                           ratio=b['block_instructions'] / a['block_instructions'])
                                      for a, b in zip(old['segments'], new['segments'])])
        if args.max_ratio is not None:
            if args.comparison_policy == 'strict':
                assert ratio <= args.max_ratio, ratio
            elif ratio > args.max_ratio:
                report['ok'] = False
                audit['ok'] = False
                audit['failures'].append({'kind': 'cost_ratio_exceeded', 'ratio': ratio,
                                          'maximum': args.max_ratio})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ('baseline', 'current')}, indent=2))
    if not report['ok']:
        raise SystemExit('Comparison evidence failed; see the saved report (no equivalence/speedup claim)')


if __name__ == '__main__':
    main()
