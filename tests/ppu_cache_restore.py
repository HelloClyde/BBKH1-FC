"""Actual MIPS PPU cache lifetime through OAM register writes and state restore.

The synthetic ROM is test owned. Register writes deliberately replace sprite
positions and 8/16-pixel height between frames; this is a correctness fixture,
not a performance workload. Both normal BDAs execute unchanged core code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from jit_smoke import Core
from make_test_rom import make_rom
from h1_mock import ROOT


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,required=True)
    p.add_argument('--baseline-elf',type=Path,required=True)
    p.add_argument('--bda',type=Path,required=True)
    p.add_argument('--elf',type=Path,required=True)
    p.add_argument('--nm',type=Path,default=ROOT.parent/'.tools/toolchain/bin/mipsel-none-elf-nm.exe')
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/v0113-ppu-cache-restore.json')
    a=p.parse_args()
    rom=bytearray(make_rom(sprites=True,dmc=True,dmc_irq=True))
    # This fixture normally writes PPUCTRL=0 each frame. Retain its initial
    # initialization but omit the later test-ROM write, so our real register
    # writes can exercise both sprite heights across complete frame calls.
    reset_control=bytes((0xa9,0,0x8d,0,0x20))
    offset=rom.rfind(reset_control)
    assert offset>rom.find(reset_control)>=16
    rom[offset:offset+len(reset_control)]=bytes((0xea,))*len(reset_control)
    rom=bytes(rom)
    old=Core(a.baseline_bda,a.baseline_elf,a.nm,rom)
    new=Core(a.bda,a.elf,a.nm,rom)
    outputs={id(c):{'audio':b'','video':b''} for c in (old,new)}
    for c in (old,new):
        out=outputs[id(c)]
        def audio(data,count,*unused,c=c,out=out):
            out['audio']+=c.m.read(data,count*4)
            return count
        def video(data,width,height,pitch,*unused,c=c,out=out):
            out['video']=b''.join(c.m.read(data+y*pitch,width*2) for y in range(height)) if data else b''
            return 0
        c.stub(0x80001b20,audio);c.stub(0x80001b00,video)
    records=[]
    def frame(label,skip):
        observed=[]
        for c in (old,new):
            out=outputs[id(c)];out['audio']=out['video']=b''
            c.call(c.s['h1_nes_set_frameskip'],skip)
            c.call(c.core[10])
            size=c.call(c.core[16]);assert 0<size<1000000
            assert c.call(c.core[17],0x83200000,size)==1
            observed.append({'pcm':digest(out['audio']),'video':digest(out['video']),
                             'state':digest(c.m.read(0x83200000,size)),
                             'oam':digest(c.m.read(c.s['SPRAM'],256))})
        assert observed[0]==observed[1],(label,observed)
        records.append({'label':label,'skip':skip,**observed[0]})
    for i in range(5):frame(f'initial-{i}',0)
    snapshots=[]
    for c in (old,new):
        size=c.call(c.core[16]);assert c.call(c.core[17],0x83200000,size)==1
        snapshots.append(c.m.read(0x83200000,size))
    assert snapshots[0]==snapshots[1]
    restores=[]
    for round_index,(height,skip) in enumerate(((16,0),(8,1),(16,2),(8,0))):
        for c in (old,new):
            control=c.m.read(c.s['PPU'],1)[0]
            control=(control&~32)|(32 if height==16 else 0)
            c.call(c.s['B2000'],0x2000,control)
            c.call(c.s['B2003'],0x2003,0)
            for sprite in range(64):
                for value in ((17+sprite*3+round_index)&255,3,(sprite*33)&255,(250+sprite)&255):
                    c.call(c.s['B2004'],0x2004,value)
        for i in range(3):frame(f'changed-{round_index}-{i}',skip)
        assert struct.unpack('<I',new.m.read(new.s['h1_sprite_rows_valid'],4))[0]==1
        assert struct.unpack('<I',new.m.read(new.s['h1_sprite_height'],4))[0]==height
        before=new.m.read(new.s['h1_sprite_oam'],256)
        for c,snapshot in zip((old,new),snapshots):
            c.m.write(0x83200000,snapshot)
            assert c.call(c.core[18],0x83200000,len(snapshot))==1
        restored=new.m.read(new.s['SPRAM'],256)
        assert before!=restored,'Fixture must invalidate a genuinely changed OAM index'
        # State data restores emulated OAM, while the derived cache is still old.
        assert new.m.read(new.s['h1_sprite_oam'],256)==before
        for i in range(3):frame(f'restored-{round_index}-{i}',skip)
        assert new.m.read(new.s['h1_sprite_oam'],256)==new.m.read(new.s['SPRAM'],256)
        assert struct.unpack('<I',new.m.read(new.s['h1_sprite_height'],4))[0]==8
        restores.append({'height_before':height,'skip':skip,'old_index_detected':True,'revalidated':True})
    old.close();new.close()
    report={'ok':True,'baseline_bda_sha256':digest(a.baseline_bda.read_bytes()),
            'bda_sha256':digest(a.bda.read_bytes()),'frames':len(records),
            'restores':restores,'records':records,
            'scope':'Actual MIPS normal BDAs; exact full serialize, PCM, video and OAM after real PPU register writes, height changes, skip changes and state restore',
            'limitation':'Correctness fixture; test-owned register mutations are excluded from performance evidence.'}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='records'},indent=2))


if __name__=='__main__':main()
