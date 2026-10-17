"""Offline ELF address manifests and H1 RAM-dump decoding. Never opens USB.

The operator supplies bytes captured with the existing H1Debug Client. This
module contains no serial, network, module-execution, memory-write or device
clock code. Addresses are resolved from the actual ELF, including the owning
object of ambiguous local names such as audio.c:active and audio.c:rate.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[1]
COUNTER_ARRAYS = {
    'h1_jit_stats': ['compiled', 'hits', 'misses', 'native_ops', 'deopts', 'rejected', 'invalidations', 'max_words'],
    'h1_jit_bank_stats': ['lookups', 'hits', 'misses', 'stores', 'evictions', 'native_reuse', 'reject_reuse', 'epoch_misses'],
    'h1_jit_run_stats': ['compiled_runs', 'compiled_ops', 'executed_runs', 'rejected_runs'],
    'h1_jit_schedule_stats': ['full_runs', 'cpu_prefix', 'event_prefix', 'contract_reuse'],
    'h1_hle_stats': ['batches', 'pairs', 'cycles'],
}
LIMITS = [
    'Read-only cached software counters; no retire(), core call, firmware-cursor refresh or TCU access.',
    'Independent RPC blocks are not an atomic application snapshot; record each request/reply time.',
    'queued is software queue accounting at the last existing audio update, not instantaneous DMA runway.',
    'Ordinary H1_PROFILE=0 has no profile clock, sampled PPU/APU timing, audio gap ring or diagnostic retired/produced counters.',
    'JIT native_ops are guest 6502 instructions, not host MIPS instructions, physical cycles or CPU percentage.',
    'total_frames is the session emulated-frame counter; frames is reset by pacing and must not be used for FPS.',
    'Keep resident image, polling cadence, ROM/scene/settings and startup state identical across comparisons.',
    'No screenshots, EXEC, input or continuous polling inside a quiet throughput interval; the priority-6 resident perturbs the foreground.',
    'STATUS.tick is a raw ABI counter; do not assume 80 Hz. Report raw delta and host elapsed separately.',
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest(bda_path, elf_path):
    bda_path, elf_path = Path(bda_path), Path(elf_path)
    metadata = json.loads(bda_path.with_suffix('.build.json').read_text(encoding='utf-8'))
    assert digest(bda_path.read_bytes()) == metadata['sha256'], 'BDA/build metadata mismatch'
    supported = {('0.1.15', False), ('0.1.12', True)}
    assert (metadata['version'], metadata['profile']) in supported and metadata['jit'], 'Unreviewed build layout'
    records, allocated_sections = {}, []
    with elf_path.open('rb') as stream:
        elf = ELFFile(stream)
        assert elf.elfclass == 32 and elf.little_endian and elf['e_machine'] == 'EM_MIPS'
        owner = None
        for symbol in elf.get_section_by_name('.symtab').iter_symbols():
            kind, binding = symbol['st_info']['type'], symbol['st_info']['bind']
            if kind == 'STT_FILE':
                owner = symbol.name
            if kind not in ('STT_OBJECT', 'STT_FUNC') or not symbol['st_size']:
                continue
            key = symbol.name if binding != 'STB_LOCAL' else f'{owner}:{symbol.name}'
            assert key not in records, ('ambiguous symbol', key)
            records[key] = dict(address=int(symbol['st_value']), size=int(symbol['st_size']))
        for section in elf.iter_sections():
            if section['sh_flags'] & 2 and section['sh_type'] != 'SHT_NOBITS':
                allocated_sections.append((int(section['sh_addr']), section.data(), int(section['sh_flags']), section.name))
    # Match every ELF load byte against the BDA payload, not just the version
    # label, before using its symbol table to describe a device read.
    payload = bda_path.with_suffix('.bin').read_bytes()
    assert digest(payload) == metadata['payload_sha256'], 'BIN/build metadata mismatch'
    assert bda_path.read_bytes().endswith(payload), 'BDA payload mismatch'
    for address, data, flags, name in allocated_sections:
        assert payload[address-0x83c00020:address-0x83c00020+len(data)] == data, name

    def field(key, label=None, size=4, names=None):
        symbol = records[key]
        expected = len(names)*4 if names else size
        assert symbol['size'] == expected, (key, symbol, expected)
        return dict(name=label or key, symbol=key, address=symbol['address'], bytes=expected,
                    encoding='u32_array' if names else f'u{size*8}', labels=names)

    audio_names = ['storage_max_ticks', 'storage_ticks', 'storage_pauses', 'storage_tick',
                   'storage_paused', 'catchup', 'paused', 'underruns', 'nonzero', 'failures',
                   'dropped', 'submitted', 'batches', 'playing', 'queued', 'read_position',
                   'used', 'rate', 'active']
    fields = {
        'audio': [field('audio.c:'+name, name) for name in audio_names],
        'app': [field('app.c:'+name, name) for name in ('audio_catchup_frames', 'video_errors', 'videos', 'total_frames', 'frames')],
        'jit': [field(name, names=labels) for name, labels in COUNTER_ARRAYS.items() if name in records] +
               [field(name) for name in ('h1_jit_links', 'h1_jit_wraps', 'h1_jit_entries', 'h1_hle_sgz2_enabled') if name in records] +
               [field('nes_jit.c:'+name, name) for name in ('used_bytes', 'cache_bytes', 'cache')],
    }
    # Fixed offsets below are independently checked by target ABI compilation
    # in tests/hardware_probe_test.py; exported struct size is checked here too.
    cart = records['iNESCart'];assert cart['size'] == 136
    fields['cartridge'] = [dict(name=name, address=cart['address']+offset, bytes=size,
                                encoding=encoding, labels=None)
                           for name, offset, size, encoding in (
                               ('mapper', 48, 4, 'u32'), ('prg_bytes', 64, 4, 'u32'),
                               ('chr_bytes', 68, 4, 'u32'), ('region', 104, 4, 'u32'),
                               ('prg_chr_md5', 108, 16, 'hex'), ('prg_crc32', 124, 4, 'u32'),
                               ('chr_crc32', 128, 4, 'u32'), ('crc32', 132, 4, 'u32'))]
    inp = records['app.c:input'];assert inp['size'] == 128
    fields['session'] = [dict(name=name, address=inp['address']+offset, bytes=size,
                             encoding=f'u{size*8}', labels=None)
                        for name, offset, size in (
                            ('pause_requested', 96, 4), ('input_mask', 100, 2),
                            ('exit_requested', 104, 4), ('menu_requested', 108, 4),
                            ('scale', 112, 4), ('configured_skip', 116, 4))] + [
                                field('nes_core.c:loaded', 'loaded', 1),
                                field('nes_core.c:skip_count', 'effective_skip'),
                                field('nes_core.c:skip_phase', 'skip_phase')]
    if metadata['profile']:
        for block, names in (
                ('profile_state', ('fault','frequency','omitted','window_count','enabled','depth')),
                ('profile_tcu', ('pc_owned','pc_stopped','pc_previous','pc_total','pc_terminal',
                                 'pc_deferred','pc_wraps','pc_backwards','pc_retries','pc_fault',
                                 'pc_max_delta','pc_bad_previous','pc_bad_b','pc_bad_a')),
                ('profile_rtc', ('rtc_run_invalid','rtc_run_displayed','rtc_run_frames','rtc_run_last',
                                 'rtc_run_start','fault_frame','fault_current_rtc','fault_previous_rtc',
                                 'run_start_rtc','frame_rtc','last')),
                ('profile_sampling', ('clock_reads','previous_reads','detail_cursor','detail_mode',
                                      'ppu_sample_phase','prepared','clock_attempted'))):
            fields[block] = [field('profile.c:'+name,name) for name in names]
        fields['gap_counts'] = [field('audio.c:'+name,name) for name in ('gap_total','gap_count')]
        gap=records['audio.c:gaps'];assert gap['size']==512
        fields['gap_records'] = [dict(name='records',address=gap['address'],bytes=512,encoding='hex',labels=None)]
    blocks = []
    for name, entries in fields.items():
        start = min(f['address'] for f in entries)
        end = max(f['address']+f['bytes'] for f in entries)
        assert 0x80000000 <= start < end <= 0x84000000 and end-start <= 512
        blocks.append(dict(name=name, address=start, address_hex=f'0x{start:08X}',
                           bytes=end-start, minimum_endpoint=name in ('app', 'audio', 'jit'), fields=entries))
    signatures = []
    functions=(('h1_app_main',96),('gba_audio_write',64),
               ('h1_profile_frame' if metadata['profile'] else 'h1_nes_hle_wait',64))
    for name, length in functions:
        symbol = records[name];address=symbol['address'];assert symbol['size'] >= length
        for start, data, flags, section_name in allocated_sections:
            if start <= address and address+length <= start+len(data):
                assert flags & 4 and not flags & 1
                value=data[address-start:address-start+length]
                signatures.append(dict(name=name, address=address, address_hex=f'0x{address:08X}',
                                       bytes=length, expected_hex=value.hex(), sha256=digest(value)))
                break
        else:
            raise AssertionError(('function outside immutable code', name))
    absent = [name for name in ('h1_profile_now', 'h1_profile_detail_mask', 'profile.c:frequency',
              'profile.c:pc_fault', 'audio.c:gaps', 'audio.c:diag_retired') if name not in records]
    if not metadata['profile']:
        assert len(absent) == 6, ('Unexpected profile instrumented image', absent)
    limits=LIMITS if not metadata['profile'] else [line for line in LIMITS if not line.startswith('Ordinary H1_PROFILE=0')]+[
        'This is the old 0.1.12 profile build: fine timing is invalid after profile fault or pc_fault; new diagnostic clock validation does not validate this old clock algorithm.',
        'The 16-entry gap ring overwrites older records and resets gap_count on menu dump; gap_total remains cumulative.',
        'Profile detail modes rotate with different denominators; active reads can observe a partially updated 64-bit field.',
    ]
    return dict(schema=1, title=metadata['title'], version=metadata['version'],
                bda=str(bda_path.resolve()), elf=str(elf_path.resolve()),
                bda_sha256=metadata['sha256'], elf_sha256=digest(elf_path.read_bytes()),
                payload_sha256=metadata['payload_sha256'], profile=metadata['profile'],
                signatures=signatures, signature_scope='Three immutable code slices support build identification; not a whole-image live hash or proof against arbitrary modified code.',
                blocks=blocks, absent=absent, limits=limits)


def verify_signatures(plan, reads):
    """reads maps each signature name to bytes already downloaded elsewhere."""
    results = []
    for item in plan['signatures']:
        value = bytes(reads[item['name']])
        results.append(dict(name=item['name'], address=item['address'], bytes=len(value),
                            expected_sha256=item['sha256'], observed_sha256=digest(value),
                            equal=value.hex() == item['expected_hex']))
    return dict(ok=all(r['equal'] for r in results), blocks=results)


def decode(plan, capture):
    """Capture reads: [{name,address,hex,host_begin,host_end}], times monotonic."""
    blocks = {item['name']: item for item in plan['blocks']}
    result = dict(schema=1, bda_sha256=plan['bda_sha256'], reads={}, limits=plan['limits'],
                  signature_verified=capture.get('signature_verified', False),
                  status=capture.get('status'), resident_image_id=capture.get('resident_image_id'))
    for item in capture['reads']:
        block = blocks[item['name']]
        address = int(item['address'], 0) if isinstance(item['address'], str) else item['address']
        data = bytes.fromhex(item['hex'])
        assert address == block['address'] and len(data) == block['bytes'], item['name']
        assert item['name'] not in result['reads'], 'duplicate read block'
        values = {}
        for field in block['fields']:
            start = field['address']-address
            value = data[start:start+field['bytes']]
            if field['encoding'] == 'hex': value=value.hex()
            elif field['encoding'] == 'u32_array':
                value=dict(zip(field['labels'], struct.unpack('<'+'I'*len(field['labels']), value)))
            else: value=int.from_bytes(value, 'little')
            values[field['name']] = value
        begin, end = item.get('host_begin'), item.get('host_end')
        if begin is not None or end is not None:
            assert isinstance(begin, (int,float)) and isinstance(end, (int,float))
            assert math.isfinite(begin) and math.isfinite(end) and end >= begin
        result['reads'][item['name']] = dict(values=values, host_begin=begin, host_end=end)
    assert all(block['name'] in result['reads'] for block in plan['blocks'] if block['minimum_endpoint'])
    return result


def compare(first, second):
    assert first['bda_sha256'] == second['bda_sha256'], 'different builds'
    issues = []
    if not first.get('signature_verified') or not second.get('signature_verified'):
        issues.append('loaded code signature was not verified at both endpoints')
    if not first.get('resident_image_id') or first.get('resident_image_id') != second.get('resident_image_id'):
        issues.append('resident identity absent or changed')
    status_delta = None
    statuses = [first.get('status'), second.get('status')]
    status_fields = ('version', 'state', 'usb_configured', 'usb_fault', 'usb_resets',
                     'bad_frames', 'tick', 'poll_count', 'resident_base', 'resident_size',
                     'keys_low', 'keys_high', 'exec_state', 'last_error')
    if not all(s and all(name in s for name in status_fields) for s in statuses):
        issues.append('resident STATUS missing required health or timing fields')
    else:
        sa,sb=statuses
        for index,s in enumerate(statuses):
            if (s['version'] != 1 or s['state'] != 1 or s['usb_configured'] != 1 or
                    s['usb_fault'] or s['last_error'] or s['keys_low'] or s['keys_high'] or
                    s['exec_state'] in (3,4)):
                issues.append(f'endpoint {index} has a non-idle or unhealthy resident')
        for name in ('resident_base','resident_size','usb_resets','bad_frames'):
            if sa[name] != sb[name]: issues.append(f'STATUS.{name} changed across interval')
        status_delta={name:(sb[name]-sa[name])&0xffffffff for name in ('tick','poll_count')}
    for name in ('cartridge','session'):
        if name not in first['reads'] or name not in second['reads']:
            issues.append(f'{name} context missing at an endpoint')
            continue
        aa,bb=first['reads'][name]['values'],second['reads'][name]['values']
        if name=='cartridge':
            if aa != bb: issues.append('cartridge identity changed across interval')
        else:
            for index,state in enumerate((aa,bb)):
                if not state['loaded'] or any(state[k] for k in ('pause_requested','exit_requested','menu_requested','input_mask')):
                    issues.append(f'endpoint {index} has unloaded, paused or active-input session state')
            if aa['scale'] != bb['scale'] or aa['configured_skip'] != bb['configured_skip']:
                issues.append('display settings changed across interval')
    a,b=first['reads']['app'],second['reads']['app']
    elapsed = None
    if all(x is not None for x in (a['host_begin'],a['host_end'],b['host_begin'],b['host_end'])):
        low,high=b['host_begin']-a['host_end'],b['host_end']-a['host_begin']
        if low>0: elapsed=dict(midpoint=(low+high)/2, interval=[low,high])
    if not elapsed: issues.append('missing or overlapping host request time brackets')
    counters = {}
    counter_names = {'app': ('total_frames','videos','video_errors','audio_catchup_frames'),
                    'audio': ('batches','submitted','dropped','failures','underruns','nonzero','storage_pauses','storage_ticks')}
    for block,names in counter_names.items():
        before,after=first['reads'][block]['values'],second['reads'][block]['values']
        counters[block]={name:(after[name]-before[name])&0xffffffff for name in names}
        for name in names:
            if after[name]<before[name]: issues.append(f'{block}.{name} decreased: reset or counter wrap needs explanation')
    jit={}
    for name,labels in COUNTER_ARRAYS.items():
        if name not in first['reads']['jit']['values'] and name not in second['reads']['jit']['values']:
            continue
        aa,bb=first['reads']['jit']['values'][name],second['reads']['jit']['values'][name]
        jit[name]={key:dict(before=aa[key],after=bb[key]) if key=='max_words' else (bb[key]-aa[key])&0xffffffff for key in labels}
        for key in labels:
            if key != 'max_words' and bb[key] < aa[key]:
                issues.append(f'{name}.{key} decreased: reset or counter wrap needs explanation')
    counters['jit']=jit
    jit_values=[x['reads']['jit']['values'] for x in (first,second)]
    for name in ('cache','cache_bytes','h1_hle_sgz2_enabled'):
        if jit_values[0].get(name) != jit_values[1].get(name): issues.append(f'JIT {name} changed across interval')
    for index,point in enumerate((first,second)):
        sound=point['reads']['audio']['values']
        if not sound['active'] or sound['paused'] or sound['storage_paused']:
            issues.append(f'endpoint {index} audio is inactive or paused')
    fps=None
    if elapsed and not issues:
        n=counters['app']['total_frames'];shown=counters['app']['videos'];low,high=elapsed['interval']
        fps=dict(emulated=n/elapsed['midpoint'],displayed=shown/elapsed['midpoint'],
                 emulated_interval=[n/high,n/low],displayed_interval=[shown/high,shown/low])
    return dict(valid=not issues,issues=issues,host_elapsed=elapsed,fps=fps,counter_deltas=counters,
                status_delta_raw=status_delta,
                cached_endpoints={name:[p['reads'][name]['values'] for p in (first,second)] for name in ('audio','jit')},
                limits=first.get('limits',LIMITS))


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('manifest');p.add_argument('--bda',type=Path,default=ROOT/'build/release-jit/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/release-jit/H1FC.elf');p.add_argument('--output',type=Path,required=True)
    p=commands.add_parser('decode');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--capture',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=commands.add_parser('compare');p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();load=lambda path:json.loads(path.read_text(encoding='utf-8'))
    if args.command=='manifest': result=manifest(args.bda,args.elf)
    elif args.command=='decode': result=decode(load(args.manifest),load(args.capture))
    else: result=compare(load(args.before),load(args.after))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(dict(output=str(args.output),command=args.command)))


if __name__=='__main__':main()
