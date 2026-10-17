"""Read-only JIT eligibility census of actual MIPS FCEUmm execution.

No guest instructions, ROM bytes or state are patched. This is an opcode
boundary census, not a JIT implementation or a performance prediction.
"""
import argparse
import hashlib
import json
from pathlib import Path
from core_equivalence import execute, ROOT


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rom',type=Path,required=True)
    p.add_argument('--frames',type=int,default=32)
    p.add_argument('--nm',type=Path,required=True)
    a=p.parse_args()
    rom=a.rom.read_bytes()
    data=execute(ROOT/'dist/H1FC.bda',ROOT/'build/release/H1FC.elf',a.nm,rom,a.frames,0,cpu_census=True)
    census=data['cpu_census']
    simple={0xa9,0xa2,0xa0,0xaa,0xa8,0x8a,0x98,0xba,0x9a,0xe8,0xc8,0xca,0x88,
            0x29,0x09,0x49,0xc9,0xe0,0xc0,0x18,0x38,0xd8,0xb8,0xea,0x0a,0x4a,0x2a,0x6a}
    simple_count=sum(count for opcode,count in census['opcodes'].items() if int(opcode,16) in simple)
    report={'kind':'Actual MIPS opcode-boundary census; no JIT enabled',
        'rom_sha256':hashlib.sha256(rom).hexdigest(),'frames':a.frames,'bda_sha256':data['bda_sha256'],
        'host_pc_store_site':census['host_pc_store_site'],'boundary_samples':census['total'],
        'readonly_cartbr_samples':census['pure_rom'],'simple_first_stage_samples':simple_count,
        'distinct_pc_bank_views':len(census['views']),
        'top_opcodes':sorted(census['opcodes'].items(),key=lambda item:item[1],reverse=True)[:20],
        'top_pc_bank_views':sorted(census['views'].items(),key=lambda item:item[1],reverse=True)[:20],
        'other_pc_store_sites':census['other_pc_store_sites'],
        'limitations':['Only the supplied intro scene, not the whole game.',
                       'Simple-opcode frequency is not a predicted speedup; APU/Mapper hooks still run per instruction.',
                       'The dominant PC store site must be checked against the built disassembly.']}
    out=ROOT/'build/verification/jit-census-sgz2.json'
    out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
