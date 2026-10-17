"""Offline comparison of session.py capture_app decoded JSON endpoints.

No USB, serial, device reads, or implicit raw-tick frequency conversion.
Counts/rates describe each block's own window; context eligibility is separate.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path

COUNTERS = {
    'app': ('total_frames', 'videos', 'video_errors', 'audio_catchup_frames'),
    'audio': ('batches', 'submitted', 'underruns', 'dropped', 'failures', 'nonzero',
              'storage_pauses', 'storage_ticks'),
}


def number(x):
    return type(x) in (int, float) and math.isfinite(x)


def delta(a, b):
    if type(a) is not int or type(b) is not int or not 0 <= a < 2**32 or not 0 <= b < 2**32:
        return dict(before=a, after=b, valid=False, delta=None, reason='not unsigned 32-bit counters')
    if b < a or b-a > 0x7fffffff:
        return dict(before=a, after=b, valid=False, delta=None,
                    modulo_candidate=(b-a)&0xffffffff,
                    reason='reset/wrap/large jump is ambiguous; modulo candidate is not accepted')
    return dict(before=a, after=b, valid=True, delta=b-a)


def timing(a, b, margin=0):
    values=[a.get('host_begin'), a.get('host_end'), b.get('host_begin'), b.get('host_end')]
    if not all(number(v) for v in values):
        return dict(valid=False, reason='missing/nonfinite host request brackets')
    a0,a1,b0,b1=values
    if not a0 <= a1 < b0 <= b1:
        return dict(valid=False, reason='reversed or overlapping host request brackets')
    low,high=b0-a1-2*margin,b1-a0+2*margin
    if low <= 0:
        return dict(valid=False, reason='timestamp margin consumes elapsed lower bound')
    return dict(valid=True, before=[a0,a1], after=[b0,b1],
                midpoint_seconds=((b0+b1)-(a0+a1))/2,
                interval_seconds=[low,high], endpoint_margin_seconds=margin,
                boundary_rpc_seconds=[a1-a0,b1-b0])


def rate(counter, elapsed):
    if not counter['valid'] or not elapsed['valid']:return None
    n=counter['delta'];low,high=elapsed['interval_seconds']
    return dict(per_second=n/elapsed['midpoint_seconds'], interval=[n/high,n/low])


def analyze(first, second, *, label='', margin=0):
    if not number(margin) or margin < 0:raise ValueError('Invalid endpoint timestamp margin')
    issues=[];notes=[];blocks={};context_issues=[]
    if first.get('schema')!=1 or second.get('schema')!=1:issues.append('unsupported capture schema')
    sha=first.get('bda_sha256')
    if not isinstance(sha,str) or len(sha)!=64 or sha!=second.get('bda_sha256'):
        issues.append('BDA identity missing or different within window')
    if first.get('signature_verified') is not True or second.get('signature_verified') is not True:
        issues.append('loaded code signature unverified')
    if not first.get('resident_image_id') or first.get('resident_image_id')!=second.get('resident_image_id'):
        issues.append('resident image missing or changed')
    reads=[first.get('reads',{}),second.get('reads',{})]
    for name in COUNTERS:
        if name not in reads[0] or name not in reads[1]:
            issues.append(f'missing required {name} block');continue
        a,b=reads[0][name],reads[1][name]
        elapsed=timing(a,b,margin)
        counters={k:delta(a.get('values',{}).get(k),b.get('values',{}).get(k)) for k in COUNTERS[name]}
        for k,value in counters.items():
            value['rate']=rate(value,elapsed)
            if not value['valid']:issues.append(f'{name}.{k}: {value["reason"]}')
        if not elapsed['valid']:issues.append(f'{name}: {elapsed["reason"]}')
        blocks[name]=dict(timing=elapsed,counters=counters,
                          endpoint_values=[a.get('values',{}),b.get('values',{})])
    if 'jit' in reads[0] and 'jit' in reads[1]:
        a,b=reads[0]['jit'],reads[1]['jit'];aa,bb=a['values'],b['values'];counts={}
        elapsed=timing(a,b,margin)
        if not elapsed['valid']:issues.append(f'jit: {elapsed["reason"]}')
        for name in sorted(set(aa)&set(bb)):
            if isinstance(aa[name],dict) and isinstance(bb[name],dict):
                for k in sorted(set(aa[name])|set(bb[name])):
                    if k=='max_words':continue  # high-water mark, not a counter
                    counts[name+'.'+k]=delta(aa[name].get(k),bb[name].get(k))
            elif name in ('h1_jit_links','h1_jit_wraps'):
                counts[name]=delta(aa[name],bb[name])
        for k,v in counts.items():
            v['rate']=rate(v,elapsed)
            if not v['valid']:issues.append(f'jit.{k}: {v["reason"]}')
        for k in ('cache','cache_bytes','h1_hle_sgz2_enabled'):
            if aa.get(k)!=bb.get(k):context_issues.append(f'JIT {k} changed')
        blocks['jit']=dict(timing=elapsed,counters=counts,endpoint_values=[aa,bb])
    else:context_issues.append('JIT context absent')
    for name in ('cartridge','session'):
        if name not in reads[0] or name not in reads[1]:
            context_issues.append(f'{name} context absent at an endpoint');continue
        a,b=(r[name]['values'] for r in reads)
        if name=='cartridge':
            if a!=b:context_issues.append('cartridge identity changed')
        else:
            for i,state in enumerate((a,b)):
                if state.get('loaded')!=1 or any(state.get(k)!=0 for k in
                    ('pause_requested','exit_requested','menu_requested','input_mask')):
                    context_issues.append(f'endpoint{i} session is unloaded/paused/active-input or incomplete')
            for k in ('scale','configured_skip'):
                if k not in a or a.get(k)!=b.get(k):context_issues.append(f'display setting {k} absent or changed')
    if 'audio' in blocks:
        for i,state in enumerate(blocks['audio']['endpoint_values']):
            if state.get('active')!=1 or state.get('paused')!=0:
                context_issues.append(f'endpoint{i} audio is inactive/paused or incomplete')
            if state.get('storage_paused'):notes.append(f'endpoint{i} is inside a storage pause; its unfinished duration may be counted after this endpoint')
        if blocks['audio']['endpoint_values'][0].get('rate')!=blocks['audio']['endpoint_values'][1].get('rate'):
            context_issues.append('audio sample rate changed')
    statuses=[first.get('status') or {},second.get('status') or {}]
    status_deltas={};quietness={}
    for i,state in enumerate(statuses):
        if (state.get('version')!=1 or state.get('state')!=1 or state.get('usb_configured')!=1 or
            any(state.get(k)!=0 for k in ('usb_fault','last_error','keys_low','keys_high')) or
            state.get('exec_state') not in (0,1,2,5)):
            context_issues.append(f'endpoint{i} resident is busy/unhealthy or incomplete')
    for k in ('resident_base','resident_size','usb_resets','bad_frames'):
        if statuses[0].get(k) is None or statuses[0].get(k)!=statuses[1].get(k):
            context_issues.append(f'STATUS.{k} absent or changed')
    for k in ('tick','poll_count','commands'):
        status_deltas[k]=delta(statuses[0].get(k),statuses[1].get(k))
        if not status_deltas[k]['valid']:context_issues.append(f'STATUS.{k} reset/wrap/absent')
    # This relation is specific to session.py capture_app: one STATUS first,
    # then one READ per <=512B manifest block. It brackets commands, not CPU
    # work; local inputs or autonomous work are not ruled out by equal counts.
    expected=len(reads[0])+1
    quietness=dict(expected_commands_from_capture_app=expected,
                   observed_commands=status_deltas['commands'].get('delta'),
                   consistent_with_no_extra_host_commands=status_deltas['commands'].get('delta')==expected)
    if not quietness['consistent_with_no_extra_host_commands']:
        context_issues.append('STATUS command count inconsistent with quiet capture_app interval; inspect wire log')
    spans=[]
    for endpoint in reads:
        ts=[(r.get('host_begin'),r.get('host_end')) for r in endpoint.values()]
        if ts and all(number(v) for pair in ts for v in pair):
            spans.append(dict(first_read=min(p[0] for p in ts),last_read=max(p[1] for p in ts),
                              span_seconds=max(p[1] for p in ts)-min(p[0] for p in ts)))
        else:spans.append(None)
    def metric(block,name):
        return blocks.get(block,{}).get('counters',{}).get(name,{}).get('rate')
    return dict(schema=1,label=label,bda_sha256=sha,resident_image_id=first.get('resident_image_id'),
                numeric_window_valid=not issues,context_checks_pass=not context_issues,
                ready_for_controlled_comparison=not issues and not context_issues,
                issues=issues,context_issues=context_issues,notes=notes,blocks=blocks,
                observed_rates=dict(emulated_fps=metric('app','total_frames'),displayed_fps=metric('app','videos'),
                                    underrun_events_per_second=metric('audio','underruns')),
                raw_status_deltas=status_deltas,quietness=quietness,endpoint_read_spans=spans,
                limits=[
                    'Each block has its own request/reply elapsed bracket; blocks and individual counters are not atomic.',
                    'frames pacing counter, queue levels and storage_max_ticks are not cumulative interval counts.',
                    'Raw STATUS ticks and storage_ticks are not seconds; no 40Hz/80Hz default or conversion is applied.',
                    'Decreases/large jumps are rejected, not silently modulo-corrected. Two endpoints cannot detect a complete multiwrap/reset followed by catch-up.',
                    'Underruns are event counts, not lost audio percentage or gap duration; a storage pause can cross an endpoint.',
                    'Caller must use the same host clock/session and preserve scene, ROM, settings and observer load; endpoint equality alone cannot prove the entire interval.',
                    'Equal BDA SHA identifies the base image, not live RAM patch state. Pair labels are operator annotations, not proof of patch installation.',
                    'Rates from incomplete context remain observations and do not establish causal optimization gains.',
                ])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',required=True,type=Path);parser.add_argument('--after',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--label',default='')
    parser.add_argument('--endpoint-margin-seconds',type=float,default=0)
    args=parser.parse_args();sources=[];captures=[]
    for p in (args.before,args.after):
        raw=p.read_bytes();captures.append(json.loads(raw.decode('utf-8-sig')))
        sources.append(dict(path=str(p.resolve()),sha256=hashlib.sha256(raw).hexdigest()))
    report=analyze(*captures,label=args.label,margin=args.endpoint_margin_seconds);report['sources']=sources
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('label','numeric_window_valid','context_checks_pass','observed_rates','issues','context_issues')},indent=2))


if __name__=='__main__':main()
