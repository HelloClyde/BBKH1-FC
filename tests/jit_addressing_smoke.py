"""Differential coverage of JIT arithmetic, bus addressing and IO boundaries.

Actual emitted MIPS is checked against the pinned interpreter. Test-owned bus
callbacks deliberately observe CPU/DB/cycle state and change A/P. Indexed reads
retain FCEUmm's target^0x100 dummy-read behavior, including xxFF/FFFF carries.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from jit_smoke import Core, ROOT
from jit_event_smoke import snapshot


def compile_at(c,pc,code):
    c.prg(pc,code)
    page=struct.unpack('<I',c.m.read(c.s['Page']+(pc>>11)*4,4))[0]
    c.call(c.s['h1_nes_jit_compile'],pc,code[0],page,1)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-20-jit/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/test-20-jit/H1FC.elf')
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'dist/H1FC.bda')
    p.add_argument('--baseline-elf',type=Path,default=ROOT/'build/release/H1FC.elf')
    p.add_argument('--nm',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-addressing.json')
    a=p.parse_args();old=Core(a.baseline_bda,a.baseline_elf,a.nm);new=Core(a.bda,a.elf,a.nm)
    cases=0;bus_cases=0;ram=bytes(2048)
    for op in (0x69,0xe9):
        for acc in (0,1,0x7f,0x80,0xfe,0xff):
            for value in (0,1,0x7f,0x80,0xfe,0xff):
                for status in (0,1,0x7e,0xff):
                    regs=[acc,255,1,240,status,status]
                    code=bytes([op,value,0xea,0x60])
                    assert old.run(0x9000,code,regs,ram)==new.run(
                        0x9000,code,regs,ram,force=True),(op,acc,value,status)
                    cases+=1
    for op in (0x65,0x75,0xe5,0xf5):
        for value in (0,1,0x7f,0x80,0xfe,0xff):
            data=bytearray(ram);data[255 if op in (0x65,0xe5) else 254]=value
            regs=[0x7f,255,1,240,0xff,0xff]
            code=bytes([op,255,0xea,0x60])
            assert old.run(0x9000,code,regs,bytes(data))==new.run(
                0x9000,code,regs,bytes(data),force=True),(op,value)
            cases+=1

    # Observable read callbacks force the generic tier, and prove exact bus
    # order/DB/PC/tcount, including the pinned XOR dummy read across 16-bit wrap.
    for op in (0xbd,0xb9,0xbe,0xbc):
        for base in (0x00ff,0x01ff,0x07ff,0x7fff,0x80ff,0xffff):
            for index in (0,1,127,255):
                effective=(base+index)&65535
                addresses=[effective^0x100,effective] if (effective^base)&0x100 else [effective]
                saved={};traces={old:[],new:[]}
                for c in (old,new):
                    for i,address in enumerate(dict.fromkeys(addresses)):
                        saved[c,address]=c.m.read(c.s['ARead']+address*4,4)
                        def read(address,*args,core=c):
                            traces[core].append((address,core.m.read(core.s['X'],28)))
                            # Original data reads may observe and mutate state.
                            core.m.write(core.s['X']+6,b'\x37')
                            core.m.write(core.s['X']+10,b'\x41')
                            return (address^0xa5)&255
                        stub=c.stub(0x80001c00+i*0x20,read)
                        c.m.word(c.s['ARead']+address*4,stub)
                code=bytes([op,base&255,base>>8,0xea,0x60])
                regs=[0x81,index,index,240,4,4]
                assert old.run(0x9000,code,regs,ram)==new.run(
                    0x9000,code,regs,ram,force=True),(op,hex(base),index,'CPU')
                assert traces[old]==traces[new],(op,hex(base),index,'bus',traces)
                assert [item[0] for item in traces[new]]==addresses
                assert snapshot(old)==snapshot(new),(op,base,index,'state')
                for (c,address),value in saved.items():c.m.write(c.s['ARead']+address*4,value)
                cases+=1;bus_cases+=1

    # Fast IO writes must spill A/P before callbacks, and retain callback
    # changes while returning to the scheduler at the exact opcode boundary.
    for op in (0x8d,0x8e,0x8c):
        saved={};traces={old:[],new:[]}
        for c in (old,new):
            saved[c]=c.m.read(c.s['BWrite']+0x5000*4,4)
            def write(address,value,*args,core=c):
                traces[core].append((address,value,core.m.read(core.s['X'],28)))
                core.m.write(core.s['X']+6,b'\x37')
                core.m.write(core.s['X']+10,b'\x41')
                return 0
            c.m.word(c.s['BWrite']+0x5000*4,c.stub(0x80001c80,write))
        code=bytes([0xa9,0x80,op,0x00,0x50,0xea,0x60])
        regs=[1,2,3,240,4,4];before=new.native
        assert old.run(0x9000,code,regs,ram,18)==new.run(
            0x9000,code,regs,ram,18,force=True),(op,'IO state')
        assert new.native>before
        assert traces[old]==traces[new],(op,'IO observations')
        assert snapshot(old)==snapshot(new),(op,'IO serialized')
        for c in (old,new):c.m.write(c.s['BWrite']+0x5000*4,saved[c])
        cases+=1;bus_cases+=1

    # A mapper IO write may change the very next opcode's PRG bank. The
    # terminal store must return before it can execute stale prepared bytes.
    compile_at(new,0x9005,b'\xa9\x10\xaa\x60')
    originals={};saved={};traces={old:[],new:[]}
    for c in (old,new):
        page_address=c.s['Page']+(0x9000>>11)*4
        originals[c]=c.m.read(page_address,4)
        page=struct.unpack('<I',originals[c])[0]
        c.m.write((page+0x9800)&0xffffffff,b'\xea'*5+b'\xa9\x42\xaa\x60')
        saved[c]=c.m.read(c.s['BWrite']+0x5000*4,4)
        def write(address,value,*args,core=c,new_page=page+0x800):
            traces[core].append((address,value,core.m.read(core.s['X'],28)))
            core.m.word(core.s['Page']+(0x9000>>11)*4,new_page)
            return 0
        c.m.word(c.s['BWrite']+0x5000*4,c.stub(0x80001cc0,write))
    code=b'\xa9\x80\x8d\x00\x50\xa9\x10\xaa\x60'
    regs=[0,1,1,240,4,4];before=new.native
    assert old.run(0x9000,code,regs,ram,30)==new.run(
        0x9000,code,regs,ram,30,force=True),'mapper write remap'
    assert new.native>before and new.m.read(new.s['X']+6,2)==b'\x42\x42'
    assert traces[old]==traces[new],'mapper write observations'
    for c in (old,new):
        c.m.write(c.s['Page']+(0x9000>>11)*4,originals[c])
        c.m.write(c.s['BWrite']+0x5000*4,saved[c])
    cases+=1;bus_cases+=1

    # A handler changed after compilation must leave a partially executed
    # block with its resident A/P intact, before any clocks/DB for that read.
    for changed in (0x0200,0x0300):
        code=b'\xa9\x80\xb9\xff\x01\xea\x60'
        compile_at(new,0x9000,code)
        saved={};traces={old:[],new:[]}
        for c in (old,new):
            saved[c]=c.m.read(c.s['ARead']+changed*4,4)
            def read(address,*args,core=c):
                traces[core].append((address,core.m.read(core.s['X'],28)))
                return 0x43
            c.m.word(c.s['ARead']+changed*4,c.stub(0x80001ca0,read))
        regs=[0,1,1,240,4,4];before=new.native
        assert old.run(0x9000,code,regs,ram,18)==new.run(
            0x9000,code,regs,ram,18),(changed,'mid-block guard')
        assert new.native>before
        assert traces[old]==traces[new],(changed,'fallback bus')
        for c in (old,new):c.m.write(c.s['ARead']+changed*4,saved[c])
        cases+=1;bus_cases+=1

    # RAM stores/RMW include mirrors; indirect JMP preserves the 6502 pointer
    # page-wrap bug. All paths execute through the production admission tier.
    for op in (0x8d,0x8e,0x8c,0xee,0xce,0x6c):
        for address in (0,0xff,0x1ff,0x7ff,0x800,0x1fff):
            code=bytes([0xea,op,address&255,address>>8,0xea,0x60])
            data=bytearray(ram);data[address&0x7ff]=0xff
            data[((address&0xff00)|((address+1)&255))&0x7ff]=0x90
            regs=[0x80,0x7f,0xff,240,4,4];before=new.native
            assert old.run(0x9000,code,regs,bytes(data),10)==new.run(
                0x9000,code,regs,bytes(data),10,force=True),(op,address,'RAM')
            assert new.native>before,(op,address,'native')
            cases+=1

    # A dynamic JMP target can directly enter a warm block in another PRG
    # bank. Verify actual native transitions and the complete resulting state.
    first=b'\xa9\x80\x6c\x40\x00'
    second=b'\xa9\x23\xaa\xe8\x86\x10\xea\xd0\xf7'
    for c in (old,new):c.prg(0xa000,second)
    compile_at(new,0xa000,second);compile_at(new,0x9000,first)
    data=bytearray(ram);data[0x40:0x42]=b'\x00\xa0'
    regs=[0,1,1,240,4,4]
    before=struct.unpack('<I',new.m.read(new.s['h1_jit_links'],4))[0]
    assert old.run(0x9000,first,regs,bytes(data),1000)==new.run(
        0x9000,first,regs,bytes(data),1000),'indirect warm cross-bank chain'
    assert struct.unpack('<I',new.m.read(new.s['h1_jit_links'],4))[0]>before
    cases+=1
    assert snapshot(old)==snapshot(new),'final serialized state'
    native=new.native;old.close();new.close()
    report={'ok':True,'cases':cases,'observable_bus_cases':bus_cases,
            'native_block_entries':native,'bda_sha256':hashlib.sha256(a.bda.read_bytes()).hexdigest(),
            'checks':['ADC/SBC carry/overflow/decimal-bit preservation',
                      'zero-page index wrapping','indexed dummy reads at xxFF/FFFF carries',
                      'exact callback-visible CPU/DB/PC/tcount and bus ordering',
                      'IO callback resident-state spill/reload and block termination',
                      'mapper IO remaps next opcode before dispatch',
                      'handler mutation after partial block execution',
                      'RAM mirrors and INC/DEC wrap','indirect JMP pointer-page wrap',
                      'indirect JMP directly chains to a warm block in another bank']}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
