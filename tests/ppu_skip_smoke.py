"""Verify actual MIPS sprite-zero/overflow behavior with and without output."""
import argparse
import hashlib
import json
from pathlib import Path
from core_equivalence import execute, ROOT
from make_test_rom import make_rom


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--nm',type=Path,default=ROOT/'.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    a=p.parse_args()
    bda=ROOT/'dist/H1FC.bda'
    elf=ROOT/'build/release/H1FC.elf'
    rom=make_rom(sprites=True)
    reference=execute(bda,elf,a.nm,rom,24,0)
    assert any((f['save_probe_status']&0x60)==0x60 for f in reference['frames'])
    cases=[]
    for skip in (2,5):
        current=execute(bda,elf,a.nm,rom,24,skip)
        for old,new in zip(reference['frames'],current['frames']):
            for key in ('pcm','state','game_state','save_probe_status'):
                assert old[key]==new[key],(skip,new['frame'],key)
            if new['video']: assert old['video']==new['video'],(skip,new['frame'],'video')
        cases.append({'skip':skip,'exact_pcm_cpu_ppu_save':True,
                      'displayed_frames':sum(bool(f['video']) for f in current['frames']),
                      'block_instructions':current['block_instructions']})
    report={'ok':True,'bda_sha256':hashlib.sha256(bda.read_bytes()).hexdigest(),
            'fixture_sha256':hashlib.sha256(rom).hexdigest(),'frames':24,
            'sprite_zero_hit_and_overflow_exercised':True,
            'no_skip_block_instructions':reference['block_instructions'],'cases':cases,
            'limitation':'This fixture does not cover every mapper or mid-scanline hardware edge case.'}
    (ROOT/'build/verification/ppu-skip-smoke.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
