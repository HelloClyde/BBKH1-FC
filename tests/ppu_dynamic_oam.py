"""Actual MIPS adversarial OAM-update fixture, separate from game performance.

The original test ROM initializes the PPU, then continuously writes rotating
values to OAM through $2004. Its usual 13-cycle loop changes sprite bytes many times
per scanline and detects pathological row-index rebuild work.
"""
import argparse
import hashlib
import json
from pathlib import Path
from core_equivalence import execute
from make_test_rom import make_rom
from h1_mock import ROOT


def make_dynamic_rom():
    rom=bytearray(make_rom(sprites=True))
    polling=bytes((0x2c,2,0x20,0x10,0xfb))
    offset=rom.rfind(polling,16,16+16384)
    assert offset>rom.find(polling)>=16
    rom[offset:offset+5]=bytes((0x4c,0,0x9f,0xea,0xea))
    # Data wraps after 255 values while OAM addresses wrap after 256 writes:
    # unlike equal 256-value/address periods, this never settles on unchanged
    # bytes after the first complete OAM pass. TXA; STA $2004; INX; CPX #255;
    # BNE $9F00; LDX #0; JMP $9F00. No run-time game/host state patching.
    loop=bytes((0x8a,0x8d,4,0x20,0xe8,0xe0,255,0xd0,0xf7,0xa2,0,0x4c,0,0x9f))
    rom[16+0x1f00:16+0x1f00+len(loop)]=loop
    return bytes(rom)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,required=True)
    p.add_argument('--baseline-elf',type=Path,required=True)
    p.add_argument('--bda',type=Path,required=True)
    p.add_argument('--elf',type=Path,required=True)
    p.add_argument('--nm',type=Path,default=ROOT.parent/'.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    p.add_argument('--frames',type=int,default=32)
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/v0113-ppu-dynamic-oam.json')
    a=p.parse_args();rom=make_dynamic_rom()
    old=execute(a.baseline_bda,a.baseline_elf,a.nm,rom,a.frames,0)
    new=execute(a.bda,a.elf,a.nm,rom,a.frames,0)
    assert old['native_block_entries'] and new['native_block_entries']
    for x,y in zip(old['frames'],new['frames']):
        for key in ('pcm','pcm_frames','video','state','game_state'):
            assert x[key]==y[key],(key,x,y)
    steady_old=sum(x['block_instructions'] for x in old['frames'][8:])
    steady_new=sum(x['block_instructions'] for x in new['frames'][8:])
    report={'ok':True,'rom_sha256':hashlib.sha256(rom).hexdigest(),
            'frames':a.frames,'baseline':old,'current':new,
            'steady_frames':[9,a.frames], 'steady_baseline':steady_old,'steady_current':steady_new,
            'steady_ratio':steady_new/steady_old,
            'scope':'Test-owned ROM writes OAM continuously; all MIPS core bytes unchanged; full state/PCM/video exact',
            'limitation':'Adversarial regression fixture, not SGZ2 performance evidence or physical H1 cycle measurement.'}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('baseline','current')},indent=2))


if __name__=='__main__':main()
