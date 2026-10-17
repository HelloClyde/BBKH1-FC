"""Real MIPS event-boundary and native-chain differential against FCEUmm.

Uses original diagnostic ROMs, real APU/DMA/Mapper callbacks and serialized
state. Only firmware/frontend services are mocked, as in jit_smoke.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from jit_smoke import Core, ROOT
from make_test_rom import make_rom


def snapshot(c):
    size=c.call(c.core[16])
    assert c.call(c.core[17],0x83200000,size)==1
    return c.m.read(0x83200000,size)


def put(c,old,new,value,size=4):
    if 'h1_apu_clock' in c.s and old in ('fhcnt','DMCacc','DMCSize','DMCHaveDMA'):
        address=c.s['h1_apu_clock']+{'fhcnt':0,'DMCacc':4,'DMCSize':8,'DMCHaveDMA':12}[old]
    else:address=c.s[new] if new in c.s else c.s[old]
    if new not in c.s and old in ('IRQa','IRQCount'):
        # Many mapper files have static IRQCount/IRQa symbols. Resolve the
        # pinned Namco state table instead of relying on an ambiguous nm name.
        address=struct.unpack('<I',c.m.read(c.s['N106_StateRegs']+(3 if old=='IRQCount' else 4)*16,4))[0]
    c.m.write(address,int(value).to_bytes(size,'little',signed=value<0))


def links(c):
    return struct.unpack('<I',c.m.read(c.s['h1_jit_links'],4))[0]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-20-jit/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/test-20-jit/H1FC.elf')
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'dist/H1FC.bda')
    p.add_argument('--baseline-elf',type=Path,default=ROOT/'build/release/H1FC.elf')
    p.add_argument('--nm',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-events.json')
    a=p.parse_args();cases=0;transitions=0
    # Mapper 19 fixture is our own cartridge, with only the mapper header set.
    for mapper in (0,19):
        rom=bytearray(make_rom(dmc=True));rom[6]=(rom[6]&15)|((mapper&15)<<4);rom[7]=mapper&0xf0
        old=Core(a.baseline_bda,a.baseline_elf,a.nm,bytes(rom))
        new=Core(a.bda,a.elf,a.nm,bytes(rom))
        regs=[35,254,128,240,4,4];ram=bytes(2048)
        # PHP remains outside the emitter. Exercise the optimized C event
        # scheduler independently from native blocks at exactly the same
        # APU/DMC boundaries, including a preceding instruction's carry.
        for frame in (1,143,144,145,10000):
            for dmc in (1,3,4,1000):
                for dma in (False,True):
                    for c in (old,new):
                        put(c,'fhcnt','h1_apu_fhcnt',frame)
                        put(c,'DMCacc','h1_apu_dmcacc',dmc)
                        put(c,'DMCSize','h1_apu_dmcsize',1 if dma else 0)
                        put(c,'DMCHaveDMA','h1_apu_have_dma',0,1)
                    before=new.native
                    assert old.run(0x9000,b'\x08\x60',regs,ram,tcount=2)==new.run(
                        0x9000,b'\x08\x60',regs,ram,tcount=2),(mapper,frame,dmc,dma,'C scheduler')
                    assert new.native==before,'PHP unexpectedly used the emitter'
                    assert snapshot(old)==snapshot(new),(mapper,frame,dmc,dma,'C serialized')
                    cases+=1
        for frame in (1,95,96,97,10000):
            for dmc in (1,2,3,1000):
                for dma in (False,True):
                    for c in (old,new):
                        put(c,'fhcnt','h1_apu_fhcnt',frame)
                        put(c,'DMCacc','h1_apu_dmcacc',dmc)
                        put(c,'DMCSize','h1_apu_dmcsize',1 if dma else 0)
                        put(c,'DMCHaveDMA','h1_apu_have_dma',0,1)
                    assert old.run(0x9000,b'\xea\xa9\x42\xea\x60',regs,ram)==new.run(
                        0x9000,b'\xea\xa9\x42\xea\x60',regs,ram,force=True),(mapper,frame,dmc,dma)
                    assert snapshot(old)==snapshot(new),(mapper,frame,dmc,dma,'serialized')
                    cases+=1
        if mapper==19:
            for enabled in (0,1):
                for count in (0,0x7ffc,0x7ffd,0x7ffe,0x7fff,0xfffe,0xffff):
                    for carry in (0,2):
                        for c in (old,new):
                            put(c,'IRQa','h1_namco_irq_enabled',enabled,1)
                            put(c,'IRQCount','h1_namco_irq_count',count,2)
                            put(c,'fhcnt','h1_apu_fhcnt',10000)
                            put(c,'DMCacc','h1_apu_dmcacc',1000)
                            put(c,'DMCSize','h1_apu_dmcsize',0)
                        assert old.run(0x9000,b'\xea\xa9\x42\xea\x60',regs,ram,tcount=carry)==new.run(
                            0x9000,b'\xea\xa9\x42\xea\x60',regs,ram,force=True,tcount=carry)
                        one,two=snapshot(old),snapshot(new)
                        assert one==two,(enabled,count,carry,'Namco IRQ',
                            [(i,x,y) for i,(x,y) in enumerate(zip(one,two)) if x!=y][:12])
                        cases+=1
                        # Repeat with an unsupported opcode to check the C
                        # scheduler's uint16 wrap and event-service path.
                        for c in (old,new):
                            put(c,'IRQa','h1_namco_irq_enabled',enabled,1)
                            put(c,'IRQCount','h1_namco_irq_count',count,2)
                        before=new.native
                        assert old.run(0x9000,b'\x08\x60',regs,ram,tcount=carry)==new.run(
                            0x9000,b'\x08\x60',regs,ram,tcount=carry)
                        assert new.native==before
                        assert snapshot(old)==snapshot(new),(enabled,count,carry,'C Namco IRQ')
                        cases+=1
        # Two separately compiled blocks, with conditional branches in both
        # directions. Mutate a chained target with an unchanged first opcode.
        first=b'\xa9\x23\xaa\xe8\x86\x10\xea\xd0\x67'
        second=b'\xa9\x80\xa8\xc8\x84\x11\xea\xd0\x87'
        for c in (old,new):
            put(c,'fhcnt','h1_apu_fhcnt',1000000)
            put(c,'DMCacc','h1_apu_dmcacc',1000000)
            put(c,'DMCSize','h1_apu_dmcsize',0)
            if mapper==19:put(c,'IRQa','h1_namco_irq_enabled',0,1)
            c.prg(0x9070,second)
        for pc,code in ((0x9070,second),(0x9000,first)):
            page=struct.unpack('<I',new.m.read(new.s['Page']+(pc>>11)*4,4))[0]
            new.prg(pc,code);new.call(new.s['h1_nes_jit_compile'],pc,code[0],page,1)
        before=links(new)
        assert old.run(0x9000,first,regs,ram,1000)==new.run(0x9000,first,regs,ram,1000)
        assert links(new)>before,'No cross-block native transitions executed'
        assert snapshot(old)==snapshot(new),'chained state';cases+=1
        for irq in (1,0x100,0x200,0x800):
            before=links(new)
            assert old.run(0x9000,first,regs,ram,1000,irq)==new.run(0x9000,first,regs,ram,1000,irq)
            assert links(new)>before,'Masked IRQ did not stay in native chain'
            assert snapshot(old)==snapshot(new),('masked chain',irq);cases+=1
        for c in (old,new):c.prg(0x9071,b'\x44')
        assert old.run(0x9000,first,regs,ram,1000)==new.run(0x9000,first,regs,ram,1000)
        assert snapshot(old)==snapshot(new),'changed target state';cases+=1
        # Restore invalidates both target entries and preserves the full state.
        for c in (old,new):
            size=c.call(c.core[16]);assert c.call(c.core[18],0x83200000,size)==1
        assert old.run(0x9000,first,regs,ram,1000)==new.run(0x9000,first,regs,ram,1000)
        assert snapshot(old)==snapshot(new),'restored chain state';cases+=1
        # A 16+16+16 instruction cycle crosses the 15-bit return packet limit.
        # Disable sound-clock advancement for this artificial >frame budget;
        # this test targets packet chunking, not an oversized audio frame.
        long_code=b'\xea'*47+b'\x4c\x00\x90'
        for c in (old,new):
            c.m.word(c.s['overclocked'],1);c.prg(0x9000,long_code)
        for pc in (0x9000,0x9010,0x9020):
            page=struct.unpack('<I',new.m.read(new.s['Page']+(pc>>11)*4,4))[0]
            new.call(new.s['h1_nes_jit_compile'],pc,0xea,page,1)
        before=links(new)
        assert old.run(0x9000,long_code,regs,ram,400000)==new.run(0x9000,long_code,regs,ram,400000)
        assert links(new)-before>4096,'Long chain did not cross the packet boundary'
        assert snapshot(old)==snapshot(new),'15-bit native count boundary';cases+=1
        for c in (old,new):c.m.word(c.s['overclocked'],0)
        transitions+=links(new);old.close();new.close()
    report={'ok':True,'cases':cases,'native_chain_transitions':transitions,
            'bda_sha256':hashlib.sha256(a.bda.read_bytes()).hexdigest(),
            'checks':['APU frame and DMC deadlines before/at/after instruction boundary',
                      'independent interpreter scheduler deadlines and pending cycle carry',
                      'DMC DMA combined with frame or bit events',
                      'Namco IRQ disabled, threshold, overflow and branch cycle carry',
                      'both native branch targets; target operand mutation; restored chain epoch',
                      'masked IRQ native chains and temporary IRQ clearing',
                      'long 16-op native chains crossing the 15-bit packet boundary',
                      'exact CPU/RAM/timestamps and full serialized state']}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
