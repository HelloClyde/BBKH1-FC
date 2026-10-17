"""Compare complete bounded apps, including startup, scaler, PCM and saves.

Runs actual BDA machine code; firmware services are mocked. Block instruction
entries are a structural-cost metric, not physical H1 cycles or frame rates.
Both inputs are H1_TEST_FRAMES=20 builds, which stop through the explicit test
frame bound; this benchmark does not send Escape or exercise the exit menu.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from unicorn import UC_HOOK_BLOCK
from h1_mock import Machine, ROOT
from make_test_rom import make_rom


def execute(bda,rom):
    path='A:\\FC\\BENCH.nes'
    from options_smoke import config_slot
    m=Machine(bda,files={path:rom,path+'.cfg.s0':config_slot(rom,0)},path=path)
    counts=[0]
    def block(uc,address,size,user):counts[0]+=size//4
    m.uc.hook_add(UC_HOOK_BLOCK,block)
    seconds=m.run()
    log=m.files['A:\\FC\\h1fc.log'].decode('gbk')
    frames=int(re.search(r'STOP video=(\d+) errors=0',log).group(1))
    assert 'APP_END result=0' in log
    save={p:hashlib.sha256(data[28:]).hexdigest() for p,data in m.files.items() if p.endswith(('.s0','.s1'))}
    native=re.search(r'JIT_CLOSE.*native_ops=(\d+)',log)
    return {'bda_sha256':hashlib.sha256(bda.read_bytes()).hexdigest(),
            'block_instructions':counts[0],'frames':frames,'seconds_host':seconds,
            'screen':hashlib.sha256(m.screen).hexdigest(),'save_payloads':save,
            'pcm':hashlib.sha256(b''.join(m.audio_submissions)).hexdigest(),
            'native_ops':int(native.group(1)) if native else 0,
            'log':log}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'build/test-20/H1FC.bda')
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-20-jit/H1FC.bda')
    p.add_argument('--rom',type=Path)
    p.add_argument('--max-instruction-ratio',type=float,
                   help='Optional structural-cost limit; no default assumption that native beats optimized C')
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-app-benchmark.json')
    a=p.parse_args();rom=a.rom.read_bytes() if a.rom else make_rom(sprites=True)
    old=execute(a.baseline_bda,rom);new=execute(a.bda,rom)
    assert old['frames']==new['frames']==20
    for key in ('screen','save_payloads','pcm'):assert old[key]==new[key],key
    assert new['native_ops']>0
    ratio=new['block_instructions']/old['block_instructions']
    if a.max_instruction_ratio is not None:
        assert ratio<=a.max_instruction_ratio,ratio
    report={'ok':True,'scope':'Complete actual MIPS app, including startup/frontend/PCM/save; firmware services mocked',
            'ratio':ratio,'rom_sha256':hashlib.sha256(rom).hexdigest(),'baseline':old,'current':new,
            'limitation':'Instruction entries and Unicorn host times do not establish physical H1 cycles or FPS.'}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('baseline','current')},indent=2))
    print(old['block_instructions'],new['block_instructions'])


if __name__=='__main__':main()
