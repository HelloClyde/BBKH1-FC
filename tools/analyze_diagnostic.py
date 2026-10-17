"""Validate H1 automatic diagnostic logs without changing or executing them.

Fine timing is withheld on faults or incomplete traces. RTC throughput remains
independent and explicitly carries the one-second endpoint quantization bound.
Neither instruction counts nor emulator-host timings establish physical H1 FPS.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics

SCOPES = ('core', 'render', 'mix', 'video', 'audio', 'input', 'rom_io', 'jit',
          'save', 'wait', 'other', 'cpu', 'native', 'event', 'complete',
          'apu_event', 'apu_synth', 'mapper', 'ppu', 'ppu_bg', 'ppu_sprite',
          'ppu_pixel')
CPU_NAMES = ('interpreter', 'jit', 'jit_hle')
SAMPLING_NAMES = ('light', 'detail', 'rtc_only')
DETAIL_NAMES = ('light', 'control', 'coarse', 'cpu', 'ppu', 'sound')
EVENT = re.compile(r'(?:^|\s)((?:DIAG|PROFILE|APP)_[A-Z_]+)(?:\s|$)')
PAIR = re.compile(r'(?:^|\s)([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)')


def number(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value):
        return int(value)
    return None


def enum_value(value, names):
    numeric = number(value)
    if numeric is not None:
        return numeric if 0 <= numeric < len(names) else None
    return names.index(value) if value in names else None


def parse_records(text):
    records, issues = [], []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = EVENT.search(line)
        if not match:
            continue
        pairs = PAIR.findall(line[match.end():])
        fields = dict(pairs)
        if len(fields) != len(pairs):
            issues.append(f'line {line_number}: duplicate field names')
        records.append({'event': match.group(1), 'line': line_number, 'fields': fields})
    return records, issues


def _single(records, event, issues, required=True):
    found = [r for r in records if r['event'] == event]
    if len(found) != 1 and (required or found):
        issues.append(f'{event}: expected one record, found {len(found)}')
    return found[0]['fields'] if found else {}


def _numeric(fields, keys, issues, label):
    result = {}
    for key in keys:
        result[key] = number(fields.get(key))
        if result[key] is None or result[key] < 0:
            issues.append(f'{label}: invalid or missing {key}')
    return result


def _stat(values):
    return {'count': len(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values)} if values else None


def _rtc_metrics(frames, shown, seconds, valid):
    result = {'valid': bool(valid), 'seconds': seconds if valid else None,
              'endpoint_quantization_seconds': 1,
              'elapsed_seconds_interval': None, 'emulated_fps': None,
              'displayed_fps': None, 'emulated_fps_interval': None,
              'displayed_fps_interval': None}
    if not valid:
        return result
    low, high = max(0, seconds - 1), seconds + 1
    result['elapsed_seconds_interval'] = [low, high]
    result['emulated_fps_interval'] = [frames / high, frames / low if low else None]
    result['displayed_fps_interval'] = [shown / high, shown / low if low else None]
    if seconds:
        result['emulated_fps'] = frames / seconds
        result['displayed_fps'] = shown / seconds
    return result


def _clock_contract(records, frames, hz, sampling, reason):
    """Rev1 evidence contract; ready=1 alone is not independent validation."""
    if sampling == 2:
        return {'valid': False, 'calibration_valid': False, 'issues': ['rtc_only_group'], 'crosscheck': None}
    problems = []
    clock = _single(records, 'PROFILE_CLOCK', problems)
    cost = _single(records, 'PROFILE_CLOCK_COST', problems)
    probes = [r for r in records if r['event'] == 'PROFILE_CLOCK_PROBE']
    proof = {}
    for phase in ('align', 'measure'):
        selected = [r for r in probes if r['fields'].get('phase') == phase]
        if len(selected) != 1:
            problems.append('missing or duplicate calibration ' + phase + ' probe')
            continue
        p = selected[0]['fields']
        n = _numeric(p, ('rtc_start', 'rtc_end', 'counter_start', 'counter_end', 'counter_delta'), problems, 'calibration ' + phase)
        if all(v is not None and v >= 0 for v in n.values()):
            seconds = n['rtc_end'] - n['rtc_start']
            if not n['rtc_start'] or seconds not in (1, 2) or p.get('outcome') != 'RTC_edge':
                problems.append('calibration ' + phase + ' has no valid RTC edge')
            if n['counter_delta'] != ((n['counter_end'] - n['counter_start']) & 0xffffffff):
                problems.append('calibration ' + phase + ' counter delta disagrees')
            proof[phase] = (seconds, n['counter_delta'])
    observed = number(clock.get('observed_hz'))
    seconds = number(clock.get('calibration_RTC_seconds'))
    if (clock.get('ready') != '1' or clock.get('source') != 'TCU5_RTC' or
            number(clock.get('hz')) != 32768 or hz != 32768 or
            number(clock.get('resolution_us')) != 31 or
            observed is None or not 32000 <= observed <= 33500 or seconds not in (1, 2) or
            clock.get('reused') == '1'):
        problems.append('clock calibration fields do not prove a fresh 32768 Hz RTC calibration')
    if 'measure' not in proof or proof['measure'][0] != seconds or not seconds or proof['measure'][1] // seconds != observed:
        problems.append('calibration measurement disagrees with reported observed_hz/RTC seconds')
    if (cost.get('valid') != '1' or number(cost.get('reads')) != 4096 or
            cost.get('subtract_from_frames') != '0'):
        problems.append('clock-read cost probe missing, incomplete, or invalid')
    cost_n = _numeric(cost, ('ticks', 'raw_ticks', 'rtc_seconds'), problems, 'clock cost')
    if all(v is not None and v >= 0 for v in cost_n.values()) and not (
            max(0, cost_n['rtc_seconds'] - 1) * 32768 <= cost_n['ticks'] <= (cost_n['rtc_seconds'] + 1) * 32768):
        problems.append('clock-read cost endpoints cannot corroborate each other')
    reads = [r for r in records if r['event'] == 'PROFILE_TCU_READS']
    calibrated = [r for r in reads if r['fields'].get('stage') == 'calibrated']
    if len(calibrated) != 1 or calibrated[0]['fields'].get('fault') != '0':
        problems.append('missing or invalid post-calibration TCU state')
    calibration_valid = not problems
    final = [r for r in reads if r['fields'].get('stage') == reason]
    boundary = _single(records, 'PROFILE_RUN_BOUNDARY', problems)
    cross = {'valid': False, 'rtc_start': number(boundary.get('start_rtc')),
             'rtc_end': frames[-1].get('rtc_end') if frames else None,
             'tcu_total_ticks': None, 'rtc_seconds': None, 'tcu_seconds': None,
             'endpoint_quantization_seconds': 1,
             'interpretation': '端点/时钟不能相互证明时拒绝细计时，不自动断言 TCU 故障或 CPU 卡顿。'}
    if len(final) != 1:
        problems.append('missing or duplicate terminal TCU total')
    else:
        f = final[0]['fields']
        total = number(f.get('total'))
        cross['tcu_total_ticks'] = total
        first = frames[0].get('rtc_begin') if frames else None
        start, end = cross['rtc_start'], cross['rtc_end']
        if (f.get('fault') != '0' or number(f.get('period')) != 65536 or
                total is None or not 0 <= total <= 0xffffffff or
                not start or end is None or first is None or not start <= first <= end or
                boundary.get('reason') != reason or number(boundary.get('fault_frame')) != 0):
            problems.append('invalid TCU run boundary or terminal state')
        else:
            elapsed = end - start
            cross['rtc_seconds'], cross['tcu_seconds'] = elapsed, total / 32768
            cross['valid'] = max(0, elapsed - 1) * 32768 <= total <= (elapsed + 1) * 32768
            measured = sum(f.get('loop') or 0 for f in frames)
            cross['frame_processing_ticks'] = measured
            cross['ticks_outside_frame_processing_segments'] = total - measured
            if measured > total:
                cross['valid'] = False
                problems.append('frame processing ticks exceed terminal TCU total')
            if not cross['valid']:
                problems.append('TCU total and independent RTC endpoints cannot corroborate each other within ±1 second')
    return {'valid': not problems and cross['valid'], 'calibration_valid': calibration_valid,
            'issues': problems, 'crosscheck': cross}


def _phase(name, frames, summaries, group_fine_ok, hz, issues):
    phase_issues = []
    summary_records = [r for r in summaries if r['fields'].get('phase') == name]
    summary = summary_records[0]['fields'] if summary_records else {}
    if len(summary_records) != (1 if frames else 0):
        phase_issues.append(f'{name}: expected {int(bool(frames))} DIAG_PHASE, found {len(summary_records)}')
    if not frames:
        issues.extend(phase_issues)
        return None
    required = ('frames', 'displayed', 'rtc_begin', 'rtc_end', 'seconds',
                'raw_ticks', 'rtc_valid', 'fine_frames', 'loop_ticks', 'core_ticks')
    totals = _numeric(summary, required, phase_issues, name)
    shown = sum(f.get('displayed') or 0 for f in frames)
    loops = sum(f.get('loop') or 0 for f in frames)
    cores = sum(f.get('core') or 0 for f in frames)
    fine_count = sum(f.get('fine') == 1 for f in frames)
    expected = {'frames': len(frames), 'displayed': shown, 'loop_ticks': loops,
                'core_ticks': cores, 'fine_frames': fine_count,
                'rtc_begin': frames[0].get('rtc_begin'),
                'rtc_end': frames[-1].get('rtc_end')}
    if expected['rtc_begin'] is not None and expected['rtc_end'] is not None:
        expected['seconds'] = expected['rtc_end'] - expected['rtc_begin']
    if frames[0].get('raw_begin') is not None and frames[-1].get('raw_end') is not None:
        expected['raw_ticks'] = (frames[-1]['raw_end'] - frames[0]['raw_begin']) & 0xffffffff
    for key, value in expected.items():
        if totals.get(key) != value:
            phase_issues.append(f'{name}: {key} summary does not match frames')
    rtc_ok = totals.get('rtc_valid') == 1
    previous = 0
    for f in frames:
        begin, end = f.get('rtc_begin'), f.get('rtc_end')
        if begin is None or end is None or not begin or begin < previous or end < begin:
            rtc_ok = False
        previous = end or 0
    # A malformed summary cannot make a damaged phase appear valid.
    if any('rtc_' in problem or 'seconds' in problem or 'DIAG_PHASE' in problem
           for problem in phase_issues):
        rtc_ok = False
    seconds = expected.get('seconds', 0)
    fine_ok = bool(group_fine_ok and not phase_issues and rtc_ok and hz and
                   fine_count == len(frames) and all(not f.get('fault') for f in frames))
    fine = {'valid': fine_ok, 'hz': hz, 'loop_mean_ms': None, 'core_mean_ms': None,
            'core_processing_fps': None, 'loop_p95_ms': None, 'loop_max_ms': None,
            'core_p95_ms': None, 'core_max_ms': None, 'submodes': []}
    if fine_ok:
        fine['loop_mean_ms'] = loops * 1000 / hz / len(frames)
        fine['core_mean_ms'] = cores * 1000 / hz / len(frames)
        fine['core_processing_fps'] = len(frames) * hz / cores if cores else None
        for key in ('loop', 'core'):
            values = sorted(f[key] for f in frames)
            fine[key + '_p95_ms'] = values[(95 * len(values) + 99) // 100 - 1] * 1000 / hz
            fine[key + '_max_ms'] = values[-1] * 1000 / hz
        by_mode = defaultdict(list)
        for f in frames:
            by_mode[f['detail_mode']].append(f)
        for mode, selected in sorted(by_mode.items()):
            denominator = sum(f['loop'] for f in selected)
            ticks = [sum(f['scopes'][i] for f in selected) for i in range(len(SCOPES))]
            fine['submodes'].append({
                'mode': mode, 'name': DETAIL_NAMES[mode], 'frames': len(selected),
                'displayed': sum(f['displayed'] for f in selected),
                'loop_ticks': denominator,
                'unattributed_loop_ticks': denominator - sum(ticks),
                'scope_costs': {scope: {'ticks': ticks[i], 'milliseconds': ticks[i] * 1000 / hz,
                                      'percent_of_this_submode_loop': ticks[i] * 100 / denominator
                                      if denominator else None}
                                for i, scope in enumerate(SCOPES)},
            })
    audio = {key: sum(f.get('audio', {}).get(key) or 0 for f in frames)
             for key in ('gaps', 'missing', 'drops', 'failures', 'storage', 'storage_raw_ticks',
                         'retired', 'produced', 'batches', 'submits', 'retires')}
    audio['min_queued_observed'] = min((f['audio'][key] for f in frames if f.get('audio')
                                      for key in ('queued_begin', 'queued_end')
                                      if f['audio'].get(key) is not None), default=None)
    completed_frames = sum(f.get('core_completed', True) for f in frames)
    result = {'name': name, 'first_frame': frames[0]['frame'], 'last_frame': frames[-1]['frame'],
              'frames': completed_frames, 'recorded_frames': len(frames), 'displayed': shown, 'issues': phase_issues,
              'rtc': _rtc_metrics(completed_frames, shown, seconds, rtc_ok), 'fine': fine,
              'audio': audio, 'jit_compiles': sum(f.get('compile') or 0 for f in frames),
              'code_begin': frames[0].get('code_begin'), 'code_end': frames[-1].get('code_end'),
              'fine_frames_observed': fine_count}
    issues.extend(phase_issues)
    return result


def _group(records, orphan=False, experiment=None):
    issues = []
    if orphan:
        issues.append('group records occurred without DIAG_GROUP_BEGIN')
    begin = _single(records, 'DIAG_GROUP_BEGIN', issues)
    end = _single(records, 'DIAG_GROUP_END', issues)
    trace = _single(records, 'DIAG_TRACE_BEGIN', issues)
    trace_end = _single(records, 'DIAG_TRACE_END', issues)
    schema = _single(records, 'DIAG_TRACE_SCHEMA', issues)
    head = _numeric(begin, ('group', 'repeat', 'frames', 'warmup'), issues, 'group begin')
    cpu = enum_value(begin.get('mode'), CPU_NAMES)
    sampling = enum_value(begin.get('sampling'), SAMPLING_NAMES)
    if cpu is None:
        issues.append('group begin: invalid CPU mode')
    if sampling is None:
        issues.append('group begin: invalid sampling')
    if not begin.get('snapshot_crc'):
        issues.append('group begin: missing snapshot_crc')
    if enum_value(trace.get('sampling'), SAMPLING_NAMES) != sampling:
        issues.append('group and trace sampling disagree')
    trace_numbers = _numeric(trace, ('revision', 'recorded', 'expected', 'warmup', 'capacity',
                                     'overflow', 'unbalanced', 'hz', 'allocated'), issues, 'trace begin')
    if trace_numbers.get('revision') != 1:
        issues.append('unsupported diagnostic trace revision')
    if trace_numbers.get('overflow') or trace_numbers.get('unbalanced'):
        issues.append('trace overflow or unbalanced frame begin/end')
    if trace_numbers.get('allocated') != 1:
        issues.append('trace buffer allocation was not successful')
    if trace_numbers.get('expected') != head.get('frames') or trace_numbers.get('warmup') != head.get('warmup'):
        issues.append('group/trace expected frame or warmup count disagreement')
    if schema.get('scopes', '').split(',') != list(SCOPES) or schema.get('scopes_exclusive') != '1':
        issues.append('missing or unsupported exclusive scope schema')
    if head.get('warmup') is not None and head.get('frames') is not None and head['warmup'] >= head['frames']:
        issues.append('warmup must be shorter than group')
    by_frame = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record['event'] in ('DIAG_FRAME', 'DIAG_SCOPES', 'DIAG_AUDIO', 'DIAG_GAP'):
            frame_id = number(record['fields'].get('frame'))
            if frame_id is None or frame_id < 1:
                issues.append(f"line {record['line']}: invalid frame id")
                continue
            by_frame[frame_id][record['event']].append(record)
    frame_order = [number(r['fields'].get('frame')) for r in records if r['event'] == 'DIAG_FRAME']
    if frame_order != sorted(by_frame):
        issues.append('frame records are duplicated, missing, or out of order')
    frame_required = ('frame', 'warm', 'mode', 'skip', 'displayed', 'fine', 'fault', 'rtc_begin',
                      'rtc_end', 'raw_begin', 'raw_end', 'loop', 'core', 'reads', 'compile',
                      'code_begin', 'code_end')
    audio_required = ('queued_begin', 'queued_end', 'flags_begin', 'flags_end', 'read_begin',
                      'read_end', 'write_begin', 'write_end', 'retired', 'produced', 'retires',
                      'batches', 'submits', 'drops', 'failures', 'gaps', 'missing', 'storage',
                      'storage_raw_ticks')
    frames = []
    for frame_id, data in sorted(by_frame.items()):
        combined = [r for values in data.values() for r in values]
        fields = _single(combined, 'DIAG_FRAME', issues)
        scopes = _single(combined, 'DIAG_SCOPES', issues)
        audio = _single(combined, 'DIAG_AUDIO', issues)
        gap = _single(combined, 'DIAG_GAP', issues, required=False)
        item = _numeric(fields, frame_required, issues, f'frame {frame_id}')
        item['frame'] = frame_id
        item['detail_mode'] = item.pop('mode')
        item['line'] = data.get('DIAG_FRAME', combined)[0]['line']
        item['audio'] = _numeric(audio, audio_required, issues, f'audio frame {frame_id}')
        ticks = scopes.get('ticks', '').split(',')
        item['scopes'] = [number(tick) for tick in ticks]
        if len(ticks) != len(SCOPES) or any(tick is None or tick < 0 for tick in item['scopes']):
            issues.append(f'frame {frame_id}: invalid scope vector')
            item['scopes'] = [0] * len(SCOPES)
        if item['loop'] is not None and sum(item['scopes']) > item['loop'] and item['fine'] == 1:
            issues.append(f'frame {frame_id}: exclusive scope ticks exceed loop ticks')
        if item['core'] is not None and item['loop'] is not None and item['core'] > item['loop']:
            issues.append(f'frame {frame_id}: core ticks exceed loop ticks')
        if item['detail_mode'] not in range(6):
            issues.append(f'frame {frame_id}: invalid detail submode')
        if item['displayed'] not in (0, 1) or item['fine'] not in (0, 1):
            issues.append(f'frame {frame_id}: invalid displayed/fine flag')
        if sampling in (0, 2) and item['detail_mode'] != 0:
            issues.append(f'frame {frame_id}: detail submode in non-detail group')
        warmup = head.get('warmup')
        if warmup is not None and item['warm'] != int(frame_id <= warmup):
            issues.append(f'frame {frame_id}: warmup boundary disagrees')
        if (item['audio']['gaps'] or 0) > 0:
            if not gap or number(gap.get('count')) != item['audio']['gaps'] or number(gap.get('missing')) != item['audio']['missing']:
                issues.append(f'frame {frame_id}: gap summary missing or inconsistent')
        elif gap:
            issues.append(f'frame {frame_id}: unexpected gap record')
        item['gap'] = {k: number(v) if number(v) is not None else v for k, v in gap.items()} if gap else None
        frames.append(item)
    if sorted(by_frame) != list(range(1, len(frames) + 1)):
        issues.append('recorded frames are not a contiguous prefix starting at frame 1')
    observed = len(frames)
    if frames and number(end.get('partial')) == 1:
        frames[-1]['core_completed'] = False
    for item in frames:
        item.setdefault('core_completed', True)
    if trace_numbers.get('recorded') != observed or number(trace_end.get('recorded')) != observed:
        issues.append('trace recorded count disagrees with observed frames')
    if number(end.get('completed')) != observed:
        if number(end.get('partial')) == 1 and number(end.get('completed')) == observed - 1:
            issues.append('last trace frame is partial: abort occurred before core execution')
        else:
            issues.append('group completed count disagrees with observed frames')
    elif number(end.get('partial')) == 1:
        issues.append('partial flag contradicts completed core frame count')
    if number(end.get('partial')) == 1 and number(end.get('aborted')) != 1:
        issues.append('partial frame requires an aborted group')
    if number(end.get('aborted')) not in (0, 1):
        issues.append('missing or invalid group aborted flag')
    if number(trace_end.get('complete')) != 1 or observed != head.get('frames'):
        issues.append('group trace is incomplete')
    if number(end.get('aborted')) == 1:
        issues.append('group was aborted')
    faults = [r for r in records if r['event'].startswith('PROFILE_') and
              (number(r['fields'].get('fault')) not in (None, 0) or
               (r['event'] == 'PROFILE_CLOCK' and r['fields'].get('ready') == '0'))]
    frame_faults = [{'frame': f['frame'], 'fault': f['fault'], 'line': f['line']}
                    for f in frames if f['fault']]
    calibration = [r for r in records if r['event'] == 'PROFILE_CLOCK' and r['fields'].get('ready') == '1']
    hz = trace_numbers.get('hz')
    clock_contract = _clock_contract(records, frames, hz, sampling, trace.get('reason'))
    calibrated = clock_contract['calibration_valid']
    fine_reasons = []
    if issues:
        fine_reasons.append('incomplete_or_inconsistent_trace')
    if (faults and sampling != 2) or frame_faults:
        fine_reasons.append('clock_or_scope_fault')
    if sampling == 2:
        fine_reasons.append('rtc_only_group')
    elif not calibrated:
        fine_reasons.append('no_valid_clock_calibration')
    elif not clock_contract['valid']:
        fine_reasons.append('tcu_rtc_run_validation_failed')
    if any(f['fine'] != 1 for f in frames) and sampling != 2:
        fine_reasons.append('one_or_more_frames_lack_fine_timing')
    fine_ok = not fine_reasons
    warmup = head.get('warmup') or 0
    phases = {}
    for name, selected in (('warmup', [f for f in frames if f['frame'] <= warmup]),
                           ('measure', [f for f in frames if f['frame'] > warmup])):
        phases[name] = _phase(name, selected, [r for r in records if r['event'] == 'DIAG_PHASE'],
                              fine_ok, hz, issues)
    rtc_invalid = any(phase and not phase['rtc']['valid'] for phase in phases.values())
    if rtc_invalid:
        fine_reasons.append('invalid_rtc_in_group')
    # A later-discovered inconsistent phase must invalidate all fine scopes.
    if issues or rtc_invalid:
        for phase in phases.values():
            if phase:
                phase['fine'] = {'valid': False, 'hz': hz, 'submodes': []}
        if issues and 'incomplete_or_inconsistent_trace' not in fine_reasons:
            fine_reasons.append('incomplete_or_inconsistent_trace')
    correlations = []
    for position, item in enumerate(frames):
        if not (item['audio']['gaps'] or 0):
            continue
        preceding = frames[max(0, position - 2):position + 1]
        correlations.append({'frame': item['frame'], 'phase': 'warmup' if item['warm'] else 'measure',
                             'gaps': item['audio']['gaps'], 'missing': item['audio']['missing'],
                             'queued_begin': item['audio']['queued_begin'], 'queued_end': item['audio']['queued_end'],
                             'same_frame_compiles': item['compile'],
                             'same_frame_save_ticks_raw': item['scopes'][SCOPES.index('save')],
                             'same_frame_storage_pauses': item['audio']['storage'],
                             'nearby_frames': [{'frame': f['frame'], 'compiles': f['compile'],
                                                'save_ticks_raw': f['scopes'][SCOPES.index('save')],
                                                'storage_pauses': f['audio']['storage'],
                                                'loop_ticks_raw': f['loop'], 'core_ticks_raw': f['core'],
                                                'fine': f['fine'], 'fault': f['fault']} for f in preceding]})
    control_violations = []
    effective_controls = dict(experiment or {})
    effective_controls.update(begin)
    for key, value in (('scale', '1'), ('skip', '1'), ('sound', '1'), ('volume', '100'),
                       ('adaptive_skip', '0'), ('storage', 'disabled')):
        if effective_controls.get(key) != value:
            control_violations.append('unproved or changed fixed control: ' + key)
    if not number(begin.get('frame_us')) or not 0 < number(begin.get('frame_us')) <= 25000:
        control_violations.append('missing or invalid NES frame interval')
    if not experiment or any(begin.get(k) != experiment.get(k) for k in ('group_total', 'frames', 'warmup')):
        control_violations.append('group counts disagree with unique experiment metadata')
    if any(f['skip'] != number(effective_controls.get('skip')) for f in frames):
        control_violations.append('per-frame requested skip disagrees with fixed control')
    restores = [r for r in records if r['event'] == 'DIAG_RESTORE']
    if len(restores) != 1:
        control_violations.append('missing or duplicate snapshot restore verification')
    else:
        restore = restores[0]['fields']
        if restore.get('equal') != '1' or restore.get('snapshot_crc') != begin.get('snapshot_crc') or restore.get('restored_crc') != begin.get('snapshot_crc'):
            control_violations.append('snapshot restore verification failed or CRCs disagree')
    for key, expected in (('mode', cpu), ('sampling', sampling)):
        if enum_value(end.get(key), CPU_NAMES if key == 'mode' else SAMPLING_NAMES) != expected:
            control_violations.append('group end ' + key + ' disagrees with begin')
    if number(end.get('repeat')) != head.get('repeat'):
        control_violations.append('group end repeat disagrees with begin')
    if number(end.get('video_errors')) != 0:
        control_violations.append('group reported video errors')
    cpu_records = [r for r in records if r['event'] == 'DIAG_CPU']
    if len(cpu_records) != 1:
        control_violations.append('missing or duplicate CPU execution counters')
    else:
        counters = _numeric(cpu_records[0]['fields'], ('compiled', 'hits', 'misses', 'native_ops', 'deopts', 'rejected',
                            'invalidations', 'code_bytes', 'hle_batches', 'hle_pairs', 'hle_cycles'), control_violations, 'CPU counters')
        if cpu in (0, 1) and any(counters[k] not in (None, 0) for k in ('hle_batches', 'hle_pairs', 'hle_cycles')):
            control_violations.append('HLE work observed in a mode that must disable HLE')
        if cpu == 0 and any(counters[k] not in (None, 0) for k in ('compiled', 'hits', 'misses', 'native_ops', 'deopts', 'rejected', 'code_bytes')):
            control_violations.append('native/JIT work observed in interpreter mode')
        if cpu in (1, 2) and (not counters['compiled'] or not counters['native_ops']):
            control_violations.append('declared JIT mode has no observed compilation/native execution')
    if end.get('reason') != 'complete' or number(end.get('partial')) != 0:
        control_violations.append('group did not finish the normal complete path')
    for f in frames:
        a = f['audio']
        if a.get('failures') not in (None, 0):
            control_violations.append('PCM interface failure observed')
            break
    if any(a.get(k) is None or not (a[k] & 1) or (a[k] & 12) or
           (not f['warm'] and not (a[k] & 2)) for f in frames for a in (f['audio'],)
           for k in ('flags_begin', 'flags_end')):
        control_violations.append('PCM inactive, paused, in storage pause, or not playing during measurement')
    if begin.get('storage') == 'disabled' and any((f['audio']['storage'] or 0) or
                                                  f['scopes'][SCOPES.index('save')] for f in frames):
        control_violations.append('storage is declared disabled but save/storage activity was observed')
    return {'group': head.get('group'), 'repeat': head.get('repeat'), 'cpu_mode': cpu,
            'cpu_name': CPU_NAMES[cpu] if cpu is not None else 'invalid',
            'sampling': sampling, 'sampling_name': SAMPLING_NAMES[sampling] if sampling is not None else 'invalid',
            'snapshot_crc': begin.get('snapshot_crc'), 'expected_frames': head.get('frames'),
            'warmup_frames': head.get('warmup'), 'observed_frames': observed,
            'completed_core_frames': number(end.get('completed')), 'partial_final_frame': number(end.get('partial')) == 1,
            'complete': not issues, 'issues': issues, 'fine_unavailable_reasons': fine_reasons,
            'control_violations': control_violations,
            'effective_controls': effective_controls, 'clock_contract': clock_contract,
            'restore_records': restores, 'cpu_counter_records': cpu_records,
            'clock_fault_records': faults, 'frame_faults': frame_faults,
            'clock_fault_records_may_be_cached': sampling == 2,
            'clock_calibration_records': calibration,
            'clock_diagnostic_records': [r for r in records if r['event'].startswith(('PROFILE_CLOCK', 'PROFILE_TCU', 'PROFILE_RUN_BOUNDARY'))],
            'group_begin': begin, 'group_end': end, 'trace_begin': trace,
            'phases': phases, 'gap_correlations': correlations, 'frames': frames}


def _control_signature(group):
    return (group['snapshot_crc'], group['expected_frames'], group['warmup_frames'],
            tuple(sorted({f['skip'] for f in group['frames']})),
            tuple(group['effective_controls'].get(key) for key in ('frame_us', 'storage', 'scale', 'sound', 'volume', 'adaptive_skip')))


def _rtc_latency_change(baseline, candidate):
    a, b = baseline['rtc'], candidate['rtc']
    result = {'valid': False, 'observed_change_percent': None,
              'change_percent_interval': None, 'endpoint_bound_excludes_zero': False,
              'uncertainty': 'RTC endpoint quantization only; one run per extra sampling level'}
    if not (a['valid'] and b['valid'] and baseline['frames'] and candidate['frames'] and a['seconds']):
        return result
    a_low, a_high = (v / baseline['frames'] for v in a['elapsed_seconds_interval'])
    b_low, b_high = (v / candidate['frames'] for v in b['elapsed_seconds_interval'])
    low = (b_low / a_high - 1) * 100
    high = (b_high / a_low - 1) * 100 if a_low else None
    result.update(valid=True,
                  observed_change_percent=(b['seconds'] / candidate['frames'] /
                                           (a['seconds'] / baseline['frames']) - 1) * 100,
                  change_percent_interval=[low, high],
                  endpoint_bound_excludes_zero=low > 0 or (high is not None and high < 0))
    return result


def _comparisons(groups):
    summaries, selected, selected_rtc = [], {}, {}
    for mode, name in enumerate(CPU_NAMES):
        candidates = [g for g in groups if g['cpu_mode'] == mode and g['sampling'] == 0]
        rtc_eligible = [g for g in candidates if g['complete'] and not g['control_violations'] and
                        g['phases']['measure'] and g['phases']['measure']['rtc']['valid'] and
                        g['phases']['measure']['rtc']['emulated_fps'] is not None]
        rtc_ready = (sorted(g['repeat'] for g in rtc_eligible) == [1, 2, 3] and
                     len({_control_signature(g) for g in rtc_eligible}) == 1)
        if rtc_ready:
            selected_rtc[mode] = {g['repeat']: g for g in rtc_eligible}
        eligible = [g for g in candidates if g['complete'] and not g['control_violations'] and g['phases']['measure'] and
                    g['phases']['measure']['fine']['valid']]
        reason = []
        if sorted(g['repeat'] for g in eligible) != [1, 2, 3]:
            reason.append('need exactly three valid light repeats 1,2,3')
        if len({g['snapshot_crc'] for g in eligible}) > 1:
            reason.append('snapshot identity differs')
        if len({_control_signature(g) for g in eligible}) > 1:
            reason.append('snapshot, frame count, warmup, pacing, storage, or display/audio settings differ')
        metrics = {}
        for metric in ('loop_mean_ms', 'core_mean_ms', 'loop_p95_ms', 'core_p95_ms'):
            metrics[metric] = _stat([g['phases']['measure']['fine'][metric] for g in eligible])
        metrics['rtc_emulated_fps'] = _stat([g['phases']['measure']['rtc']['emulated_fps'] for g in rtc_eligible])
        metrics['rtc_displayed_fps'] = _stat([g['phases']['measure']['rtc']['displayed_fps'] for g in rtc_eligible])
        summaries.append({'cpu_mode': mode, 'cpu_name': name, 'valid_light_groups': [g['group'] for g in eligible],
                          'three_repeat_comparison_ready': not reason,
                          'rtc_three_repeat_comparison_ready': rtc_ready,
                          'rtc_repeats': [{'group': g['group'], 'repeat': g['repeat'], **g['phases']['measure']['rtc']} for g in rtc_eligible],
                          'issues': reason, 'metrics': metrics})
        if not reason:
            selected[mode] = {g['repeat']: g for g in eligible}
    comparisons = []
    for base, candidate in ((0, 1), (1, 2), (0, 2)):
        result = {'baseline': CPU_NAMES[base], 'candidate': CPU_NAMES[candidate], 'eligible': False,
                  'metrics': {}, 'rtc_overall': {'eligible': False}, 'follow_up': None}
        if base in selected_rtc and candidate in selected_rtc:
            bg, cg = selected_rtc[base], selected_rtc[candidate]
            pair_groups = list(bg.values()) + list(cg.values())
            if len({_control_signature(g) for g in pair_groups}) == 1:
                baseline = [bg[r]['phases']['measure']['rtc']['emulated_fps'] for r in (1, 2, 3)]
                values = [cg[r]['phases']['measure']['rtc']['emulated_fps'] for r in (1, 2, 3)]
                intervals = [(bg[r]['phases']['measure']['rtc']['emulated_fps_interval'],
                              cg[r]['phases']['measure']['rtc']['emulated_fps_interval']) for r in (1, 2, 3)]
                separated = (all(a[1] is not None and b[0] > a[1] for a, b in intervals) or
                             all(b[1] is not None and b[1] < a[0] for a, b in intervals))
                result['rtc_overall'] = {'eligible': True, 'baseline_fps': _stat(baseline), 'candidate_fps': _stat(values),
                                         'paired_fps_change_percent': [(b / a - 1) * 100 for a, b in zip(baseline, values)],
                                         'paired_endpoint_intervals': intervals,
                                         'endpoint_bound_separates_all_three': separated}
                if separated:
                    result['follow_up'] = '整体 RTC 吞吐三次同向且端点量化区间分离；可结合时间线继续定位，不能直接归因缓存或 Mapper 音频。'
        if base in selected and candidate in selected:
            pair_groups = list(selected[base].values()) + list(selected[candidate].values())
            signature = {_control_signature(g) for g in pair_groups}
            result['eligible'] = len(signature) == 1
            if result['eligible']:
                for metric in ('loop_mean_ms', 'core_mean_ms'):
                    baseline = [selected[base][r]['phases']['measure']['fine'][metric] for r in (1, 2, 3)]
                    values = [selected[candidate][r]['phases']['measure']['fine'][metric] for r in (1, 2, 3)]
                    percentages = [(b - a) * 100 / a if a else None for a, b in zip(baseline, values)]
                    same_direction = all(b < a for a, b in zip(baseline, values)) or all(b > a for a, b in zip(baseline, values))
                    disjoint = max(values) < min(baseline) or min(values) > max(baseline)
                    result['metrics'][metric] = {'baseline': _stat(baseline), 'candidate': _stat(values),
                                                  'paired_change_percent': percentages,
                                                  'same_direction_all_three': same_direction,
                                                  'observed_ranges_disjoint': disjoint,
                                                  'consistent_observed_difference': same_direction and disjoint}
                if not result['follow_up'] and any(m['consistent_observed_difference'] for m in result['metrics'].values()):
                    result['follow_up'] = '核心或帧处理段三次同向且观测范围分离；这是局部耗时线索，不等于整体提速，不能判定缓存或 Mapper 音频根因。'
        comparisons.append(result)
    perturbation = []
    for mode, name in enumerate(CPU_NAMES):
        first = [g for g in groups if g['cpu_mode'] == mode and g['repeat'] == 1 and g['complete'] and not g['control_violations']]
        runs = {sampling: [g for g in first if g['sampling'] == sampling] for sampling in (0, 1, 2)}
        entry = {'cpu_name': name, 'eligible': all(len(v) == 1 for v in runs.values()), 'runs': {},
                 'interpretation': '各额外组只有一次；用于检查仪器扰动，不能作为三次性能主比较。'}
        if entry['eligible']:
            selected_runs = [runs[s][0] for s in (0, 1, 2)]
            entry['eligible'] = len({_control_signature(g) for g in selected_runs}) == 1
            for s, group in zip((0, 1, 2), selected_runs):
                phase = group['phases']['measure']
                entry['runs'][SAMPLING_NAMES[s]] = {'group': group['group'], 'rtc': phase['rtc'],
                                                    'fine': {k: v for k, v in phase['fine'].items() if k != 'submodes'}}
            light, detail, rtc = (g['phases']['measure'] for g in selected_runs)
            entry['rtc_latency_comparisons'] = {
                'light_vs_rtc_only': _rtc_latency_change(rtc, light),
                'detail_vs_rtc_only': _rtc_latency_change(rtc, detail),
                'detail_vs_light': _rtc_latency_change(light, detail),
            }
            entry['detail_vs_light_loop_change_percent'] = (
                (detail['fine']['loop_mean_ms'] / light['fine']['loop_mean_ms'] - 1) * 100
                if detail['fine']['valid'] and light['fine']['valid'] and light['fine']['loop_mean_ms'] else None)
        perturbation.append(entry)
    return {'light_by_cpu': summaries, 'paired_cpu_comparisons': comparisons,
            'instrumentation_controls': perturbation}


def analyze_text(text, source_kind='unknown'):
    records, issues = parse_records(text)
    groups, latest, active = [], {}, None
    final_records, begin_records, unassigned = [], [], []
    for record in records:
        event, fields = record['event'], record['fields']
        if event == 'DIAG_GROUP_BEGIN':
            key = fields.get('group')
            if key in latest and latest[key]['orphan'] and all(r['event'] == 'DIAG_RESTORE' for r in latest[key]['records']):
                # The app verifies restore before it opens the measured group.
                active = latest[key]
                active['orphan'] = False
                active['records'].append(record)
                continue
            elif key in latest:
                issues.append(f'line {record["line"]}: repeated group id {key}; possible multiple experiments')
            active = {'records': [record], 'orphan': False}
            groups.append(active)
            latest[key] = active
        elif event == 'DIAG_END':
            final_records.append(record)
        elif event == 'DIAG_BEGIN':
            begin_records.append(record)
        elif fields.get('group') is not None:
            key = fields['group']
            if key not in latest:
                latest[key] = {'records': [], 'orphan': True}
                groups.append(latest[key])
            latest[key]['records'].append(record)
            active = latest[key]
        elif active is not None and (event.startswith('PROFILE_') or event == 'DIAG_TRACE_SCHEMA'):
            active['records'].append(record)
        else:
            unassigned.append(record)
    if len(final_records) != 1:
        issues.append(f'expected one DIAG_END, found {len(final_records)}; log may be truncated or concatenated')
    if len(begin_records) != 1:
        issues.append('missing or multiple DIAG_BEGIN records; analyze experiments separately')
    status = final_records[-1]['fields'].get('status') if final_records else 'truncated'
    if status not in ('done', 'aborted', 'error', 'truncated', 'unsupported', 'disabled'):
        issues.append(f'unknown diagnostic completion status: {status}')
    experiment = begin_records[0]['fields'] if len(begin_records) == 1 else {}
    if (experiment.get('revision') != '1' or not number(experiment.get('boot_frames')) or
            any(number(experiment.get(k)) is None or number(experiment.get(k)) <= 0
                for k in ('group_total', 'frames', 'warmup'))):
        issues.append('missing or invalid experiment revision/frame metadata')
    output_groups = [_group(g['records'], g['orphan'], experiment) for g in groups]
    group_totals = {number(g['group_begin'].get('group_total')) for g in output_groups}
    group_totals.discard(None)
    expected_groups = next(iter(group_totals)) if len(group_totals) == 1 else 15
    if not 1 <= expected_groups <= 1000:
        issues.append('invalid group_total bound')
        expected_groups = 15
    if len(group_totals) > 1:
        issues.append('group_total values disagree')
    if [g['group'] for g in output_groups] != list(range(1, expected_groups + 1)):
        issues.append('group IDs do not form the complete expected experiment')
    if any(not g['complete'] for g in output_groups):
        issues.append('one or more groups are incomplete or inconsistent')
    if any(g['control_violations'] for g in output_groups):
        issues.append('one or more groups violate the controlled experiment conditions')
    result = {'schema_version': 1, 'source_kind': source_kind, 'status': status,
              'experiment_complete': status == 'done' and not issues and bool(output_groups),
              'expected_groups': expected_groups, 'observed_groups': len(output_groups),
              'issues': issues, 'groups': output_groups,
              'diagnostic_begin_records': begin_records, 'diagnostic_end_records': final_records,
              'unassigned_records': unassigned, 'comparisons': _comparisons(output_groups),
              'timing_boundaries': {'overall_throughput': 'RTC phase first frame begin through last frame end, including inter-frame telemetry work',
                                    'loop_fields': 'frame processing segment: input through PCM wait; excludes trace copies, profile_frame aggregation and inter-frame maintenance',
                                    'core_fields': 'core call processing only; not complete-application FPS'},
              'limitations': [
                  'RTC 端点为整秒，elapsed 的量化界为 ±1 秒；它不包含其他未验证的时钟误差。',
                  'emulated_fps 为完成 NES 帧数/墙钟时间，displayed_fps 为实际呈现帧数/墙钟时间。',
                  'core_processing_fps 为排除非核心工作后的处理容量，不是真机完整应用帧率。',
                  'loop_ticks/loop_mean_ms 表示帧处理段（输入至播放等待），不含 trace 前后复制、profile_frame 汇总及帧间维护；不能换算完整应用 FPS。',
                  'RTC phase 端点跨越帧间记录工作，是整体吞吐主指标；TCU 细计时须通过独立 RTC 总时长交叉核对。',
                  'light/detail/RTC-only 都是诊断构建；RTC-only 仍复制 RAM trace，不能折算普通版速度。',
                  'PCM 播放等待可能吸收仪器开销；吞吐无差异不证明测量扰动为零。',
                  'warmup 与 measure 独立；warmup 不自动等同于冷缓存，是否清空 JIT 由应用日志约定。',
                  'DIAG_FRAME.mode 是采样子模式，CPU 模式来自 DIAG_GROUP_BEGIN.mode。',
                  '采样 scope 的百分比只在同一子模式内计算，不能跨模式相加或外推全程热点。',
                  '欠载在回收播放位置时才被观察到；当前及前两帧事件相关不等于因果。',
                  'DIAG_GAP 仅保留该帧最后一次缺口细节，count/missing 保留整帧总数。',
                  '没有缓存未命中或硬件周期计数，不能据此自动认定缓存或 Mapper 音频根因。',
                  '测试/模拟器日志只验证记录和分析协议，不能报告为物理 H1 性能。',
              ]}
    app_begin = [r for r in records if r['event'] == 'APP_BEGIN']
    app_end = [r for r in records if r['event'] == 'APP_END']
    result['app_lifecycle'] = {'begin_records': app_begin, 'end_records': app_end,
                               'normal_exit_verified': len(app_begin) == 1 and len(app_end) == 1 and
                               app_end[0]['fields'].get('result') == '0' and bool(final_records) and
                               app_end[0]['line'] > final_records[-1]['line']}
    ambiguous = any('duplicate field' in issue or 'repeated group id' in issue or
                    'multiple DIAG_BEGIN' in issue for issue in issues) or len(final_records) > 1
    if ambiguous:
        result['comparison_blocked_reason'] = 'ambiguous duplicate fields or concatenated experiments'
        for summary in result['comparisons']['light_by_cpu']:
            summary['three_repeat_comparison_ready'] = False
            summary['rtc_three_repeat_comparison_ready'] = False
            summary['issues'].append(result['comparison_blocked_reason'])
        for comparison in result['comparisons']['paired_cpu_comparisons']:
            comparison['eligible'] = False
            comparison['rtc_overall']['eligible'] = False
            comparison['follow_up'] = None
        for control in result['comparisons']['instrumentation_controls']:
            control['eligible'] = False
    return result


def summary_zh(result):
    lines = [f"诊断状态：{result['status']}；完整组 {sum(g['complete'] for g in result['groups'])}/{result['expected_groups']}。",
             f"来源：{result['source_kind']}。{'完整实验通过结构校验。' if result['experiment_complete'] else '实验不完整或存在校验问题，保留所有异常。'}"]
    if result['source_kind'] in ('synthetic', 'qemu'):
        lines.append('以下数字仅验证记录与分析协议，不代表物理 H1 性能。')
    for item in result['comparisons']['light_by_cpu']:
        metric = item['metrics']['core_mean_ms']
        loop = item['metrics']['loop_mean_ms']
        if item['rtc_three_repeat_comparison_ready']:
            rtc = item['metrics']['rtc_emulated_fps']
            displayed = item['metrics']['rtc_displayed_fps']
            lines.append(f"{item['cpu_name']}：整体 RTC 模拟帧率中位数 {rtc['median']:.2f}，范围 {rtc['min']:.2f}–{rtc['max']:.2f} FPS；显示帧率中位数 {displayed['median']:.2f} FPS，端点量化区间见 JSON。")
        if item['three_repeat_comparison_ready']:
            lines.append(f"{item['cpu_name']}：light 三次核心均值的中位数 {metric['median']:.3f} ms，范围 {metric['min']:.3f}–{metric['max']:.3f} ms；帧处理段（输入至播放等待）均值中位数 {loop['median']:.3f} ms，不能换算完整应用帧率。")
        else:
            lines.append(f"{item['cpu_name']}：不足三次有效 TCU 细计时，不作核心耗时结论；RTC 单独判断。")
    rtc_only, faults, cached_faults, gaps = [], 0, 0, 0
    for group in result['groups']:
        faults += bool((group['clock_fault_records'] and group['sampling'] != 2) or group['frame_faults'])
        cached_faults += bool(group['clock_fault_records'] and group['sampling'] == 2)
        phase = group['phases'].get('measure')
        if phase:
            gaps += phase['audio']['gaps']
            if phase['rtc']['valid'] and not phase['fine']['valid']:
                fps = phase['rtc']['emulated_fps']
                rtc_only.append(f"组 {group['group']} {fps:.2f} 模拟帧/秒" if fps is not None else f"组 {group['group']} RTC 时段不足 1 秒")
    if rtc_only:
        lines.append('仅粗计时：' + '；'.join(rtc_only) + '。RTC 端点量化界 ±1 秒，区间见 JSON；显示帧率另列。')
    lines.append(f"有时钟/栈故障的组 {faults}；测量段观察到音频缺口 {gaps} 次。缺口与编译/存档的前后帧关联见 JSON，不能当作因果证据。")
    if cached_faults:
        lines.append(f"另有 {cached_faults} 个 RTC-only 组保留先前的 TCU 故障寄存状态；原记录保留，但不算新发生的 TCU 测量失败。")
    violated = [g['group'] for g in result['groups'] if g['control_violations']]
    if violated:
        lines.append('组 ' + ','.join(map(str, violated)) + ' 未满足预设控制条件（详见 JSON），已排除出主比较。')
    stable = [p for p in result['comparisons']['paired_cpu_comparisons'] if p.get('follow_up')]
    if stable:
        lines.append('重复一致的差异：' + '、'.join(p['baseline'] + '→' + p['candidate'] for p in stable) + '；可据时间线继续定位，未判定缓存或 Mapper 音频根因。')
    else:
        lines.append('目前没有满足三次同向且观测范围分离的比较，不建议据此指定优化对象。')
    lines.append('场景为城市进入＋停留：预热与测量分开，测量段保持界面静止。仪器扰动组只有一次，不代替三次主比较。')
    lines.append('RTC 是整体吞吐指标。三种采样都是诊断构建；RTC-only 仍记录 RAM trace。播放等待可吸收仪器开销，吞吐相同不证明扰动为零。')
    if not result['app_lifecycle']['normal_exit_verified']:
        lines.append('未获得唯一且成功的 APP_END，应用正常收尾未获证明；与已采集帧数据是否完整分开报告。')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--json', dest='json_path', type=Path)
    parser.add_argument('--text', dest='text_path', type=Path)
    parser.add_argument('--source-kind', choices=('unknown', 'device', 'synthetic', 'qemu'), default='unknown')
    args = parser.parse_args()
    # Sidecars are new artifacts; the original evidence is never edited.
    if args.json_path is None:
        args.json_path = args.log.with_suffix('.analysis.json')
    if args.text_path is None:
        args.text_path = args.log.with_suffix('.analysis.txt')
    targets = [p.resolve() for p in (args.json_path, args.text_path) if p]
    if args.log.resolve() in targets or len(targets) != len(set(targets)):
        parser.error('output paths must be distinct and must not overwrite the source log')
    raw = args.log.read_bytes()
    text = raw.decode('utf-8-sig', errors='replace')
    result = analyze_text(text, args.source_kind)
    result['source'] = {'path': str(args.log.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'bytes': len(raw), 'encoding_replacement_count': text.count('\ufffd')}
    rendered = summary_zh(result)
    if args.json_path:
        args.json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.text_path:
        args.text_path.write_text(rendered, encoding='utf-8')
    print(rendered, end='')


if __name__ == '__main__':
    main()
