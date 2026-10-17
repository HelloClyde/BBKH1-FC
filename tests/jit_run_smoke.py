"""MIPS event-free run admission and exact boundary restoration differential."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
from jit_smoke import Core, ROOT
from jit_event_smoke import snapshot, put
from make_test_rom import make_rom


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-17-jit/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/test-17-jit/H1FC.elf')
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'dist/H1FC.bda')
    p.add_argument('--baseline-elf',type=Path,default=ROOT/'build/release/H1FC.elf')
    p.add_argument('--nm',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-runs.json')
    a=p.parse_args();cases=0;totals=[0,0,0,0];rng=random.Random(65024)
    programs=[
        bytes.fromhex('a9 80 aa e8 85 10 58 78 ea 4c 00 90'),
        bytes.fromhex('ad 00 10 09 01 8d ff 07 e6 12 ce ff 07 a2 04 ea 4c 00 90'),
        # The backward edge enters the middle of the original decode window.
        bytes.fromhex('a2 03 a9 01 e8 ea ca d0 fa 4c 00 90'),
        bytes.fromhex('58 78 58 4c 00 90'),
        bytes.fromhex('78 58 78 4c 00 90'),
        # An observable PPU status read separates two regions; the backward
        # branch enters the second region and must repeat its event proof.
        bytes.fromhex('a9 01 aa e8 ad 02 20 a9 80 aa e8 85 10 d0 f8 4c 00 90'),
    ]
    for mapper in (0,19):
        rom=bytearray(make_rom(dmc=True));rom[6]=(rom[6]&15)|((mapper&15)<<4);rom[7]=mapper&0xf0
        old=Core(a.baseline_bda,a.baseline_elf,a.nm,bytes(rom))
        new=Core(a.bda,a.elf,a.nm,bytes(rom))
        def compare(code,ppu_cycles,frame=1000000,dmc=1000000,dma=0,carry=0,
                    irq=0,namco=0,enabled=0,overclock=0):
            nonlocal cases
            regs=[rng.randrange(256) for _ in range(6)]
            # Reset sources identically; avoid enabling an unrelated DMC IRQ
            # while testing mapper admission. APU DMA cases still use real reads.
            ram=bytes(rng.randrange(256) for _ in range(2048))
            for c in (old,new):
                put(c,'fhcnt','h1_apu_fhcnt',frame)
                put(c,'DMCacc','h1_apu_dmcacc',dmc)
                put(c,'DMCSize','h1_apu_dmcsize',1 if dma else 0)
                put(c,'DMCHaveDMA','h1_apu_have_dma',dma==2,1)
                c.m.word(c.s['overclocked'],overclock)
                c.m.word(c.s['timestamp'],1000)
                c.m.word(c.s['sound_timestamp'],1000)
                # Exercise 32-bit timestamp wrap without involving audio
                # waveform producers, which index frame-local sample arrays.
                if overclock:
                    c.m.word(c.s['timestamp'],0xfffffff0)
                    c.m.word(c.s['sound_timestamp'],0xfffffff0)
                if mapper==19:
                    put(c,'IRQa','h1_namco_irq_enabled',enabled,1)
                    put(c,'IRQCount','h1_namco_irq_count',namco,2)
            one=old.run(0x9000,code,regs,ram,ppu_cycles,irq,tcount=carry)
            two=new.run(0x9000,code,regs,ram,ppu_cycles,irq,force=True,tcount=carry)
            label=(mapper,code.hex(),ppu_cycles,frame,dmc,dma,carry,irq,namco,enabled,overclock)
            assert one==two,(label,[(i,x,y) for i,(x,y) in enumerate(zip(one,two)) if x!=y][:16])
            first,second=snapshot(old),snapshot(new)
            assert first==second,(label,'serialized',[(i,x,y) for i,(x,y) in enumerate(zip(first,second)) if x!=y][:16])
            cases+=1
        for code in programs:
            for ppu in (1,6,7,12,13,18,19,24,25,30,31,48,96,192):
                compare(code,ppu)
        code=programs[0]
        # Deadlines on both sides of every instruction in a 7-op region,
        # with incoming residual cycles from a preceding branch.
        for carry in (0,1,2,3,255,256):
            for elapsed in (2,4,6,9,11,13,15):
                for delta in (-1,0,1):
                    compare(code,48,frame=(carry+elapsed)*48+delta,carry=carry)
                    compare(code,48,dmc=carry+elapsed+delta,carry=carry)
        for dma in (1,2):
            for frame,dmc in ((96,2),(1000000,1000000),(97,3)):
                compare(code,96,frame,dmc,dma)
        for irq in (1,0x40,0x80,0x100,0x200,0x400,0x800):
            for ppu in (1,18,96):compare(code,ppu,irq=irq)
        for carry in (0,2,256):
            for ppu in (1,18,96):compare(code,ppu,carry=carry,overclock=1)
        if mapper==19:
            for enabled in (0,1):
                for count in (0,0x7fef,0x7ff0,0x7ffc,0x7ffd,0x7ffe,0x7fff,0xfffe,0xffff):
                    for carry in (0,2):compare(code,96,namco=count,enabled=enabled,carry=carry)
        # Mutate a later absolute read handler after compilation. The complete
        # stable-entry contract must reject it before any deferred clock debt.
        code=bytes.fromhex('a9 01 aa ad 00 10 ea 4c 00 90')
        new.prg(0x9000,code)
        page=struct.unpack('<I',new.m.read(new.s['Page']+18*4,4))[0]
        new.call(new.s['h1_nes_jit_compile'],0x9000,code[0],page,1)
        saved=[]
        for c in (old,new):
            address=c.s['ARead']+0x1000*4
            saved.append(struct.unpack('<I',c.m.read(address,4))[0])
            c.m.word(address,c.stub(0x80001b80,lambda *args:0x42))
            put(c,'fhcnt','h1_apu_fhcnt',1000000)
            put(c,'DMCacc','h1_apu_dmcacc',1000000)
            put(c,'DMCSize','h1_apu_dmcsize',0)
            c.m.word(c.s['overclocked'],0)
            c.m.word(c.s['timestamp'],1000);c.m.word(c.s['sound_timestamp'],1000)
        regs=[1,2,3,0xf0,4,4];ram=bytes(2048)
        assert old.run(0x9000,code,regs,ram,96)==new.run(0x9000,code,regs,ram,96)
        assert snapshot(old)==snapshot(new),'later read handler mutation'
        assert struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[4]>0
        for c,handler in zip((old,new),saved):c.m.word(c.s['ARead']+0x1000*4,handler)
        cases+=1
        stats=struct.unpack('<4I',new.m.read(new.s['h1_jit_run_stats'],16))
        assert stats[2]>0 and stats[3]>0,('run admission paths unexercised',mapper,stats)
        totals=[x+y for x,y in zip(totals,stats)]
        old.close();new.close()
    report={'ok':True,'cases':cases,'bda_sha256':hashlib.sha256(a.bda.read_bytes()).hexdigest(),
            'run_counters':dict(zip(('compiled_regions','compiled_ops','executed_regions','rejected_regions'),totals)),
            'checks':['exact CPU including PC, DB, previous P; RAM; timestamps; full serialized state',
                      'count budget before/at/after instruction boundaries and final overshoot',
                      'APU/DMC deadlines at every region position, pending DMA and incoming carry',
                      'CLI/SEI, pending IRQ/NMI/NMI2/TEMP, and internal branch entry split',
                      'PPU status read region barrier and later read-handler mutation',
                      'Namco enable/threshold/overflow; overclock and timestamp wrap',
                      'both aggregate admission and one-instruction native fallback executed']}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
