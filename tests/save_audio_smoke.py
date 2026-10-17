"""Actual MIPS storage transactions and audio recovery with faulting services.

The injected storage stalls are deliberately longer than multiple PCM ring
laps. This tests ownership and stale-data prevention, not physical H1 timing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1,
                               UC_MIPS_REG_A2, UC_MIPS_REG_A3,
                               UC_MIPS_REG_RA, UC_MIPS_REG_V0)
from h1_mock import Machine, ROOT
from make_test_rom import make_rom
from pause_menu_smoke import symbol

ROM = 'A:\\FC\\SAVE-AUDIO.nes'
LOG = 'A:\\FC\\h1fc.log'


class StorageMachine(Machine):
    def __init__(self, bda, map_path):
        super().__init__(bda, path=ROM, files={ROM: make_rom()})
        self.map_path = map_path
        self.fault = None
        self.storage_writes = []
        self.storage_events = []
        self.fail_start = False

    def pcm_start(self, *args):
        return 0 if self.fail_start else super().pcm_start(*args)

    def call(self, name, *args):
        self.finished = False
        for register, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1,
                                    UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.uc.reg_write(register, value)
        self.uc.reg_write(UC_MIPS_REG_RA, 0x80001ff0)
        self.uc.emu_start(symbol(name, self.map_path), 0,
                          timeout=20_000_000, count=20_000_000)
        assert self.finished, name
        value = self.uc.reg_read(UC_MIPS_REG_V0)
        return value if value < 0x80000000 else value - 0x100000000

    def storage_stall(self, samples=65536):
        # Firmware playback cannot reread any cancelled ring, even across four
        # laps. Do not pretend that silence here is useful generated audio.
        assert not self.audio_active and not self.audio_queue
        self.ticks += samples // 32

    def fwrite(self, ptr, size, count, handle, *args):
        path, mode, stream = self.handles[handle]
        if path in (ROM + '.s0', ROM + '.s1'):
            self.storage_events.append('write')
            self.storage_writes.append(size * count)
            self.storage_stall()
            if self.fault == 'short_write':
                return stream.write(self.read(ptr, size * count // 2))
        return super().fwrite(ptr, size, count, handle, *args)

    def fclose(self, handle, *args):
        path, mode, _ = self.handles[handle]
        result = super().fclose(handle, *args)
        if path in (ROM + '.s0', ROM + '.s1'):
            self.storage_events.append('close_' + mode)
            self.storage_stall()
            if self.fault == 'close_failure' and 'w' in mode:
                return -1
        return result

    def fread(self, ptr, size, count, handle, *args):
        path, mode, _ = self.handles[handle]
        result = super().fread(ptr, size, count, handle, *args)
        if self.fault == 'corrupt_readback' and path == ROM + '.s1' and count == 8320:
            self.write(ptr + 8192, bytes([self.read(ptr + 8192, 1)[0] ^ 1]))
        return result


class CheckpointApplication(StorageMachine):
    def __init__(self, bda, map_path):
        super().__init__(bda, map_path)
        self.elapsed_checkpoint_interval = False
        self.live_log_writes = 0
        self.periodic_write_count = 0

    def clock(self, *args):
        # Exercise the actual app's periodic-checkpoint branch without running
        # ten seconds of emulation. Only mocked wall time changes; no guest
        # instructions, ROM state, SRAM or input memory are patched.
        if self.blits >= 12 and not self.elapsed_checkpoint_interval:
            self.ticks += 10001
            self.elapsed_checkpoint_interval = True
        return super().clock(*args)

    def fwrite(self, ptr, size, count, handle, *args):
        path, mode, _ = self.handles[handle]
        if path == LOG and self.audio_active and self.audio_queue:
            self.live_log_writes += 1
        if path in (ROM + '.s0', ROM + '.s1') and self.audio_initialized:
            self.periodic_write_count += 1
            assert b'SAVE_CHECKPOINT_END' not in self.files[LOG]
        return super().fwrite(ptr, size, count, handle, *args)

    def event(self, code, key, *args):
        if self.blits >= 22:
            self.menu = True
        return super().event(code, key, *args)


def resample(data, rate, phase=0, accumulator=0):
    output = []
    for left, right in data:
        sample = int((left + right) / 2)
        remaining = 32000
        while remaining:
            weight = min(rate - phase, remaining)
            accumulator += sample * weight
            phase += weight
            remaining -= weight
            if phase == rate:
                output.append(int(accumulator / rate))
                phase = accumulator = 0
    return output, phase, accumulator


def audio_cases(bda, map_path):
    cases = []
    for rate in (22050, 32000, 48000):
        machine = StorageMachine(bda, map_path)
        machine.call('h1_diag_init')
        assert machine.call('gba_audio_open', rate) == 1
        pairs = [(-32768, 32767), (1000, -500), (-32768, -32768), (32767, 32767)]
        data = [pairs[i % 4] for i in range(16000)]
        ptr = 0x83000000
        machine.write(ptr, struct.pack('<' + 'h' * 32000, *[v for p in data for v in p]))
        first, phase, acc = resample(data[:7681], rate)
        second, _, _ = resample(data[7681:13825], rate, phase, acc)
        assert machine.call('gba_audio_write', ptr, 7681) == 7681
        assert machine.audio_queue
        original_base = machine.audio_base
        assert machine.read(original_base, len(first) * 2) == struct.pack('<' + 'h' * len(first), *first)
        log_before = machine.files[LOG]
        machine.call('gba_audio_storage_begin')
        machine.call('gba_audio_storage_begin')
        assert not machine.audio_active and not machine.audio_queue
        assert machine.read(original_base, 32768) == bytes(32768)
        machine.storage_stall()
        assert machine.call('gba_audio_pacing') == 0
        machine.call('gba_audio_storage_end')
        assert machine.audio_active and not machine.audio_queue
        assert machine.files[LOG] == log_before, 'Storage recovery wrote a synchronous diagnostic'
        assert machine.call('gba_audio_render_skip') == 2, 'Recovery must refill before spending full display cost'
        assert machine.call('gba_audio_write', ptr + 7681 * 4, 6144) == 6144
        assert len(machine.audio_queue) == 1
        assert machine.call('gba_audio_render_skip') == 0, 'Temporary refill skips must end at high water'
        for index in range(128):
            second[index] = int(second[index] * (index + 1) / 128)
        assert machine.read(original_base, len(second) * 2) == struct.pack('<' + 'h' * len(second), *second)
        assert machine.read(original_base + len(second) * 2, 32768 - len(second) * 2) == bytes(32768 - len(second) * 2)
        machine.call('gba_audio_pause')
        machine.call('gba_audio_storage_begin')
        machine.call('gba_audio_storage_end')
        assert not machine.audio_active and not machine.audio_queue, 'Storage resumed a paused menu'
        assert machine.call('gba_audio_resume') == 1
        machine.call('gba_audio_close')
        assert b'AUDIO_STORAGE pauses=1 ' in machine.files[LOG]
        assert not machine.audio_initialized and not machine.audio_queue
        cases.append({'source_rate': rate, 'preserved_resampler_phase': True,
                      'fresh_pcm_samples': len(second), 'stalled_ring_laps': 4,
                      'old_pcm_replayed_during_storage': False, 'fade_samples': 128,
                      'no_recovery_log_io': True, 'menu_pause_preserved': True})
    machine = StorageMachine(bda, map_path)
    machine.call('h1_diag_init')
    assert machine.call('gba_audio_open', 32000) == 1
    machine.call('gba_audio_storage_begin')
    machine.fail_start = True
    machine.call('gba_audio_storage_end')
    assert machine.call('gba_audio_pacing') == 0
    assert not machine.audio_active and not machine.audio_initialized and not machine.audio_queue
    assert machine.system_volume == 37
    cases.append({'restart_failure': True, 'device_and_descriptors_closed': True,
                  'original_volume_restored': True})
    return cases


def save_cases(bda, map_path):
    machine = StorageMachine(bda, map_path)
    store, path, data, restored, reload_store = 0x83100000, 0x83101000, 0x83102000, 0x83106000, 0x83109000
    machine.write(path, ROM.encode() + b'\0')
    payload = bytearray((index * 19 + 3) & 255 for index in range(8320))
    machine.write(data, bytes(payload))
    assert machine.call('save_load', store, path, data, len(payload)) == 0
    assert machine.call('save_dirty', store, data, len(payload)) == 0
    events_before = list(machine.storage_events)
    assert machine.call('save_checkpoint', store, data, len(payload)) == 0
    assert machine.storage_events == events_before
    # Only the 128-byte mapper IRAM changes. It must remain part of the saved
    # payload and of dirty detection, even if a title uses it for audio state.
    payload[8192 + 73] ^= 1
    machine.write(data, bytes(payload))
    assert machine.call('save_dirty', store, data, len(payload)) == 1
    assert machine.call('save_checkpoint', store, data, len(payload)) == 1
    assert machine.storage_writes == [8348], 'Header and payload were not written together'
    good_slot = machine.files[ROM + '.s0']
    header = struct.unpack('<7I', good_slot[:28])
    assert header[0:2] == (0x31534748, 1) and header[4] == 8320
    assert header[5] == zlib.crc32(payload) and header[6] == zlib.crc32(good_slot[:24])
    assert good_slot[28:] == payload
    initial_store = machine.read(store, 284)
    cases = []
    for fault in ('short_write', 'close_failure', 'corrupt_readback'):
        machine.fault = fault
        changed = bytes([payload[0] ^ 1]) + payload[1:]
        machine.write(data, changed)
        assert machine.call('save_checkpoint', store, data, len(payload)) == -1, fault
        assert machine.read(store, 284) == initial_store, fault
        assert machine.files[ROM + '.s0'] == good_slot, fault
        # The injected close failure may have written valid bytes anyway. It
        # must not advance the in-memory generation until verified, while a
        # future launch is allowed to accept any complete CRC-valid slot.
        machine.fault = None
        machine.files[ROM + '.s1'] = b'bad'
        assert machine.call('save_load', reload_store, path, restored, len(payload)) == 1
        assert machine.read(restored, len(payload)) == payload
        cases.append({'fault': fault, 'verified_slot_preserved': True,
                      'generation_unchanged': True, 'previous_slot_reload': True})
    assert not machine.allocations and not machine.handles
    return {'unchanged_no_io': True, 'payload_bytes': 8320, 'mapper_iram_bytes': 128,
            'combined_write_bytes': 8348, 'format_and_crc_unchanged': True, 'faults': cases}


def application_case(bda, map_path):
    machine = CheckpointApplication(bda, map_path)
    machine.run()
    log = machine.files[LOG].decode('gbk')
    assert machine.elapsed_checkpoint_interval and machine.periodic_write_count == 1
    assert machine.live_log_writes == 0
    assert 'SAVE_CHECKPOINT_END status=1 ' in log and 'deferred=1' in log
    assert 'AUDIO_STORAGE pauses=1 ' in log and 'APP_END result=0' in log
    # Deferred output is allowed only after either menu pause or final close.
    checkpoint_index = log.index('SAVE_CHECKPOINT_END status=1 ')
    assert ('AUDIO_PAUSE' in log[:checkpoint_index] or 'AUDIO_CLOSE_BEGIN' in log[:checkpoint_index])
    return {'actual_app_periodic_save': True, 'periodic_writes': machine.periodic_write_count,
            'log_writes_with_live_ring': machine.live_log_writes, 'deferred_after_audio_stops': True,
            'successful_exit': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bda', type=Path, default=ROOT / 'dist/H1FC-jit.bda')
    parser.add_argument('--map', dest='map_path', type=Path, default=ROOT / 'build/release-jit/H1FC.map')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/save-audio-smoke.json')
    args = parser.parse_args()
    result = {'ok': True, 'kind': 'Actual MIPS functions, mocked blocking/faulting firmware services',
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'audio': audio_cases(args.bda, args.map_path),
              'save': save_cases(args.bda, args.map_path),
              'application': application_case(args.bda, args.map_path),
              'limitation': 'Storage is deliberately silent. This does not remove IO latency or establish physical-device performance.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
