"""Actual MIPS emitter versus the actual pinned interpreter at CPU boundaries.

Both run the core's real timing/APU hooks. Only frontend/firmware IO is mocked.
Test-owned PRG bytes are changed deliberately to test cache guards, not core code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct
from unicorn import UC_HOOK_BLOCK
from unicorn.mips_const import *
from core_equivalence import symbols
from h1_mock import Machine, ROOT
from make_test_rom import make_rom


class Core:
    def __init__(self,bda,elf,nm,rom=None):
        self.s=symbols(elf,nm)
        self.m=Machine(bda,files={'A:\\FC\\LOCAL.nes':make_rom() if rom is None else rom})
        self.core=struct.unpack('<19I',self.m.read(self.s['h1_nes_core'],76))
        self.native=0
        self.arena=None
        self.m.uc.hook_add(UC_HOOK_BLOCK,self.block)
        self.call(self.core[1],self.s['environment'])
        self.call(self.core[2],self.stub(0x80001b00,lambda *a:0))
        self.call(self.core[3],self.stub(0x80001b20,lambda ptr,n,*a:n))
        self.call(self.core[4],self.stub(0x80001b40,lambda *a:0))
        self.call(self.core[5],self.stub(0x80001b60,lambda *a:0))
        self.call(self.core[6])
        self.m.write(0x83000000,b'A:\\FC\\LOCAL.nes\0')
        self.m.write(0x83000040,struct.pack('<4I',0x83000000,0,0,0))
        assert self.call(self.core[8],0x83000040)==1
        if 'cache' in self.s and 'h1_jit_stats' in self.s:
            ptr=struct.unpack('<I',self.m.read(self.s['cache'],4))[0]
            size=struct.unpack('<I',self.m.read(self.s['cache_bytes'],4))[0]
            self.arena=(ptr,ptr+size)

    def block(self,uc,address,size,user):
        if self.arena and self.arena[0]<=address<self.arena[1]:self.native+=1

    def stub(self,address,fn):
        self.m.callbacks[address]=fn
        self.m.write(address,struct.pack('<II',0x03e00008,0))
        return address

    def call(self,address,*args):
        self.m.finished=False
        for r,v in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            self.m.uc.reg_write(r,v)
        self.m.uc.reg_write(UC_MIPS_REG_RA,0x80001ff0)
        self.m.uc.emu_start(address,0,timeout=30_000_000,count=100_000_000)
        assert self.m.finished,hex(address)
        return self.m.uc.reg_read(UC_MIPS_REG_V0)

    def prg(self,pc,data):
        page=struct.unpack('<I',self.m.read(self.s['Page']+(pc>>11)*4,4))[0]
        self.m.write((page+pc)&0xffffffff,data)

    def run(self,pc,data,regs,ram,cycles=1,irq=0,encrypted=0,force=False,tcount=0):
        self.prg(pc,data)
        if force and 'h1_nes_jit_compile' in self.s:
            page=struct.unpack('<I',self.m.read(self.s['Page']+(pc>>11)*4,4))[0]
            self.call(self.s['h1_nes_jit_compile'],pc,data[0],page,1)
        cpu=bytearray(28)
        struct.pack_into('<I',cpu,0,tcount)
        struct.pack_into('<H',cpu,4,pc)
        cpu[6:12]=bytes(regs)
        struct.pack_into('<I',cpu,20,irq)
        self.m.write(self.s['X'],bytes(cpu))
        self.m.write(self.s['RAM'],ram)
        self.m.write(self.s['encryptOpcodes'],bytes([encrypted]))
        self.call(self.s['X6502_Run'],cycles)
        return self.m.read(self.s['X'],28)+self.m.read(self.s['RAM'],2048)+b''.join(
            self.m.read(self.s[n],4) for n in ('timestamp','sound_timestamp'))

    def close(self):
        self.call(self.core[7]);assert not self.m.allocations and not self.m.handles


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,default=ROOT/'build/test-20-jit/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/test-20-jit/H1FC.elf')
    p.add_argument('--baseline-bda',type=Path,default=ROOT/'dist/H1FC.bda')
    p.add_argument('--baseline-elf',type=Path,default=ROOT/'build/release/H1FC.elf')
    p.add_argument('--nm',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-smoke.json')
    a=p.parse_args();old=Core(a.baseline_bda,a.baseline_elf,a.nm);new=Core(a.bda,a.elf,a.nm)
    modes=new.m.read(new.s['h1_jit_modes'],256)
    cycle_table=new.m.read(new.s['CycTable'],256)
    rng=random.Random(6502);cases=0
    for op,mode in enumerate(modes):
        if not mode:continue
        executed_before=struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[3]
        for i in range(32):
            regs=[rng.randrange(256) for _ in range(6)]
            # Every branch flag polarity, all P bits, zero and negative operands.
            regs[4]=(0,255,1,2,64,128)[i%6] if i<12 else rng.randrange(256)
            ram=bytes(rng.randrange(256) for _ in range(2048))
            operand=(0,1,127,128,255,rng.randrange(256))[i%6]
            operands=[] if mode==1 else [operand]
            if mode>=4:
                address=(0,255,0x2002,0x8000,0x6000)[i%5]
                operands=[address&255,address>>8]
            code=bytes([op]+operands+[0xea]*8+[0x60])
            # Vary PC to cover bank offsets and branch page crossing/wrap.
            pc=(0x8010,0x80fd,0x8100,0xfffd)[i%4]
            if pc==0xfffd and len(code)>3:code=code[:3]
            # A control-flow opcode terminates its block. Prefix RTS too so
            # emitter coverage includes it without relaxing the two-op rule.
            prefixed = mode in (3,5,9) or op==0x60 or pc==0xfffd
            if prefixed:
                pc-=1;code=b'\xea'+code;cycles=10
                # Budget executes NOP (2 cycles) plus this op (>=2 cycles).
            else:
                # Credit one unit more than the first opcode's base cycles,
                # so the cost-aware dispatcher can start a second opcode.
                # Keep the native-op counter assertion for every opcode.
                cycles=3*cycle_table[op]+1
            if not prefixed:
                # Retain the old single-instruction boundary observation.
                # This now deliberately checks the short-budget C path;
                # the following run still proves native opcode execution.
                first=old.run(pc,code,regs,ram,1)
                second=new.run(pc,code,regs,ram,1,force=True)
                assert first==second,(hex(op),i,'short',first[:28].hex(),second[:28].hex())
                cases+=1
            first=old.run(pc,code,regs,ram,cycles);second=new.run(pc,code,regs,ram,cycles,force=True)
            assert first==second,(hex(op),i,hex(pc),first[:28].hex(),second[:28].hex())
            cases+=1
        assert struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[3]>executed_before,hex(op)
    # Same PC/page/first opcode; mutate a later opcode between executions.
    regs=[32,255,128,240,4,0];ram=bytes(2048)
    for code in (b'\xea\xa9\x12\xaa\xea\x60',b'\xea\xa2\x34\x8a\xea\x60',
                 b'\xea\xa2\xff\x8a\xea\x60'):
        assert old.run(0x8200,code,regs,ram,100)==new.run(0x8200,code,regs,ram,100)
        cases+=1
    # Mapper hook changes the page after opcode fetch, before operand fetch.
    original={c:struct.unpack('<I',c.m.read(c.s['Page']+16*4,4))[0] for c in (old,new)}
    for c in (old,new):
        c.m.word(c.s['Page']+16*4,original[c])
        c.prg(0x8800,b'\x60\x91\xaa\xea\x60')
        c.saved_hook=struct.unpack('<I',c.m.read(c.s['MapIRQHook'],4))[0]
        changed=[False]
        def swap(cycles,*args,core=c,once=changed):
            if not once[0]:
                core.m.word(core.s['Page']+16*4,original[core]+0x800);once[0]=True
            return 0
        c.m.word(c.s['MapIRQHook'],c.stub(0x80001b80,swap))
    assert old.run(0x8000,b'\xa9\x22\xaa\xea\x60',regs,ram,20)==new.run(0x8000,b'\xa9\x22\xaa\xea\x60',regs,ram,20,force=True)
    assert new.m.read(new.s['X']+6,2)==b'\x91\x91'
    for c in (old,new):
        c.m.word(c.s['MapIRQHook'],c.saved_hook)
        c.m.word(c.s['Page']+16*4,original[c])
    cases+=1
    # Writable PRG execution and encrypted modes are interpreter-only.
    native_before=new.native
    assert old.run(0x6000,b'\xa9\x42\xaa\xea\x60',regs,ram,20)==new.run(0x6000,b'\xa9\x42\xaa\xea\x60',regs,ram,20)
    assert new.native==native_before
    assert old.run(0x8500,b'\xea'*8,regs,ram,20,encrypted=10)==new.run(0x8500,b'\xea'*8,regs,ram,20,encrypted=10)
    assert new.native==native_before
    cases+=2
    # NMI/IRQ entry and CLI's delayed interrupt polling remain interpreter work.
    for c in (old,new):
        c.prg(0xfffa,struct.pack('<3H',0x8500,0x8500,0x8500))
        c.prg(0x8500,b'\x40')
    for irq in (1,0x80,0x40,0x20,0x100,0x200,0x800):
        for status in (0,4):
            r=[*regs];r[4]=status;r[5]=status
            assert old.run(0x8600,b'\x58\xea\xa9\x10\xea\x60',r,ram,100,irq)==new.run(0x8600,b'\x58\xea\xa9\x10\xea\x60',r,ram,100,irq)
            cases+=1
    # Page pointer remapping: alternate the two physical NROM views at same PC.
    original={c:struct.unpack('<I',c.m.read(c.s['Page']+16*4,4))[0] for c in (old,new)}
    for c in (old,new):c.m.write(original[c]+0x8000+0x800,b'\xa9\x91\xaa\xea\x60')
    for displacement in (0,0x800,0):
        for c in (old,new):c.m.word(c.s['Page']+16*4,original[c]+displacement)
        code=b'\xa9\x91\xaa\xea\x60' if displacement else b'\xa9\x23\xaa\xea\x60'
        assert old.run(0x8000,code,regs,ram,20)==new.run(0x8000,code,regs,ram,20)
        cases+=1
    # Save/load actually invalidates metadata and resumes with identical state.
    for c in (old,new):
        size=c.call(c.core[16]);assert c.call(c.core[17],0x83200000,size)==1
        before=struct.unpack('<8I',c.m.read(c.s['h1_jit_stats'],32))[6] if c is new else 0
        assert c.call(c.core[18],0x83200000,size)==1
        if c is new:assert struct.unpack('<8I',c.m.read(c.s['h1_jit_stats'],32))[6]>before
    assert old.run(0x8300,b'\xa9\x80\xaa\xea\x60',regs,ram,20)==new.run(0x8300,b'\xa9\x80\xaa\xea\x60',regs,ram,20)
    # Overclock changes invalidate a specialization; IRQ/APU semantics remain
    # the same as the original core in both modes.
    for enabled in (1,0):
        for c in (old,new):c.m.word(c.s['overclocked'],enabled)
        assert old.run(0x8300,b'\xa9\x42\xaa\xea\x60',regs,ram,20)==new.run(0x8300,b'\xa9\x42\xaa\xea\x60',regs,ram,20,force=True)
        cases+=1
    before=struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[6]
    # The linker can inline/GC Reset; Power calls that same reset path.
    for c in (old,new):c.call(c.s['X6502_Power'])
    assert struct.unpack('<8I',new.m.read(new.s['h1_jit_stats'],32))[6]>before
    wraps=struct.unpack('<I',new.m.read(new.s['h1_jit_wraps'],4))[0]
    capacity=struct.unpack('<I',new.m.read(new.s['cache_bytes'],4))[0]
    if capacity==1024*1024:assert wraps>0,'Forced backend cases did not exercise arena reclamation'
    # Allocation failure leaves a fully functional interpreter.
    native_before=new.native;new.call(new.s['h1_nes_jit_close'])
    original_alloc=new.m.alloc
    for address,fn in list(new.m.callbacks.items()):
        if fn==original_alloc:new.m.callbacks[address]=lambda *a:0;alloc_stub=address;break
    new.call(new.s['h1_nes_jit_open'],0)
    assert struct.unpack('<I',new.m.read(new.s['cache'],4))[0]==0
    assert old.run(0x8400,b'\xa9\x42\xaa\xea\x60',regs,ram,20)==new.run(0x8400,b'\xa9\x42\xaa\xea\x60',regs,ram,20)
    assert new.native==native_before
    new.m.callbacks[alloc_stub]=original_alloc
    assert native_before>0
    old.close();new.close()
    report={'ok':True,'cases':cases,'supported_opcodes':sum(bool(v) for v in modes),
            'native_block_entries':native_before,'bda_sha256':hashlib.sha256(a.bda.read_bytes()).hexdigest(),
            'cache_wraps':wraps,
            'checks':['exact CPU/RAM/cycle/timestamp differential','both branch outcomes and page crossing',
                      'zero-page index wrapping','later opcode/operand mutation','PRG page remapping',
                      'mapper hook changes operand page after opcode fetch',
                      'writable PRG and encrypted opcode fallback','IRQ/NMI entry and CLI delay',
                      'state restore invalidation','arena reclamation and epoch invalidation',
                      'overclock specialization invalidation','reset invalidates cache metadata',
                      'allocation failure interpreter fallback','unload frees cache']}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
