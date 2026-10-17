"""Actual MIPS regression for an APU deadline crossing a dirty PPU scope.

An unknown CPU reader installs a PPU/input callback, followed exclusively by
C interpretation. The real PPU loop invokes the callback between CPU slices;
it changes DMC state without an API and, for input, immediately uninstalls
itself. Compare the complete frame against this artifact's original interpreter.
No core instructions, native code, or scheduler implementation are patched.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_smoke import Core, ROOT
from jit_event_smoke import put
from jit_contract_smoke import frame_packet
from make_test_rom import make_rom


def word(c, name):
    return struct.unpack('<I', c.m.read(c.s[name], 4))[0]


def signed_distance(later, earlier):
    return struct.unpack('<i', struct.pack('<I', (later - earlier) & 0xffffffff))[0]


def run_case(args, mapper, kind):
    rom = bytearray(make_rom(dmc=True))
    rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
    rom[7] = mapper & 0xf0
    old, new = [Core(args.bda, args.elf, args.nm, bytes(rom)) for _ in range(2)]
    old.call(old.s['h1_nes_jit_close'])
    old.arena = None
    traces = []
    packets = []
    for c in (old, new):
        trace = {'installs': [], 'first_hook': [], 'hook_calls': 0, 'dma_reads': []}
        traces.append(trace)
        cpu = bytearray(28)
        struct.pack_into('<H', cpu, 4, 0x9000)
        cpu[6:12] = bytes((0x35, 0xfe, 0x80, 0xf0, 4, 4))
        c.m.write(c.s['X'], bytes(cpu))
        c.m.write(c.s['RAM'], bytes(2048))
        c.prg(0x9000, bytes.fromhex('ad 00 50 4c 00 92'))
        c.prg(0x9200, bytes.fromhex('08 48 68 28 4c 00 92'))
        # Mode 13 leaves opcodes unchanged but disallows native admission.
        # Thus no native strict entry can refresh the dirty frame contract.
        c.m.write(c.s['encryptOpcodes'], b'\x0d')
        c.m.write(c.s['PPU'], b'\0\0')
        c.m.write(c.s['ppudead'], b'\0')
        for name in ('PPU_hook', 'InputScanlineHook', 'GameHBIRQHook', 'GameHBIRQHook2'):
            c.m.word(c.s[name], 0)
        c.m.word(c.s['timestamp'], 0)
        c.m.word(c.s['sound_timestamp'], 0)
        put(c, 'fhcnt', 'h1_apu_fhcnt', 100000000)
        put(c, 'DMCacc', 'h1_apu_dmcacc', 1000000)
        put(c, 'DMCSize', 'h1_apu_dmcsize', 0)
        put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
        slot = c.s['InputScanlineHook' if kind == 'input' else 'PPU_hook']

        def first_hook(*unused, core=c, log=trace, hook_slot=slot):
            log['hook_calls'] += 1
            if log['first_hook']:
                return 0
            stamp = word(core, 'timestamp')
            log['first_hook'].append({
                'timestamp': stamp,
                'deadline_distance': signed_distance(word(core, 'h1_cpu_apu_deadline'), stamp),
                'frame': word(core, 'h1_jit_contract_frame'),
                'dirty': word(core, 'h1_jit_contract_dirty'),
            })
            core.m.word(core.s['DMCAddress'], 0x4000)
            put(core, 'DMCacc', 'h1_apu_dmcacc', 1)
            put(core, 'DMCSize', 'h1_apu_dmcsize', 1)
            put(core, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
            if kind == 'input':
                # Checking only the hook pointer in the following CPU slice
                # cannot detect this one-shot callback's state mutation.
                core.m.word(hook_slot, 0)
            else:
                # The pre-render loop calls the pointer twice per iteration
                # without a second null check. Keep it valid for that loop,
                # but turn rendering off to bound later callback traffic.
                core.m.write(core.s['PPU'] + 1, b'\0')
            return 0

        callback = c.stub(0x80001bc0, first_hook)

        def install(address, *unused, core=c, log=trace, hook_slot=slot, pointer=callback):
            log['installs'].append({
                'timestamp': word(core, 'timestamp'),
                'frame': word(core, 'h1_jit_contract_frame'),
                'dirty': word(core, 'h1_jit_contract_dirty'),
            })
            core.m.word(hook_slot, pointer)
            if kind == 'ppu':
                core.m.write(core.s['PPU'] + 1, b'\x08')
            return 0

        def dma(address, *unused, core=c, log=trace):
            log['dma_reads'].append((address, word(core, 'timestamp')))
            return 0x80

        c.m.word(c.s['ARead'] + 0x5000 * 4, c.stub(0x80001ba0, install))
        c.m.word(c.s['ARead'] + 0xc000 * 4, c.stub(0x80001be0, dma))
        packets.append(frame_packet(c))

    differences = [i for i, (reference, candidate) in enumerate(zip(*packets))
                   if reference != candidate]
    native_ops = struct.unpack('<8I', new.m.read(new.s['h1_jit_stats'], 32))[3]
    checks = {
        'complete_cpu_ram_serialized_pcm_video_match': not differences,
        'both_deliver_pcm_and_video': all(packet[3] and packet[4] for packet in packets),
        'only_c_fallback_executed': native_ops == 0,
        'installer_was_in_admitted_dirty_scope': len(traces[1]['installs']) == 1 and
            traces[1]['installs'][0]['frame'] == traces[1]['installs'][0]['dirty'] == 1,
        'real_ppu_callback_executed': all(len(trace['first_hook']) == 1 for trace in traces),
        'deadline_expired_before_ppu_callback': bool(traces[1]['first_hook']) and
            traces[1]['first_hook'][0]['deadline_distance'] <= 0,
        'exact_four_dma_reads_at_same_time': len(traces[0]['dma_reads']) == 4 and
            traces[0]['dma_reads'] == traces[1]['dma_reads'],
    }
    result = {'mapper': mapper, 'hook': kind, 'ok': all(checks.values()),
              'checks': checks, 'packet_differences': differences,
              'reference_trace': traces[0], 'candidate_trace': traces[1],
              'pcm_frames': [sum(len(x) // 4 for x in packet[3]) for packet in packets]}
    old.close()
    new.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--mapper', type=int, action='append', choices=(0, 19))
    parser.add_argument('--hook', action='append', choices=('input', 'ppu'))
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'build/verification/jit-schedule-scope.json')
    args = parser.parse_args()
    cases = [run_case(args, mapper, kind) for mapper in args.mapper or (0, 19)
             for kind in args.hook or ('input', 'ppu')]
    report = {'ok': all(case['ok'] for case in cases),
              'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'reference': 'Same artifact with JIT closed; complete original-interpreter frame',
              'cases': cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not report['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
