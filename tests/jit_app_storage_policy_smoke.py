"""Host regressions for the opt-in storage/audio comparison evidence policy."""
import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

from jit_app_long_benchmark import (ROOT, compare_storage_audio, digest, identity,
                                    latest_save, save_observation)


def fixture():
    frames = [{'frame': i, 'lcd': 'image-a', 'pcm': 'pcm', 'pcm_frames': 532,
               'cpu_ppu_state': 'cpu', 'declared_state': 'all-core',
               'block_instructions': 100, 'pcm_nonzero_samples': 1,
               'presented': True, 'video_count': i} for i in (1, 2)]
    return {'frames': frames, 'screen': 'screen-a', 'core_pcm': 'core-pcm', 'pcm_frames': 1064,
            'pcm_nonzero_samples': 2, 'save_payloads': {'A:\\FC\\BENCH.nes.s0': 'final-save'},
            'save_slots': {'A:\\FC\\BENCH.nes.s0': {'payload': 'final-save', 'generation': 1}},
            'rendered_frames': 2, 'audio_catchup_frames': 0,
            'submitted_ring_pcm': 'ring-a', 'observed_ring_pcm': 'heard-a',
            'gameplay_submitted_ring_pcm': 'ring-a', 'gameplay_observed_ring_pcm': 'heard-a',
            'block_instructions': 200, 'segments': [{'start_frame': 1, 'end_frame': 2,
                                                   'block_instructions': 200}],
            'log': 'SAVE_LOAD_END status=0 slot=-1 generation=0\nSAVE_EXIT_END status=1\n'}


