"""Compare actual MIPS indexed scaling with v3 and an independent nearest oracle.

All calls include palette checks and LCD copies. Warm repeats isolate steady
state from map/palette construction; counts are executed block entries, not
physical H1 timings. Linear scaling retains v3 as its bit-exact reference.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct

from unicorn import UC_HOOK_BLOCK, UC_HOOK_MEM_READ
from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1,
    UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_RA)
from h1_mock import Machine, ROOT
from pause_menu_smoke import symbol


PIXELS=0x83020000
EMPHASIS=0x83060000
PALETTE=0x83010000
FRAME=0x83000000
OUTPUT=0x83200000
LCD_BYTES=480*272*4


class Scaler:
    def __init__(self,bda,map_path):
        self.machine=Machine(bda,path=None)
        self.sha256=hashlib.sha256(bda.read_bytes()).hexdigest()
        self.addresses={name:symbol(name,map_path) for name in
                        ('h1_linear_init','scale_nes_indexed_rgb32')}
        self.instructions=0
        self.machine.uc.hook_add(UC_HOOK_BLOCK,self.block)
        self.call('h1_linear_init')

    def block(self,uc,address,size,user):
        self.instructions+=size//4

    def call(self,name,*args):
        m=self.machine
        m.finished=False
        for register,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,
                                   UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            m.uc.reg_write(register,value)
        m.uc.reg_write(UC_MIPS_REG_RA,0x80001ff0)
        before=self.instructions
        m.uc.emu_start(self.addresses[name],0,timeout=30_000_000,count=100_000_000)
        assert m.finished,name
        return self.instructions-before


def nearest(pixels,emphasis,palette,width,height,pitch,crop,mask,mode,clear):
    w=480 if mode==2 else width*272//height if mode else width
    h=272 if mode else height
    ox=(480-w)//2
    oy=(272-h)//2
    result=bytearray(LCD_BYTES) if clear else bytearray(b'\xa5'*LCD_BYTES)
    colors=[((p&0xf800)<<8)|((p&0x07e0)<<5)|((p&31)<<3) for p in palette]
    for y in range(h):
        sy=y*height//h+crop
        e=emphasis[sy*pitch]&7
        base=256+(e<<6) if e else 0
        effective=63 if e else mask
        row=[colors[base+(pixels[sy*pitch+x*width//w]&effective)] for x in range(w)]
        start=((y+oy)*480+ox)*4
        result[start:start+w*4]=struct.pack('<'+'I'*w,*row)
    return bytes(result)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'build/jit-v4-baseline/release-jit/H1FC.bda')
    p.add_argument('--baseline-map',type=Path,default=ROOT/'build/jit-v4-baseline/release-jit/H1FC.map')
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-18-jit/H1FC.bda')
    p.add_argument('--map',type=Path,default=ROOT/'build/test-18-jit/H1FC.map')
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/indexed-scale-v4.json')
    p.add_argument('--nearest-only',action='store_true',help='Omit unchanged linear path for exploratory measurements')
    a=p.parse_args()
    old,new=Scaler(a.baseline_bda,a.baseline_map),Scaler(a.bda,a.map)
    rng=random.Random(474004)
    cases=[]
    geometries=[(256,224,264,8),(256,240,256,0),(248,224,256,8),
                (255,240,263,0),(257,240,265,0),(320,272,320,0),
                (256,200,264,3),(256,272,256,0)]
    # Alternate geometry and palette state, with high emphasis bits and
    # nonzero crop offsets. A second call checks cached maps and palettes.
    for generation in range(2):
        palette=[rng.randrange(65536) for _ in range(768)]
        for width,height,pitch,crop in geometries:
            pixels=bytes(rng.randrange(256) for _ in range(pitch*(height+crop)))
            emphasis=bytes((0 if generation==0 else (y*13+0x80))&255
                           for y in range(height+crop) for _ in range(pitch))
            for mask in (63,255):
                for mode in range(3 if a.nearest_only else 4):
                    # The linear kernel is unchanged. Exercise both usual
                    # heights and its generic-width route with changed
                    # palettes, both masks, emphasis and clear/no-clear,
                    # without repeating every nearest-only geometry there.
                    if mode==3 and ((width,height) not in ((256,224),(256,240),(248,224))
                                    or mask!=(63 if generation==0 else 255)):
                        continue
                    clear=(mode+generation)&1
                    for scaler in (old,new):
                        m=scaler.machine
                        m.write(PIXELS,pixels)
                        m.write(EMPHASIS,emphasis)
                        m.write(PALETTE,struct.pack('<768I',*palette))
                        m.write(FRAME,struct.pack('<7I',PIXELS+crop*pitch,EMPHASIS+crop*pitch,
                            PALETTE,width,height,pitch,mask))
                    expected=None if mode==3 else nearest(pixels,emphasis,palette,
                        width,height,pitch,crop,mask,mode,clear)
                    measurements=[]
                    for repeat in range(2):
                        count=[]
                        images=[]
                        for scaler in (old,new):
                            scaler.machine.write(OUTPUT-16,b'\x5a'*16+b'\xa5'*LCD_BYTES+b'\x5a'*16)
                            count.append(scaler.call('scale_nes_indexed_rgb32',OUTPUT,FRAME,mode,clear))
                            images.append(scaler.machine.read(OUTPUT,LCD_BYTES))
                            assert scaler.machine.read(OUTPUT-16,16)==b'\x5a'*16
                            assert scaler.machine.read(OUTPUT+LCD_BYTES,16)==b'\x5a'*16
                        key=(generation,width,height,pitch,crop,mask,mode,clear,repeat)
                        assert images[0]==images[1],('v3 comparison',key)
                        if expected is not None: assert images[1]==expected,('nearest oracle',key)
                        measurements.append({'baseline':count[0],'candidate':count[1],
                            'reduction_percent':100*(1-count[1]/count[0])})
                    cases.append({'generation':generation,'width':width,'height':height,
                        'pitch':pitch,'crop':crop,'mask':mask,'mode':mode,'clear':clear,
                        'cold':measurements[0],'warm':measurements[1],
                        'lcd_sha256':hashlib.sha256(images[1]).hexdigest()})
                    print(f'PASS {len(cases):03d}: {width}x{height} mode={mode} mask={mask} '
                          f'warm={measurements[1]["reduction_percent"]:.2f}%',flush=True)
    # Read bounds for the expanded-row path: each indexed source access must
    # stay inside an actual pixel, including the very last 256th pixel.
    # Use a fresh VM: Unicorn's previously translated memory accesses can
    # retain partial range-hook instrumentation after many scaling calls.
    guard=Scaler(a.bda,a.map)
    guard.machine.write(PALETTE,struct.pack('<768I',*palette))
    guarded=[]
    for height in (224,240):
        pitch=256
        pixels=bytes(rng.randrange(256) for _ in range(pitch*height))
        emphasis=bytes([0x87])*len(pixels)
        m=guard.machine
        m.write(PIXELS,pixels)
        m.write(EMPHASIS,emphasis)
        m.write(FRAME,struct.pack('<7I',PIXELS,EMPHASIS,PALETTE,256,height,pitch,255))
        for mode in (0,1,2):
            reads=[]
            accesses=[0]
            # Unicorn memory hooks expose the translated physical address
            # for MIPS kseg0, unlike instruction hooks' virtual addresses.
            base=PIXELS&0x1fffffff
            def check_read(uc,access,address,size,value,user):
                accesses[0]+=1
                if address<base or address+size>base+len(pixels):
                    reads.append((address,size))
            hook=m.uc.hook_add(UC_HOOK_MEM_READ,check_read,begin=base-16,
                              end=base+len(pixels)+16)
            m.write(OUTPUT,b'\xa5'*LCD_BYTES)
            guard.call('scale_nes_indexed_rgb32',OUTPUT,FRAME,mode,0)
            m.uc.hook_del(hook)
            assert not reads,(height,mode,reads)
            assert accesses[0]>0,(height,mode,accesses[0])
            assert m.read(OUTPUT,LCD_BYTES)==nearest(pixels,emphasis,palette,
                256,height,pitch,0,255,mode,0)
            guarded.append({'height':height,'mode':mode,'source_reads_in_bounds':True,
                            'source_read_count':accesses[0]})
            # Repeat with cached rows and a fresh LCD. The equality check
            # reads words, so an exact byte-load count would reject valid
            # implementations. Assert actual bounds and oracle output.
            reads.clear();accesses[0]=0
            hook=m.uc.hook_add(UC_HOOK_MEM_READ,check_read,begin=base-16,
                              end=base+len(pixels)+16)
            m.uc.ctl_flush_tb()
            m.write(OUTPUT,b'\xa5'*LCD_BYTES)
            guard.call('scale_nes_indexed_rgb32',OUTPUT,FRAME,mode,0)
            m.uc.hook_del(hook)
            assert not reads,(height,mode,'warm',reads)
            assert accesses[0]>0,(height,mode,'warm',accesses[0])
            assert m.read(OUTPUT,LCD_BYTES)==nearest(pixels,emphasis,palette,
                256,height,pitch,0,255,mode,0)
            guarded.append({'height':height,'mode':mode,'warm':True,
                            'source_reads_in_bounds':True,'source_read_count':accesses[0]})
    report={'ok':True,'baseline_sha256':old.sha256,'bda_sha256':new.sha256,
        'cases':cases,'source_bounds':guarded,
        'limitations':['MIPS block instruction entries do not model physical H1 cache or timing.',
                      'Linear mode uses the unmodified v3 scaler as its image oracle.']}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(f'PASS: {len(cases)} cases, each cold/warm, plus {len(guarded)} source bounds checks')


if __name__=='__main__': main()
