"""Actual MIPS stack/subroutine lowering against the pinned interpreter.

Only test cartridge bytes, initial CPU/APU state and test bus handlers change.
The product CPU bodies and generated instructions execute unmodified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from jit_smoke import Core, ROOT
from jit_event_smoke import snapshot, put, links
from make_test_rom import make_rom


def clock(c, frame=1000000, dmc=1000000, dma=0, mapper=False, irq=0):
    put(c, 'fhcnt', 'h1_apu_fhcnt', frame)
    put(c, 'DMCacc', 'h1_apu_dmcacc', dmc)
    put(c, 'DMCSize', 'h1_apu_dmcsize', dma)
    put(c, 'DMCHaveDMA', 'h1_apu_have_dma', 0, 1)
    if mapper:
        put(c, 'IRQa', 'h1_namco_irq_enabled', bool(irq), 1)
        put(c, 'IRQCount', 'h1_namco_irq_count', irq, 2)


def compile_block(c, pc, code):
    c.prg(pc, code)
    page = struct.unpack('<I', c.m.read(c.s['Page'] + (pc >> 11) * 4, 4))[0]
    c.call(c.s['h1_nes_jit_compile'], pc, code[0], page, 1)
    table = struct.unpack('<I', c.m.read(c.s['h1_jit_entries'], 4))[0]
    run = struct.unpack('<I', c.m.read(table + pc * 16, 4))[0]
    assert run, (hex(pc), code.hex())
    return run


def compare(old, new, pc, code, regs, ram, cycles, label, carry=0):
    ram = bytes(ram)
    first = old.run(pc, code, regs, ram, cycles, tcount=carry)
    second = new.run(pc, code, regs, ram, cycles, tcount=carry)
    assert first == second, (label, first[:28].hex(), second[:28].hex())
    one, two = snapshot(old), snapshot(new)
    assert one == two, (label, 'serialized', [(i, a, b) for i, (a, b) in
                                             enumerate(zip(one, two)) if a != b][:12])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--baseline-bda', type=Path, default=ROOT/'build/release/H1FC.bda')
    parser.add_argument('--baseline-elf', type=Path, default=ROOT/'build/release/H1FC.elf')
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    counts = {'boundaries': 0, 'events': 0, 'control_regions': 0,
              'chains': 0, 'opaque_order': 0}
    native_transitions = 0
    for mapper in (0, 19):
        rom = bytearray(make_rom(dmc=True))
        rom[6] = (rom[6] & 15) | ((mapper & 15) << 4)
        rom[7] = mapper & 0xf0
        old = Core(args.baseline_bda, args.baseline_elf, args.nm, bytes(rom))
        new = Core(args.bda, args.elf, args.nm, bytes(rom))
        cores = old, new
        # Check the actual emitter for each instruction, including every S
        # wrap, high/low return-address carry and CPU budget prefix.
        for opcode in (0x48, 0x68, 0x20, 0x60):
            observed_native = struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[3]
            for stack in (0, 1, 0x7f, 0xfe, 0xff):
                for pc in (0x9000, 0x9ffc, 0xfffc):
                    code = bytes((0xea, opcode)) + (b'\x00\x94' if opcode == 0x20 else b'\xea\xea')
                    for budget in (1, 6, 7, 10, 24):
                        regs = [0x81, 0x25, 0x5a, stack, 0xa5, 0x21]
                        ram = bytearray((i * 37 + 11) & 255 for i in range(2048))
                        # RTS must wrap 0xffff+1 to zero; a larger budget is
                        # deliberately avoided because it would execute RAM.
                        ret = 0xffff if budget <= 24 else 0x93ff
                        ram[0x100 + ((stack+1) & 255)] = ret & 255
                        ram[0x100 + ((stack+2) & 255)] = ret >> 8
                        for c in cores:
                            clock(c, mapper=mapper == 19)
                            c.prg(0x9400, b'\xea\xea\xea\xea\x4c\x00\x94')
                        compile_block(new, pc, code)
                        compare(old, new, pc, code, regs, ram, budget,
                                (mapper, opcode, stack, pc, budget))
                        counts['boundaries'] += 1
            assert struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[3] > observed_native, (
                'No native opcode execution', opcode)
        # Place an APU/DMC/Mapper event before or at the new instruction;
        # partial clock exits must use the original completion body once.
        for opcode in (0x48, 0x68, 0x20, 0x60):
            code = bytes((0xea, opcode)) + (b'\x00\x94' if opcode == 0x20 else b'\xea\xea')
            for frame, dmc, dma in ((95,100,0), (96,100,0), (97,100,0),
                                     (100000,1,0), (100000,2,1), (100000,7,1),
                                     (287,1,1), (288,6,1), (289,7,1)):
                for carry in (0, 2):
                    for c in cores:
                        clock(c, frame, dmc, dma, mapper == 19, 0x7ffd if mapper else 0)
                    regs = [0xa7,0,0,0xff,4,4]
                    ram = bytearray(2048); ram[0x100:0x102] = b'\xff\x93'
                    compile_block(new, 0x9000, code)
                    compare(old, new, 0x9000, code, regs, ram, 10,
                            (mapper, opcode, frame, dmc, dma, carry), carry)
                    counts['events'] += 1
        # Long pure prefixes exercise both the whole-run and partial-run
        # clocks when the final opcode changes PC. Include a target equal to
        # the linear continuation: only retired count can prove completion.
        for opcode in (0x20, 0x60, 0x4c):
            for value, status in ((0,0), (0x80,0xff), (0xff,0x45)):
                prefix = bytes((0xa9,value,0x48,0xa9,0x55,0x68,0xaa,0xe8))
                tail_length = 1 if opcode == 0x60 else 3
                target = 0x9000 + len(prefix) + tail_length
                code = prefix + bytes((opcode,))
                if opcode != 0x60:
                    code += struct.pack('<H', target)
                code += b'\xea\xea\xea\x4c\x00\x94'
                for budget in (7, 18, 24, 36, 54, 60, 72):
                    for frame in (193, 337, 961, 100000):
                        regs = [0x81,0,0,0, status, status]
                        ram = bytearray(2048)
                        ram[0x101:0x103] = struct.pack('<H', target-1)
                        for c in cores:
                            clock(c, frame, 100000, 0, mapper == 19)
                            c.prg(0x9400, b'\xea\x4c\x00\x94')
                        compile_block(new, 0x9000, code)
                        compare(old, new, 0x9000, code, regs, ram, budget,
                                (mapper, 'control region', opcode, value, status, budget, frame))
                        counts['control_regions'] += 1
        # JSR -> stack body -> RTS -> continuation all use the same native
        # O32 frame. Every block has a separately checked ROM mapping.
        caller = bytes.fromhex('ea 20 00 98')
        callee = bytes.fromhex('48 a9 55 68 60')
        continuation = bytes.fromhex('e8 86 10 ea 4c 00 90')
        for stack in (0, 1, 0xfe, 0xff):
            for c in cores:
                clock(c, mapper=mapper == 19)
                c.prg(0x9004, continuation); c.prg(0x9800, callee)
            for pc, code in ((0x9000, caller), (0x9800, callee), (0x9004, continuation)):
                compile_block(new, pc, code)
            before = links(new)
            compare(old, new, 0x9000, caller, [0x81,0,0,stack,4,4], bytes(2048),
                    1500, (mapper, 'chain', stack))
            assert links(new)-before >= 3, 'No JSR and RTS native transitions'
            counts['chains'] += 1
        # The target's Page changes between runs. Its stale L1 entry must
        # return safely so ordinary bank binding can recover the new version.
        for c in cores:
            slot = c.s['Page'] + (0x9800 >> 11) * 4
            page = struct.unpack('<I', c.m.read(slot, 4))[0]
            c.saved_page = page
            c.m.word(slot, page + 0x800)
            c.prg(0x9800, bytes.fromhex('48 a9 66 68 60'))
            clock(c, mapper=mapper == 19)
        compare(old, new, 0x9000, caller, [0x81,0,0,0xff,4,4], bytes(2048),
                1500, (mapper, 'return remapped target'))
        for c in cores:
            c.m.word(c.s['Page'] + (0x9800 >> 11) * 4, c.saved_page)
        counts['chains'] += 1
        # Force generic lowering with an unknown mapper callback. The hook
        # changes S before JSR; low/high operand callbacks observe exactly the
        # original order and high fetch sees the newly pushed return bytes.
        logs = [[], []]
        for index, c in enumerate(cores):
            c.saved_irq = struct.unpack('<I', c.m.read(c.s['MapIRQHook'], 4))[0]
            def observe(tag, core=c, log=logs[index]):
                cpu = core.m.read(core.s['X'], 28)
                log.append((tag, cpu[9], cpu[24], cpu[4:6].hex(),
                            core.m.read(core.s['RAM']+0x100, 256).hex()))
            def mapper_hook(cycles, *unused, core=c, observe=observe):
                if cycles == 6:
                    core.m.write(core.s['X']+9, b'\x00')
                    observe('clock')
                return 0
            def low(address, *unused, core=c, observe=observe):
                observe('low')
                return 0x34
            def high(address, *unused, core=c, observe=observe):
                observe('high')
                return 0x94
            c.m.word(c.s['MapIRQHook'], c.stub(0x80001b80, mapper_hook))
            c.m.word(c.s['ARead'] + 0x9002*4, c.stub(0x80001ba0, low))
            c.m.word(c.s['ARead'] + 0x9003*4, c.stub(0x80001bc0, high))
            clock(c, mapper=mapper == 19)
        code = bytes.fromhex('ea 20 00 00')
        compile_block(new, 0x9000, code)
        compare(old, new, 0x9000, code, [0x81,0,0,0xfe,4,4], bytes(2048),
                10, (mapper, 'opaque fetch/stack order'))
        assert logs[0] == logs[1], logs
        assert [r[0] for r in logs[0]] == ['clock','low','high'], logs
        assert logs[0][1][1] == 0 and logs[0][2][1] == 0xfe, logs
        for c in cores:
            c.m.word(c.s['MapIRQHook'], c.saved_irq)
            c.m.word(c.s['ARead']+0x9002*4, c.s['CartBR'])
            c.m.word(c.s['ARead']+0x9003*4, c.s['CartBR'])
        counts['opaque_order'] += 1
        native_transitions += links(new)
        old.close(); new.close()
    report = {'ok': True, 'bda_sha256': hashlib.sha256(args.bda.read_bytes()).hexdigest(),
              'cases': sum(counts.values()), 'groups': counts,
              'native_chain_transitions': native_transitions,
              'checks': ['PHA/PLA/JSR/RTS, S wrap, PC/return-address wrap, cycle prefixes',
                         'APU/DMC/Mapper deadlines, pending DMA and cycle carry',
                         'native JSR/RTS chain across ROM pages and target remapping',
                         'opaque mapper hook then JSR low-read/push/high-read order',
                         'exact CPU/RAM/timestamps and complete serialized state']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
