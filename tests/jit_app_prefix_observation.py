"""Supplement a counted baseline with a state-verified, uncounted display prefix.

No guest instructions, inputs or clock behavior are changed. The slower host
instruction-counting hook is omitted, and the final replay frame is excluded
because it delivers an earlier normal menu exit. Original costs stay untouched.
"""
import argparse
import copy
import json
from pathlib import Path

from jit_app_long_benchmark import LongApplication, ROOT, digest, identity


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-cache', type=Path, required=True)
    p.add_argument('--bda', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    p.add_argument('--nm', type=Path, default=ROOT.parent/'.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    p.add_argument('--rom', type=Path, required=True)
    p.add_argument('--input-script', type=Path, required=True)
    p.add_argument('--replay-frames', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--supplement-output', type=Path, required=True)
    a = p.parse_args()
    assert a.output.resolve() != a.baseline_cache.resolve()
    assert a.supplement_output.resolve() != a.baseline_cache.resolve()
    assert a.supplement_output.resolve() != a.output.resolve()
    original_bytes = a.baseline_cache.read_bytes()
    original = json.loads(original_bytes)
    baseline = original['baseline']
    rom = a.rom.read_bytes()
    events = json.loads(a.input_script.read_text())
    assert baseline['bda_sha256'] == digest(a.bda.read_bytes())
    assert baseline['elf_sha256'] == digest(a.elf.read_bytes())
    assert original['comparison'] == identity(rom, len(baseline['frames']), events,
                                               original['comparison']['cost_census'], True, True, True)
    assert 1 < a.replay_frames < len(baseline['frames'])
    schedule = [event for event in events if event['frame'] < a.replay_frames]
    machine = LongApplication(a.bda, a.elf, a.nm, rom, a.replay_frames, schedule,
                              a.output.parent/a.output.stem, False, True, True,
                              count_instructions=False)
    machine.run_long()
    assert machine.instructions == 0 and not machine.function_costs
    assert len(machine.records) == a.replay_frames
    assert machine.exit_menu_observed
    fields = ('frame', 'lcd', 'pcm', 'pcm_frames', 'cpu_ppu_state', 'declared_state')
    prefix = machine.records[:-1]
    for observed, expected in zip(prefix, baseline['frames']):
        assert 'block_instructions' not in observed, 'Uncounted record contains a cost statistic'
        for field in fields:
            assert observed[field] == expected[field], (observed['frame'], field)
    report = {'ok': True, 'kind': 'Read-only display prefix replay; instruction counting disabled',
              'source_cache_sha256': digest(original_bytes),
              'bda_sha256': baseline['bda_sha256'], 'elf_sha256': baseline['elf_sha256'],
              'rom_sha256': digest(rom), 'replay_frames': a.replay_frames,
              'verified_prefix_frames': len(prefix), 'cost_statistics_collected': False,
              'verified_equal_fields': list(fields), 'frames': prefix,
              'observation': 'presented/video_count read from native application videos counter; '
                             'no UC_HOOK_BLOCK instruction-counting observer registered',
              'execution': 'Same ROM, BDA, ELF, initial files, input-prefix and guest clock as the counted run. '
                           'Normal P/menu exit occurs only in the excluded final replay frame.',
              'limitation': 'This replay supplies presentation evidence only, not replacement performance costs.'}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2)+'\n')
    supplement = copy.deepcopy(original)
    for observed, target in zip(prefix, supplement['baseline']['frames']):
        for field in ('presented', 'video_count'):
            assert field not in target or target[field] == observed[field]
            target[field] = observed[field]
    supplement['observation_supplement'] = {
        'source_cache_sha256': digest(original_bytes), 'report': a.output.name,
        'report_sha256': digest(a.output.read_bytes()), 'start_frame': 1, 'end_frame': len(prefix),
        'added_fields': ['presented', 'video_count'], 'cost_statistics_collected': False,
        'original_costs_preserved': True,
        'proof': 'Every replayed prefix LCD, source PCM, PCM length, CPU/PPU and declared state '
                 'matches the original counted cache frame by frame; final replay exit frame excluded.'}
    # Removing only the added observations restores every byte-equivalent JSON
    # value of the original measured result, including all cost statistics.
    restored = copy.deepcopy(supplement)
    del restored['observation_supplement']
    for target, expected in zip(restored['baseline']['frames'], baseline['frames']):
        for field in ('presented', 'video_count'):
            if field not in expected:
                target.pop(field, None)
    assert restored == original
    assert a.baseline_cache.read_bytes() == original_bytes, 'Original cache changed during replay'
    a.supplement_output.parent.mkdir(parents=True, exist_ok=True)
    a.supplement_output.write_text(json.dumps(supplement, indent=2)+'\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'frames'}, indent=2))


if __name__ == '__main__':
    main()
