"""Actual MIPS memory primitives: byte oracle, alignment and access bounds."""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn import UC_HOOK_MEM_READ
from unicorn.mips_const import UC_MIPS_REG_V0, UC_MIPS_REG_PC, UC_MIPS_REG_SP
from indexed_scale_v4 import Scaler
from pause_menu_smoke import symbol


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,required=True)
    p.add_argument('--map',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    s=Scaler(a.bda,a.map);m=s.machine
    for name in ('memcpy','memmove','memcmp'):s.addresses[name]=symbol(name,a.map)
    source,dest=0x83020000,0x83040000
    windows=((source-32,source+8192),(dest-32,dest+8192))
    allowed_read=[];observations=[0]
    def observe(uc,access,address,size,value,user):
        # Unicorn reports physical KSEG addresses for memory hooks.
        address |= 0x80000000
        if not any(lo<=address<hi for lo,hi in windows):return
        index=0;regions=allowed_read
        assert any(lo<=address and address+size<=hi for lo,hi in regions),(
            'read' if index==0 else 'write',hex(address),size,regions)
        observations[index]+=1
    m.uc.hook_add(UC_HOOK_MEM_READ,observe,user_data=0)
    # Unicorn's MIPS write hook faults on a branch-delay byte store, including
    # the unchanged baseline memcpy. Check complete destination canary buffers
    # instead; read hooks do work and remain active for every input span.
    # Initialization ran before hooks were attached. Flush only the host's
    # translated code so every subsequent primitive access is observed.
    m.uc.ctl_flush_tb()
    counts=dict(copy=0,compare=0,move=0)
    lengths=list(range(18))+[31,32,33,63,64,65,127,128,129,
        255,256,257,511,512,513,1024,1160,1240,1920,3072,4096]
    payload=bytes((i*71+(i>>3)*19+173)&255 for i in range(8192))
    def call(name,dst,src,n):
        allowed_read[:]=[(src,src+n)]
        if name=='memcmp':allowed_read.append((dst,dst+n))
        try:
            s.call(name,dst,src,n)
        except Exception as error:
            raise AssertionError((name,hex(dst),hex(src),n,
                hex(m.uc.reg_read(UC_MIPS_REG_PC)),hex(m.uc.reg_read(UC_MIPS_REG_SP)))) from error
        return m.uc.reg_read(UC_MIPS_REG_V0)
    for so in range(4):
        for do in range(4):
            for n in lengths:
                src,dst=source+so,dest+do
                data=payload[:n]
                m.write(source-16,b'\x5a'*16+payload+b'\x5a'*16)
                expected=bytearray(b'\xa5'*8224)
                expected[16+do:16+do+n]=payload[so:so+n]
                m.write(dest-16,b'\xa5'*8224)
                assert call('memcpy',dst,src,n)==dst
                assert m.read(dest-16,8224)==expected
                assert m.read(source-16,8224)==b'\x5a'*16+payload+b'\x5a'*16
                counts['copy']+=1
                m.write(src,data);m.write(dst,data)
                assert call('memcmp',dst,src,n)==0
                counts['compare']+=1
                positions=(range(n) if n in (15,16,17,63,64,65) and
                    (so,do) in ((0,0),(1,3)) else {0,n//2,n-1})
                for x in positions:
                    if n==0:continue
                    changed=bytearray(data);changed[x]^=0xff
                    m.write(dst,bytes(changed))
                    expected=changed[x]-data[x]
                    assert call('memcmp',dst,src,n)==expected&0xffffffff
                    counts['compare']+=1
                # Both input spans remain unchanged by memcmp.
                assert m.read(src,n)==data
        print('PASS source alignment',so,flush=True)
    for n in range(81):
        for delta in (-17,-7,-1,0,1,7,17):
            for offset in (0,1,3):
                original=bytearray(payload[:256])
                src=source+64+offset;dst=src+delta
                expected=bytearray(original)
                expected[dst-source:dst-source+n]=original[src-source:src-source+n]
                m.write(source,bytes(original))
                assert call('memmove',dst,src,n)==dst
                assert m.read(source,256)==expected
                counts['move']+=1
    assert all(observations)
    report=dict(ok=True,bda_sha256=hashlib.sha256(a.bda.read_bytes()).hexdigest(),
        cases=counts,observed_reads=observations[0],
        checks=['Exact unsigned byte comparison including first mismatch',
                'All source/destination alignments and word/64-byte tails',
                'No reads outside requested input spans; complete destination buffer oracle',
                'Return pointer, destination sentinels, overlapping memmove'],
        limitation='Actual MIPS semantics, not physical cache/bus timing. Write hooks fault on baseline branch-delay byte stores; writes checked with full-buffer canaries instead.')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