def main():
    checks = []
    old = fixture(); new = copy.deepcopy(old)
    assert compare_storage_audio(old, new)['ok']; checks.append('identical observations pass')
    new['observed_ring_pcm'] = 'heard-b'
    audit = compare_storage_audio(old, new)
    assert audit['ok'] and 'observed_ring_pcm' in audit['differences']
    checks.append('ring difference is explicit and does not relax source PCM')
    for field in ('pcm', 'pcm_frames', 'cpu_ppu_state', 'declared_state'):
        new = copy.deepcopy(old); new['frames'][0][field] = 'corrupt'
        audit = compare_storage_audio(old, new)
        assert not audit['ok'] and not audit['core_observations_exact']
    checks.append('each required source/core field fails on mismatch')
    new = copy.deepcopy(old); old['frames'][1]['lcd'] = 'image-b'; old['screen'] = 'screen-b'
    new['frames'][1]['presented'] = False; new['frames'][1]['video_count'] = 1
    new['rendered_frames'] = 1; new['audio_catchup_frames'] = 1
    audit = compare_storage_audio(old, new)
    assert audit['ok'] and audit['lcd_differences'][0]['kind'] == 'observed_display_cadence_difference'
    checks.append('same-frame LCD difference requires a witnessed display hold')
    new['frames'][1]['presented'] = True
    assert not compare_storage_audio(old, new)['ok']
    checks.append('different images on shared presentation fail')
    del old['frames'][1]['presented']; del new['frames'][1]['presented']
    audit = compare_storage_audio(old, new)
    assert not audit['ok'] and audit['lcd_differences'][0]['kind'] == 'unresolved_visual_difference'
    checks.append('legacy hashes cannot be mistaken for skipped presentation')
    old = fixture(); new = copy.deepcopy(old)
    new['frames'][1].update(presented=False, lcd='unexpected-write')
    assert not compare_storage_audio(old, new)['ok']
    checks.append('LCD changed without presentation fails')
    old = fixture(); new = copy.deepcopy(old)
    new['save_slots']['A:\\FC\\BENCH.nes.s0']['payload'] = 'different-final-save'
    assert not compare_storage_audio(old, new)['ok']
    checks.append('latest final save must match')
    old = fixture()
    for path in ('A:\\FC\\BENCH.nes.cfg.s0', 'A:\\FC\\BENCH.nes.state.s1'):
        old['save_payloads'][path] = 'other-domain'
        old['save_slots'][path] = {'payload': 'other-domain', 'generation': 99}
    assert latest_save(old)['payload'] == 'final-save'
    new = copy.deepcopy(old)
    assert compare_storage_audio(old, new)['ok']
    new['save_slots']['A:\\FC\\BENCH.nes.s0']['payload'] = 'changed-battery'
    assert not compare_storage_audio(old, new)['ok']
    new = copy.deepcopy(old)
    new['save_payloads']['A:\\FC\\BENCH.nes.state.s1'] = 'changed-state'
    audit = compare_storage_audio(old, new)
    assert not audit['ok'] and any(f['kind'] == 'other_save_domain_mismatch' for f in audit['failures'])
    assert 'save_payloads' in audit['differences']
    checks.append('higher-generation config/state slots cannot mask battery changes; other domains stay strict')
    del old['save_slots']
    assert latest_save(old)['payload'] == 'final-save'
    checks.append('legacy alternating-slot lookup selects the exact ROM battery path')
    legacy = fixture(); del legacy['save_slots']
    legacy['log'] = ('SAVE_LOAD_END status=0 slot=-1 generation=0\n'
                     'SAVE_CHECKPOINT_DUMP count=1 omitted=0\n'
                     'SAVE_CHECKPOINT_END status=1\nSAVE_EXIT_END status=1\n')
    legacy['save_payloads']['A:\\FC\\BENCH.nes.s1'] = 'latest-save'
    assert latest_save(legacy)['payload'] == 'latest-save'
    new = fixture(); new['save_slots']['A:\\FC\\BENCH.nes.s0']['payload'] = 'latest-save'
    audit = compare_storage_audio(legacy, new)
    assert audit['ok'] and 'save_payloads' in audit['differences']
    checks.append('legacy latest slot is established by complete alternating-write history')
    legacy['log'] = legacy['log'].replace('omitted=0', 'omitted=1')
    assert not compare_storage_audio(legacy, new)['ok']
    checks.append('omitted save history cannot establish latest slot')
    payload = b'local-test-save'
    header = struct.pack('<6I', 0x31534748, 1, 123, 7, len(payload), zlib.crc32(payload))
    data = header + struct.pack('<I', zlib.crc32(header)) + payload
    observed = save_observation({'a.s0': data})
    assert observed['a.s0']['generation'] == 7 and observed['a.s0']['payload'] == digest(payload)
    try:
        save_observation({'a.s0': data[:-1] + b'!'})
    except AssertionError:
        pass
    else:
        raise AssertionError('Corrupt persisted save accepted')
    checks.append('new slot observations validate payload and header CRCs')
    # Exercise the CLI with caches only: failed evidence must remain inspectable
    # and return nonzero; strict remains the default and rejects ring changes.
    with tempfile.TemporaryDirectory(prefix='storage-policy-', dir=ROOT/'build') as folder:
        folder = Path(folder)
        paths = {name: folder/name for name in ('old.bda', 'old.elf', 'new.bda', 'new.elf', 'rom.nes')}
        for name, path in paths.items(): path.write_bytes(name.encode())
        old = fixture(); new = copy.deepcopy(old)
        for result, prefix in ((old, 'old'), (new, 'new')):
            result['bda_sha256'] = digest(paths[prefix+'.bda'].read_bytes())
            result['elf_sha256'] = digest(paths[prefix+'.elf'].read_bytes())
        comparison = identity(paths['rom.nes'].read_bytes(), 2, [], False, True)
        old_path, new_path, output = folder/'old.json', folder/'new.json', folder/'comparison.json'
        old_path.write_text(json.dumps({'comparison': comparison, 'baseline': old}))
        new['observed_ring_pcm'] = 'changed-ring'
        new_path.write_text(json.dumps({'comparison': comparison, 'baseline': new}))
        command = [sys.executable, str(ROOT/'tests/jit_app_long_benchmark.py'), '--baseline-bda', str(paths['old.bda']),
                   '--baseline-elf', str(paths['old.elf']), '--bda', str(paths['new.bda']), '--elf', str(paths['new.elf']),
                   '--rom', str(paths['rom.nes']), '--frames', '2', '--declared-state', '--allow-silent',
                   '--baseline-cache', str(old_path), '--current-cache', str(new_path), '--output', str(output)]
        strict = subprocess.run(command, capture_output=True, text=True)
        assert strict.returncode and not output.exists(), strict.stdout + strict.stderr
        explicit = subprocess.run(command + ['--comparison-policy', 'storage-audio-v1'], capture_output=True, text=True)
        assert explicit.returncode == 0 and json.loads(output.read_text())['ok'], explicit.stdout + explicit.stderr
        new['frames'][1]['lcd'] = 'changed-frame'; del new['frames'][1]['presented']
        new_path.write_text(json.dumps({'comparison': comparison, 'baseline': new}))
        unresolved = subprocess.run(command + ['--comparison-policy', 'storage-audio-v1'], capture_output=True, text=True)
        assert unresolved.returncode and not json.loads(output.read_text())['ok'], unresolved.stdout + unresolved.stderr
    checks.append('CLI keeps strict default; explicit unresolved report is saved and returns failure')
    print(json.dumps({'ok': True, 'checks': checks}, indent=2))


if __name__ == '__main__':
    main()
