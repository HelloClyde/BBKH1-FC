"""Run the real MIPS PCM functions with controlled firmware cursor movement."""
import argparse
import hashlib
import json
import re
import struct
from pathlib import Path
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_RA, UC_MIPS_REG_V0
from h1_mock import Machine, ROOT
from pause_menu_smoke import symbol


class SlowDisplay(Machine):
    """Charge 1500 consumed samples to each display operation, not each frame."""
    def game_buffer(self, *args):
        result = super().game_buffer(*args)
        if self.audio_active and self.audio_queue:
            self.audio_cursor = (self.audio_cursor+1500) % self.audio_samples
            self.word(0x80006108, self.audio_base+self.audio_cursor*2)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, default=ROOT / 'dist/H1FC.bda')
    parser.add_argument('--map', dest='map_path', type=Path, default=ROOT / 'build/release/H1FC.map')
    parser.add_argument('--stress-bda', type=Path, default=ROOT / 'build/test-20/H1FC.bda')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/verification/audio-smoke.json')
    args = parser.parse_args()
    bda, map_path = args.bda, args.map_path
    results = {}
    for rate in (22050, 32000, 48000):
        m = Machine(bda, path=None)

        def call(name, *args):
            boolean_signal = name == 'gba_audio_catchup'
            if boolean_signal: name = 'gba_audio_render_skip'
            m.finished = False
            for register, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
                m.uc.reg_write(register, value)
            m.uc.reg_write(UC_MIPS_REG_RA, 0x80001ff0)
            m.uc.emu_start(symbol(name, map_path), 0, timeout=10_000_000, count=10_000_000)
            assert m.finished, name
            value = m.uc.reg_read(UC_MIPS_REG_V0)
            return int(bool(value)) if boolean_signal else value

        assert call("gba_audio_open", rate) == 1
        pairs = [(-32768,32767), (-32768,-32768), (32767,32767), (-3,0), (2,-1), (1000,-500)]
        data = [pairs[i % len(pairs)] for i in range(7680)]
        flat = [v for pair in data for v in pair]
        pointer = 0x83000000
        m.write(pointer, struct.pack('<'+'h'*len(flat), *flat))
        offset = 0
        for length in (1, 63, 511, 1024, 6081):
            assert call("gba_audio_write", pointer+offset*4, length) == length
            offset += length
        # Independent weighted-box reference, carrying phase across callbacks.
        expected = []
        phase = accumulator = 0
        for left, right in data:
            sample = int((left+right)/2)
            remaining = 32000
            while remaining:
                weight = min(rate-phase, remaining)
                accumulator += sample*weight
                phase += weight
                remaining -= weight
                if phase == rate:
                    expected.append(int(accumulator/rate))
                    phase = accumulator = 0
        actual = struct.unpack('<'+'h'*len(expected), m.read(m.audio_base, len(expected)*2))
        assert list(actual) == expected, (rate, "PCM mismatch")
        assert call("gba_audio_catchup") == 0
        # Cursor consumes real ring samples; unread content must stay intact.
        advance = len(expected)-2048
        m.word(0x80006108, m.audio_base+advance*2)
        retained = m.read(m.audio_base+advance*2, 4096)
        assert call("gba_audio_catchup") == 1
        assert m.read(m.audio_base+advance*2, 4096) == retained
        assert m.read(m.audio_base, advance*2) == bytes(advance*2)
        # Hysteresis: 3072 stays in catch-up, 4096 returns to normal rendering.
        replenish = (1024*rate+31999)//32000
        assert call("gba_audio_write", pointer, replenish) == replenish
        assert call("gba_audio_catchup") == 1
        assert call("gba_audio_write", pointer, replenish) == replenish
        assert call("gba_audio_catchup") == 0
        # A genuine deficit keeps the same live descriptor and positions the
        # producer 512 silent samples ahead. No 128 ms restart or queue growth.
        position = (advance+5000) % m.audio_samples
        m.word(0x80006108, m.audio_base+position*2)
        submissions=len(m.audio_submissions)
        live_descriptor=m.audio_queue[0]
        assert call("gba_audio_catchup") == 1 and m.audio_queue==[live_descriptor]
        assert len(m.audio_submissions)==submissions
        silence=b''.join(m.read(m.audio_base+((position+i)%m.audio_samples)*2,2) for i in range(512))
        assert silence==bytes(1024)
        rebuild = (4096*rate+31999)//32000
        assert call("gba_audio_write", pointer, rebuild) == rebuild
        assert call("gba_audio_catchup") == 0 and len(m.audio_queue) == 1
        assert len(m.audio_submissions)==submissions
        recovered=b''.join(m.read(m.audio_base+((position+512+i)%m.audio_samples)*2,2) for i in range(128))
        actual_recovered=struct.unpack('<128h',recovered)
        # Continuing resampler state differs by rate; full amplitude is restored
        # after the finite ramp. At 32000 the exact ramp is independently known.
        if rate==32000:
            ramp=[int(expected[i]*(i+1)/128) for i in range(128)]
            assert list(actual_recovered)==ramp
        # Two genuine deficits must activate the stronger display budget.
        m.word(0x80006108, m.audio_base+((position+10000)%m.audio_samples)*2)
        assert call("gba_audio_render_skip") == 5
        assert call("gba_audio_write", pointer, rebuild+2) == rebuild+2
        assert call("gba_audio_catchup") == 0
        # Storage no longer raises a reserve target: cancel before blocking,
        # then refill fresh PCM under temporary display skips.
        call("gba_audio_storage_begin")
        assert not m.audio_active and not m.audio_queue
        assert call("gba_audio_pacing") == 0
        call("gba_audio_storage_end")
        assert m.audio_active and not m.audio_queue
        assert call("gba_audio_catchup") == 1 and call("gba_audio_wait") == 0
        assert call("gba_audio_write", pointer, rebuild+2) == rebuild+2
        assert call("gba_audio_catchup") == 0 and len(m.audio_queue) == 1
        call("gba_audio_pause")
        assert call("gba_audio_catchup") == 0 and not m.audio_active and not m.audio_queue
        assert call("gba_audio_resume") == 1
        assert call("gba_audio_catchup") == 0
        call("gba_audio_close")
        assert m.system_volume == 37 and not m.audio_active and not m.audio_initialized
        assert not m.audio_queue
        results[str(rate)] = {"exact_samples":len(expected), "submissions":len(m.audio_submissions),
                             "low_water_and_recovery":True, "unread_pcm_preserved":True,
                             "continuous_underrun_recovery":True,"recovery_silence_samples":512,
                             "storage_cancel_and_refill":True}
    # The actual frontend must continue all 20 CPU/APU frames while skipping
    # expensive displays, and must not persist its temporary skip as a setting.
    from options_smoke import config_slot
    from make_test_rom import make_rom
    path = 'A:\\FC\\H1TEST.nes'
    rom = make_rom()
    stress = SlowDisplay(args.stress_bda, path=path,
                         files={path: rom, path + '.cfg.s0': config_slot(rom, 0)})
    stress.run()
    log = stress.files['A:\\FC\\h1fc.log'].decode('gbk')
    assert 'AUDIO_CLOSE_BEGIN batches=20 ' in log
    catchup = re.search(r'AUDIO_CATCHUP frames=(\d+) max_skip=5 config_skip=0', log)
    assert catchup and int(catchup[1]) > 0
    assert 0 < stress.blits < 20
    saved_configs = [value for name,value in stress.files.items() if '.cfg.s' in name]
    assert all(struct.unpack_from('<I', value, 28+12)[0] == 0 for value in saved_configs)
    results['slow_display'] = {'core_audio_frames':20, 'display_frames':stress.blits,
                              'catchup_frames':int(catchup[1]), 'configured_skip_preserved':True,
                              'bda_sha256':hashlib.sha256(args.stress_bda.read_bytes()).hexdigest()}
    report = {"ok":True, "kind":"Actual MIPS PCM functions with mocked H1 services",
              "bda_sha256":hashlib.sha256(bda.read_bytes()).hexdigest(), "cases":results}
    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
