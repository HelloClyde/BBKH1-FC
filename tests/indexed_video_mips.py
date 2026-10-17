"""Compare fused MIPS LCD output with the previous RGB565 scaler.

The reference image is independently decoded in Python, then scaled by the
saved v0.1.4 binary. This tests the actual MIPS implementation and MXU path.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
from unicorn import UC_HOOK_BLOCK
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_SP, UC_MIPS_REG_RA
from h1_mock import Machine, ROOT
from pause_menu_smoke import symbol


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'build/audio-v015/v014.bda')
    p.add_argument('--baseline-map',type=Path,default=ROOT/'build/audio-v015/v014.map')
    a=p.parse_args()
    old=Machine(a.baseline_bda,path=None)
    new=Machine(ROOT/'dist/H1FC.bda',path=None)
    maps=[a.baseline_map,ROOT/'build/release/H1FC.map']
    instructions=[0,0]
    for i,m in enumerate((old,new)):
        def block(uc,address,size,user): instructions[user]+=size//4
        m.uc.hook_add(UC_HOOK_BLOCK,block,i)

    def call(m,map_path,name,*args):
        m.finished=False
        for register,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            m.uc.reg_write(register,value)
        sp=m.uc.reg_read(UC_MIPS_REG_SP)
        for i,value in enumerate(args[4:]): m.word(sp+16+i*4,value)
        m.uc.reg_write(UC_MIPS_REG_RA,0x80001ff0)
        m.uc.emu_start(symbol(name,map_path),0,timeout=30_000_000,count=100_000_000)
        assert m.finished,name

    # Warm the colour tables and MXU self-test outside the measured calls.
    call(old,maps[0],'h1_linear_init')
    call(new,maps[1],'h1_linear_init')
    rng=random.Random(4740)
    pixels=bytes(rng.randrange(256) for _ in range(264*240))
    old.write(0x83020000,bytes(264*240*2))
    call(old,maps[0],'scale_frame_rgb32',0x83200000,0x83020000,528,0,256,240,1)
    cases=[]
    for generation in range(2):
        palette=[rng.randrange(65536) for _ in range(1024)]
        new.write(0x83010000,struct.pack('<1024I',*palette))
        for height in (224,240):
            for mask in (63,255):
                emphasis=bytes((y+generation)&7 for y in range(240) for _ in range(264))
                rgb=[]
                for y in range(height):
                    e=emphasis[y*264]
                    base=256+(e<<6) if e else 0
                    rgb.extend(palette[base+(v&(63 if e else mask))] for v in pixels[y*264:(y+1)*264])
                old.write(0x83020000,struct.pack('<'+'H'*len(rgb),*rgb))
                new.write(0x83020000,pixels)
                new.write(0x83040000,emphasis)
                new.write(0x83000000,struct.pack('<7I',0x83020000,0x83040000,0x83010000,256,height,264,mask))
                for scale in range(4):
                    before=list(instructions)
                    call(old,maps[0],'scale_frame_rgb32',0x83200000,0x83020000,528,scale,256,height,1)
                    call(new,maps[1],'scale_nes_indexed_rgb32',0x83200000,0x83000000,scale,1)
                    expected=old.read(0x83200000,480*272*4)
                    actual=new.read(0x83200000,480*272*4)
                    assert actual==expected,(generation,height,mask,scale)
                    cases.append({'palette_generation':generation,'height':height,'mask':mask,'scale':scale,
                        'lcd_sha256':hashlib.sha256(actual).hexdigest(),
                        'old_scaler_block_instructions':instructions[0]-before[0],
                        'fused_scaler_block_instructions':instructions[1]-before[1]})
                    # The retained RGB565 fallback allocates its large table
                    # from the heap and must still match the old static path.
                    new.write(0x83060000,old.read(0x83020000,len(rgb)*2))
                    call(new,maps[1],'scale_frame_rgb32',0x83280000,0x83060000,528,scale,256,height,1)
                    assert new.read(0x83280000,480*272*4)==expected
                    assert len(new.allocations)==1
    call(new,maps[1],'h1_video_release')
    assert not new.allocations
    failed=Machine(ROOT/'dist/H1FC.bda',path=None)
    for address,callback in list(failed.callbacks.items()):
        if getattr(callback,'__name__','')=='alloc': failed.callbacks[address]=lambda *args:0
    failed.write(0x83060000,old.read(0x83020000,len(rgb)*2))
    call(old,maps[0],'scale_frame_rgb32',0x83200000,0x83020000,528,1,256,height,1)
    call(failed,maps[1],'scale_frame_rgb32',0x83280000,0x83060000,528,1,256,height,1)
    assert failed.read(0x83280000,480*272*4)==old.read(0x83200000,480*272*4)
    assert not failed.allocations
    report={'ok':True,'baseline_sha256':hashlib.sha256(a.baseline_bda.read_bytes()).hexdigest(),
        'bda_sha256':hashlib.sha256((ROOT/'dist/H1FC.bda').read_bytes()).hexdigest(),
        'cases':cases,'rgb565_heap_cache_released':True,'allocation_failure_fallback':True,
        'limitations':['Old scaler counts exclude the preceding libretro RGB565 conversion.',
                                    'Block instruction entries do not model physical H1 caches or timing.']}
    output=ROOT/'build/verification/indexed-video-mips.json'
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('PASS: 32 actual MIPS LCD comparisons, all scaling modes, emphasis, raw masks, changed palettes')


if __name__=='__main__': main()
