"""Synthetic log tests. These numbers are protocol fixtures, never H1 timings."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('analyze_diagnostic', ROOT / 'tools/analyze_diagnostic.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PLAN = [(0, 1, 2), (0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0),
        (1, 1, 2), (2, 1, 2), (2, 1, 0), (2, 1, 1), (2, 2, 0),
        (0, 2, 0), (1, 2, 0), (1, 3, 0), (2, 3, 0), (0, 3, 0)]


def record(event, **fields):
    return event + ' ' + ' '.join(f'{k}={v}' for k, v in fields.items())


def fixture_group(group, mode, repeat, sampling, count=8, fault=False, aborted=False,
                  anomaly=False, snapshot='1234ABCD', rtc_backwards=False, storage_events=False):
    # Each synthetic frame spans one RTC second. TCU segments are shorter than
    # the wall interval; terminal total includes the intentionally modeled gap.
    rows = [record('DIAG_RESTORE', group=group, snapshot_crc=snapshot, restored_crc=snapshot, equal=1),
            record('DIAG_GROUP_BEGIN', group=group, group_total=15, mode=mode,
                   repeat=repeat, sampling=sampling, snapshot_crc=snapshot,
                   frames=8, warmup=2, storage='disabled', frame_us=16639,
                   scale=1, skip=1, sound=1, adaptive_skip=0)]
    if sampling != 2:
        for phase, rtc, tick in (('align', 90, 0), ('measure', 91, 32768)):
            rows.append(record('PROFILE_CLOCK_PROBE', phase=phase, rtc_start=rtc, rtc_end=rtc + 1,
                               counter_start=tick, counter_end=tick + 32768, counter_delta=32768,
                               raw_ticks=80, iterations=100, outcome='RTC_edge'))
        rows.append(record('PROFILE_CLOCK', ready=1, hz=32768, observed_hz=32768,
                           calibration_RTC_seconds=1, resolution_us=31, source='TCU5_RTC'))
        rows.append(record('PROFILE_CLOCK_COST', reads=4096, ticks=10, raw_ticks=0,
                           rtc_seconds=0, valid=1, subtract_from_frames=0))
        rows.append(record('PROFILE_TCU_READS', stage='calibrated', fault=0, period=65536, total=65546))
    reason = 'user_abort' if aborted else 'complete'
    rows.append(record('DIAG_TRACE_BEGIN', revision=1, group=group, reason=reason,
                       sampling=MODULE.SAMPLING_NAMES[sampling], recorded=count, expected=8,
                       warmup=2, capacity=1800, overflow=0, unbalanced=0, hz=32768,
                       frame_bytes=324, allocated=1, audio_snapshot='cached_no_retire'))
    rows.append(record('DIAG_TRACE_SCHEMA', scopes=','.join(MODULE.SCOPES), scopes_exclusive=1))
    frames = []
    for n in range(1, count + 1):
        fine = sampling != 2 and not (fault and n >= 4)
        core = (30000 - mode * 1500 + repeat * 3) if fine else 0
        if anomaly and repeat == 2:
            core += 2500
        loop = core + 100 if fine else 0
        rtc = 100 + n - 1
        if rtc_backwards and n == 5:
            rtc -= 2
        values = dict(group=group, frame=n, warm=int(n <= 2), mode=n % 6 if sampling == 1 else 0,
                      skip=1, displayed=n % 2, fine=int(fine), fault=int(fault and n >= 4),
                      rtc_begin=rtc, rtc_end=rtc + 1, raw_begin=(n - 1) * 80, raw_end=n * 80,
                      loop=loop, core=core, reads=8 if fine else 0, compile=int(mode > 0 and n == 4),
                      code_begin=1024 if mode else 0, code_end=(1024 + int(n >= 4) * 64) if mode else 0)
        frames.append(values)
    for phase, selected in (('warmup', frames[:2]), ('measure', frames[2:])):
        if selected:
            rows.append(record('DIAG_PHASE', group=group, phase=phase, frames=len(selected),
                               displayed=sum(f['displayed'] for f in selected),
                               rtc_begin=selected[0]['rtc_begin'], rtc_end=selected[-1]['rtc_end'],
                               seconds=selected[-1]['rtc_end'] - selected[0]['rtc_begin'],
                               raw_ticks=selected[-1]['raw_end'] - selected[0]['raw_begin'],
                               rtc_valid=int(not rtc_backwards), fine_frames=sum(f['fine'] for f in selected),
                               loop_ticks=sum(f['loop'] for f in selected), core_ticks=sum(f['core'] for f in selected)))
    for f in frames:
        n = f['frame']
        rows.append(record('DIAG_FRAME', **f))
        ticks = [0] * len(MODULE.SCOPES)
        ticks[0] = f['core']
        if f['fine']:
            ticks[3], ticks[8 if storage_events and n == 4 else 9] = 50, 50
        rows.append(record('DIAG_SCOPES', group=group, frame=n, ticks=','.join(map(str, ticks))))
        rows.append(record('DIAG_AUDIO', group=group, frame=n, queued_begin=400, queued_end=350,
                           flags_begin=3, flags_end=3, read_begin=100, read_end=630, write_begin=500,
                           write_end=980, retired=530, produced=480, retires=1, batches=1, submits=1,
                           drops=0, failures=0, gaps=2 if n == 5 else 0, missing=100 if n == 5 else 0,
                           storage=int(storage_events and n == 4), storage_raw_ticks=2 if storage_events and n == 4 else 0))
        if n == 5:
            rows.append(record('DIAG_GAP', group=group, frame=n, count=2, missing=100,
                               last_queued=400, last_consumed=450, last_old_position=100, last_new_position=550))
    rows.append(record('DIAG_TRACE_END', group=group, recorded=count, complete=int(count == 8)))
    if sampling != 2:
        rows.append(record('PROFILE_TCU_READS', stage=reason, fault=2 if fault else 0,
                           period=65536, total=count * 32768))
        rows.append(record('PROFILE_RUN_BOUNDARY', reason=reason, start_rtc=100,
                           fault_frame=4 if fault else 0, fault_previous_rtc=0, fault_current_rtc=0))
    rows.append(record('DIAG_CPU', group=group, compiled=int(mode > 0), hits=10 if mode else 0,
                       misses=int(mode > 0), native_ops=100 if mode else 0, deopts=0, rejected=0,
                       invalidations=2, code_bytes=1088 if mode else 0,
                       hle_batches=5 if mode == 2 else 0, hle_pairs=20 if mode == 2 else 0,
                       hle_cycles=120 if mode == 2 else 0))
    rows.append(record('DIAG_GROUP_END', group=group, completed=count, aborted=int(aborted),
                       mode=MODULE.CPU_NAMES[mode], sampling=MODULE.SAMPLING_NAMES[sampling],
                       repeat=repeat, partial=0, reason=reason, video_errors=0, suppressed_core_logs=0))
    return rows


def fixture(changes=None):
    changes = changes or {}
    rows = [record('DIAG_BEGIN', source='synthetic', revision=1, group_total=15, frames=8, warmup=2,
                   boot_frames=1500, scale=1, skip=1, sound=1, volume=100, adaptive_skip=0, storage='disabled')]
    for group, (mode, repeat, sampling) in enumerate(PLAN, 1):
        rows.extend(fixture_group(group, mode, repeat, sampling, **changes.get(group, {})))
    rows.append(record('DIAG_END', status='done'))
    # Prefix matches the actual diagnostic writer and must not affect parsing.
    return '\n'.join(f'{i:06d} {line}' for i, line in enumerate(rows, 1)) + '\n'


class DiagnosticAnalyzerTest(unittest.TestCase):
    def test_complete_three_modes_and_sampling_denominators(self):
        result = MODULE.analyze_text(fixture(), 'synthetic')
        self.assertTrue(result['experiment_complete'], result['issues'])
        self.assertEqual(result['source_kind'], 'synthetic')
        self.assertEqual(len(result['groups']), 15)
        for group in result['groups']:
            self.assertTrue(group['complete'], group['issues'])
            measure = group['phases']['measure']
            self.assertEqual(measure['frames'], 6)
            self.assertEqual(measure['rtc']['emulated_fps'], 1)
            self.assertEqual(measure['rtc']['displayed_fps'], 0.5)
            self.assertEqual(measure['rtc']['elapsed_seconds_interval'], [5, 7])
            self.assertEqual(measure['rtc']['emulated_fps_interval'], [6 / 7, 6 / 5])
            self.assertEqual(measure['fine']['valid'], group['sampling'] != 2)
            if group['sampling'] == 1:
                # Every rotating submode keeps its own one-frame denominator.
                self.assertEqual(len(measure['fine']['submodes']), 6)
                for mode in measure['fine']['submodes']:
                    self.assertEqual(mode['frames'], 1)
                    costs = mode['scope_costs']
                    self.assertAlmostEqual(sum(v['percent_of_this_submode_loop'] for v in costs.values()), 100)
        summaries = result['comparisons']['light_by_cpu']
        self.assertTrue(all(g['three_repeat_comparison_ready'] for g in summaries))
        self.assertEqual(summaries[0]['metrics']['core_mean_ms']['count'], 3)
        pairs = result['comparisons']['paired_cpu_comparisons']
        self.assertTrue(all(p['follow_up'] and p['eligible'] for p in pairs))
        self.assertTrue(all(p['eligible'] for p in result['comparisons']['instrumentation_controls']))

    def test_gap_retains_counts_and_previous_compile_save_frame(self):
        result = MODULE.analyze_text(fixture({5: {'storage_events': True}}))
        gap = result['groups'][4]['gap_correlations'][0]
        self.assertEqual((gap['frame'], gap['gaps'], gap['missing']), (5, 2, 100))
        self.assertEqual(gap['same_frame_compiles'], 0)
        previous = gap['nearby_frames'][1]
        self.assertEqual((previous['frame'], previous['compiles'], previous['storage_pauses']), (4, 1, 1))
        self.assertEqual(previous['save_ticks_raw'], 50)
        self.assertTrue(result['groups'][4]['control_violations'])
        self.assertFalse(result['comparisons']['light_by_cpu'][1]['three_repeat_comparison_ready'])

    def test_fault_never_becomes_fine_percent_but_rtc_survives(self):
        result = MODULE.analyze_text(fixture({2: {'fault': True}}))
        group = result['groups'][1]
        self.assertTrue(group['complete'])
        self.assertTrue(group['clock_fault_records'] and group['frame_faults'])
        for phase in group['phases'].values():
            self.assertFalse(phase['fine']['valid'])
            self.assertFalse(phase['fine']['submodes'])
            self.assertTrue(phase['rtc']['valid'])
        self.assertFalse(result['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'])
        self.assertIn('仅粗计时', MODULE.summary_zh(result))

    def test_rtc_invalid_blocks_fine_even_when_tcu_flags_good(self):
        result = MODULE.analyze_text(fixture({2: {'rtc_backwards': True}}))
        measure = result['groups'][1]['phases']['measure']
        self.assertFalse(measure['rtc']['valid'])
        self.assertFalse(measure['fine']['valid'])
        self.assertFalse(measure['fine']['submodes'])

    def test_aborted_partial_trace_is_retained_not_selected(self):
        rows = fixture_group(1, 0, 1, 0, count=5, aborted=True)
        rows.append(record('DIAG_END', status='aborted'))
        result = MODULE.analyze_text('\n'.join(rows))
        group = result['groups'][0]
        self.assertEqual(result['status'], 'aborted')
        self.assertFalse(result['experiment_complete'])
        self.assertEqual(group['observed_frames'], 5)
        self.assertTrue(group['phases']['measure']['rtc']['valid'])
        self.assertFalse(group['phases']['measure']['fine']['valid'])
        self.assertEqual(len(group['gap_correlations']), 1)

    def test_abort_in_input_does_not_count_partial_as_emulated_frame(self):
        rows = fixture_group(1, 0, 1, 0, count=5, aborted=True)
        rows[-1] = record('DIAG_GROUP_END', group=1, completed=4, partial=1, aborted=1)
        rows.append(record('DIAG_END', status='aborted'))
        group = MODULE.analyze_text('\n'.join(rows))['groups'][0]
        self.assertTrue(group['partial_final_frame'])
        self.assertEqual(group['completed_core_frames'], 4)
        self.assertEqual(group['observed_frames'], 5)
        self.assertFalse(group['frames'][-1]['core_completed'])
        self.assertEqual(group['phases']['measure']['recorded_frames'], 3)
        self.assertEqual(group['phases']['measure']['frames'], 2)
        self.assertEqual(group['phases']['measure']['rtc']['emulated_fps'], 2 / 3)

    def test_duplicate_fields_or_experiments_block_all_comparisons(self):
        source = fixture()
        for changed in (source.replace('mode=0 repeat=1 sampling=2', 'mode=0 mode=0 repeat=1 sampling=2', 1),
                        source + 'DIAG_END status=done\n', source + source):
            result = MODULE.analyze_text(changed)
            self.assertFalse(result['experiment_complete'])
            self.assertIn('comparison_blocked_reason', result)
            self.assertFalse(any(p['eligible'] or p['follow_up'] for p in result['comparisons']['paired_cpu_comparisons']))

    def test_malformed_numeric_fields_report_issues_without_crashing(self):
        source = '\n'.join(fixture_group(1, 0, 1, 0)) + '\nDIAG_END status=done\n'
        for original in ('frames=8', 'warmup=2', 'loop=30103', 'core=30003', 'rtc_begin=100',
                         'rtc_end=101', 'raw_begin=0', 'fault=0', 'queued_begin=400',
                         'fine=1', 'compile=0', 'mode=0', 'skip=1', 'sampling=0', 'hz=32768'):
            for value in ('invalid', '-1'):
                key = original.split('=')[0]
                self.assertIn(original, source)
                result = MODULE.analyze_text(source.replace(original, key + '=' + value, 1))
                group = result['groups'][0]
                self.assertTrue(not group['phases']['measure']['fine']['valid'] or group['control_violations'], (original, value))
        self.assertFalse(MODULE.analyze_text('')['experiment_complete'])

    def test_rtc_zero_second_interval_has_no_point_fps(self):
        metrics = MODULE._rtc_metrics(5, 2, 0, True)
        self.assertIsNone(metrics['emulated_fps'])
        self.assertEqual(metrics['emulated_fps_interval'], [5, None])
        self.assertEqual(metrics['elapsed_seconds_interval'], [0, 1])

    def test_truncation_duplicate_and_missing_frame_records_fail_closed(self):
        text = fixture()
        truncated = text[:text.index('DIAG_TRACE_END')]
        result = MODULE.analyze_text(truncated)
        self.assertEqual(result['status'], 'truncated')
        self.assertFalse(result['experiment_complete'])
        for mutation in ('duplicate', 'audio_missing', 'gap_missing'):
            lines = text.splitlines()
            event = {'duplicate': 'DIAG_FRAME group=2 frame=4 ',
                     'audio_missing': 'DIAG_AUDIO group=2 frame=4 ',
                     'gap_missing': 'DIAG_GAP group=2 frame=5 '}[mutation]
            index = next(i for i, line in enumerate(lines) if event in line)
            if mutation == 'duplicate':
                lines.insert(index, lines[index])
            else:
                del lines[index]
            damaged = MODULE.analyze_text('\n'.join(lines))
            group = damaged['groups'][1]
            self.assertFalse(group['complete'], mutation)
            self.assertFalse(group['phases']['measure']['fine']['valid'], mutation)

    def test_snapshot_and_nonrepeatable_difference_block_followup(self):
        mismatched = MODULE.analyze_text(fixture({11: {'snapshot': 'OTHER'}}))
        self.assertFalse(mismatched['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'])
        variable = MODULE.analyze_text(fixture({12: {'anomaly': True}}))
        comparison = variable['comparisons']['paired_cpu_comparisons'][0]
        self.assertTrue(comparison['eligible'])
        self.assertFalse(comparison['metrics']['core_mean_ms']['same_direction_all_three'])
        self.assertIsNone(comparison['follow_up'])

    def test_real_app_string_metadata_and_restore_control(self):
        lines = fixture().splitlines()
        for i, line in enumerate(lines):
            if 'DIAG_GROUP_BEGIN ' in line:
                for mode, name in enumerate(MODULE.CPU_NAMES):
                    line = line.replace(f'mode={mode} ', f'mode={name} ')
                for sampling, name in enumerate(MODULE.SAMPLING_NAMES):
                    line = line.replace(f'sampling={sampling} ', f'sampling={name} ')
                lines[i] = line
        result = MODULE.analyze_text('\n'.join(lines))
        self.assertTrue(result['experiment_complete'])
        self.assertTrue(all(g['three_repeat_comparison_ready'] for g in result['comparisons']['light_by_cpu']))
        broken = '\n'.join(lines).replace('DIAG_RESTORE group=2 snapshot_crc=1234ABCD restored_crc=1234ABCD equal=1',
                                          'DIAG_RESTORE group=2 snapshot_crc=1234ABCD restored_crc=00000000 equal=0')
        rejected = MODULE.analyze_text(broken)
        self.assertTrue(rejected['groups'][1]['control_violations'])
        self.assertFalse(rejected['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'])

    def test_no_clock_calibration_no_invented_fine_measurement(self):
        text = '\n'.join(line for line in fixture().splitlines() if 'PROFILE_CLOCK ' not in line)
        result = MODULE.analyze_text(text)
        group = result['groups'][1]
        self.assertIn('no_valid_clock_calibration', group['fine_unavailable_reasons'])
        self.assertFalse(group['phases']['measure']['fine']['valid'])
        self.assertTrue(group['phases']['measure']['rtc']['valid'])

    def test_full_clock_evidence_contract_not_just_ready_flag(self):
        source = fixture()
        mutations = {
            'observed_frequency': source.replace('observed_hz=32768', 'observed_hz=1'),
            'source': source.replace('source=TCU5_RTC', 'source=unknown'),
            'cost_invalid': source.replace('valid=1 subtract_from_frames=0', 'valid=0 subtract_from_frames=0'),
            'cost_incomplete': source.replace('PROFILE_CLOCK_COST reads=4096', 'PROFILE_CLOCK_COST reads=123'),
            'cost_missing': '\n'.join(x for x in source.splitlines() if 'PROFILE_CLOCK_COST ' not in x),
            'measurement_probe_missing': '\n'.join(x for x in source.splitlines() if 'PROFILE_CLOCK_PROBE phase=measure ' not in x),
            'boundary_missing': '\n'.join(x for x in source.splitlines() if 'PROFILE_RUN_BOUNDARY ' not in x),
            'duplicate_calibration': '\n'.join(x + ('\n' + x if 'PROFILE_CLOCK source=' in x or 'PROFILE_CLOCK ready=' in x else '') for x in source.splitlines()),
        }
        for name, changed in mutations.items():
            result = MODULE.analyze_text(changed)
            group = result['groups'][1]
            self.assertFalse(group['clock_contract']['valid'], name)
            self.assertFalse(group['phases']['measure']['fine']['valid'], name)
            self.assertTrue(group['phases']['measure']['rtc']['valid'], name)
            self.assertFalse(result['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'], name)

    def test_tcu_total_must_agree_with_independent_rtc_not_frame_tick_sum(self):
        source = fixture()
        # The old loose fixture had eight RTC seconds but ~0.27 TCU seconds.
        # All fine/ready flags remain 1 and all fault flags remain 0.
        bad = source.replace('stage=complete fault=0 period=65536 total=262144',
                             'stage=complete fault=0 period=65536 total=9000')
        result = MODULE.analyze_text(bad)
        group = result['groups'][1]
        cross = group['clock_contract']['crosscheck']
        self.assertEqual(cross['rtc_seconds'], 8)
        self.assertAlmostEqual(cross['tcu_seconds'], 9000 / 32768)
        self.assertFalse(cross['valid'])
        self.assertFalse(group['phases']['measure']['fine']['valid'])
        self.assertTrue(group['phases']['measure']['rtc']['valid'])
        self.assertFalse(group['frame_faults'] or group['clock_fault_records'])
        self.assertFalse(result['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'])
        valid = MODULE.analyze_text(source)['groups'][1]['clock_contract']['crosscheck']
        self.assertTrue(valid['valid'])
        self.assertGreater(valid['ticks_outside_frame_processing_segments'], 0)

    def test_metadata_cpu_and_pcm_failure_negatives_are_not_comparable(self):
        source = fixture()
        mutations = {
            'begin_missing': '\n'.join(x for x in source.splitlines() if 'DIAG_BEGIN ' not in x),
            'sound_missing': source.replace(' sound=1', ''),
            'volume_missing': source.replace(' volume=100', ''),
            'adaptive_skip': source.replace('adaptive_skip=0', 'adaptive_skip=1'),
            'per_frame_skip': '\n'.join(x.replace('skip=1', 'skip=5') if 'DIAG_FRAME ' in x else x for x in source.splitlines()),
            'cpu_missing': '\n'.join(x for x in source.splitlines() if 'DIAG_CPU ' not in x),
            'cpu_duplicate': '\n'.join(x + ('\n' + x if 'DIAG_CPU ' in x else '') for x in source.splitlines()),
            'native_in_interpreter': source.replace('native_ops=0', 'native_ops=1'),
            'pcm_failure': source.replace('failures=0', 'failures=1'),
            'pcm_inactive': source.replace('flags_begin=3 flags_end=3', 'flags_begin=0 flags_end=0'),
            'pcm_paused': source.replace('flags_begin=3 flags_end=3', 'flags_begin=7 flags_end=7'),
            'end_error': '\n'.join(x.replace('reason=complete', 'reason=PCM_failure') if 'DIAG_GROUP_END ' in x else x for x in source.splitlines()),
        }
        for name, changed in mutations.items():
            result = MODULE.analyze_text(changed)
            self.assertFalse(result['experiment_complete'], name)
            self.assertTrue(result['groups'][1]['control_violations'], name)
            self.assertFalse(result['comparisons']['light_by_cpu'][0]['three_repeat_comparison_ready'], name)

    def test_frame_processing_label_never_claims_complete_wall_time(self):
        summary = MODULE.summary_zh(MODULE.analyze_text(fixture(), 'synthetic'))
        self.assertIn('帧处理段（输入至播放等待）', summary)
        self.assertNotIn('完整帧循环', summary)
        self.assertIn('RTC 是整体吞吐指标', summary)
        self.assertIn('吞吐相同不证明扰动为零', summary)

    def test_application_exit_is_separate_from_complete_collected_data(self):
        source = 'APP_BEGIN version=test\n' + fixture() + 'APP_END result=0\n'
        normal = MODULE.analyze_text(source)
        self.assertTrue(normal['experiment_complete'])
        self.assertTrue(normal['app_lifecycle']['normal_exit_verified'])
        for changed in (source.replace('APP_END result=0\n', ''), source.replace('APP_END result=0', 'APP_END result=1')):
            result = MODULE.analyze_text(changed)
            self.assertTrue(result['experiment_complete'])
            self.assertFalse(result['app_lifecycle']['normal_exit_verified'])
            self.assertIn('应用正常收尾未获证明', MODULE.summary_zh(result))

    def test_rtc_only_cached_tcu_fault_is_retained_but_not_new_measurement(self):
        source = fixture().replace('DIAG_CPU group=1 ', 'PROFILE_TCU_READS stage=complete fault=2 period=65536 total=1234\nDIAG_CPU group=1 ', 1)
        result = MODULE.analyze_text(source)
        group = result['groups'][0]
        self.assertTrue(group['clock_fault_records'])
        self.assertTrue(group['clock_fault_records_may_be_cached'])
        self.assertNotIn('clock_or_scope_fault', group['fine_unavailable_reasons'])
        self.assertTrue(group['phases']['measure']['rtc']['valid'])

    def test_cli_never_changes_input_and_rejects_output_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source, output, summary = path / 'log.txt', path / 'result.json', path / 'summary.txt'
            original = fixture().encode()
            source.write_bytes(original)
            command = [sys.executable, str(ROOT / 'tools/analyze_diagnostic.py'), str(source)]
            process = subprocess.run(command + ['--json', str(output), '--text', str(summary),
                                                '--source-kind', 'synthetic'], capture_output=True, text=True,
                                     encoding='utf-8', errors='replace')
            self.assertEqual(process.returncode, 0, process.stderr)
            data = json.loads(output.read_text(encoding='utf-8'))
            self.assertTrue(data['experiment_complete'])
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(summary.read_text(encoding='utf-8').startswith('诊断状态'))
            reject = subprocess.run(command + ['--json', str(source)], capture_output=True)
            self.assertNotEqual(reject.returncode, 0)
            self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
