"""Actual ordinary-build MIPS save transactions and interrupted-slot recovery.

No USB/device writes. Filesystem services and power loss are modeled in RAM;
this verifies the real application's slot/CRC/ownership logic, not FAT/FTL
durability or the latency previously measured on the device.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib
from unicorn.mips_const import UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3,UC_MIPS_REG_RA,UC_MIPS_REG_V0
from h1_mock import Machine,ROOT
from make_test_rom import make_rom
from pause_menu_smoke import symbol

OUT=ROOT/'build/save-update-smoke'
BUILD=OUT/'ordinary';ROM='A:\\FC\\SAVE-UPDATE.nes'
STORE,PATH,DATA,RESTORED=0x83100000,0x83101000,0x83102000,0x83110000


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(toolchain):
    """Use current ordinary build, changing only its output directory.

    All compiler flags/dependency checks/source preparation remain in the
    production builder. Its fixed release directory is never overwritten.
    """
    subprocess.run([sys.executable,str(ROOT/'tools/build.py'),'--build-dir',str(BUILD),
                    '--toolchain',str(toolchain)],check=True)


def slot(payload,generation,rom_id):
    h=struct.pack('<6I',0x31534748,1,rom_id,generation,len(payload),zlib.crc32(payload))
    return h+struct.pack('<I',zlib.crc32(h))+payload


class SaveMachine(Machine):
    def __init__(self,files,fault=None,cut=None):
        self.ops=[];self.fault=fault;self.fault_hit=False;self.cut=cut;self.power_lost=False;self.writer_seen=False
        self.closed=[];super().__init__(BUILD/'H1FC.bda',files=files,path=ROM)

    def once(self,name):
        if self.fault==name and not self.fault_hit:self.fault_hit=True;return True
        return False

    def call(self,name,*args):
        self.finished=False
        for reg,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):self.uc.reg_write(reg,value)
        self.uc.reg_write(UC_MIPS_REG_RA,0x80001ff0)
        self.uc.emu_start(symbol(name,BUILD/'H1FC.map'),0,timeout=20_000_000,count=20_000_000)
        if self.power_lost:return None
        assert self.finished,name
        value=self.uc.reg_read(UC_MIPS_REG_V0);return value if value<0x80000000 else value-0x100000000

    def fopen(self,path,mode,*args):
        p,m=self.string(path),self.string(mode);self.ops.append(['open',p,m])
        if m=='r+b' and self.once('reject_r+b'):return 0
        if '+' in m and self.fault=='unsupported_update':self.fault_hit=True;return 0
        if m=='wb' and self.once('create_fail'):return 0
        return super().fopen(path,mode,*args)

    def fseek(self,handle,off,origin,*args):
        p,m,stream=self.handles[handle];self.ops.append(['seek',p,m,off,origin])
        if '+' in m:
            if self.once('end_fail') and origin==2:return -1
            if origin==0 and self.once('rewind_fail'):return -1
            if origin==0 and self.once('rewind_wrong_position'):stream.seek(1);return 1
            if self.fault=='end_then_close_fail' and origin==2:return -1
        return super().fseek(handle,off,origin,*args)

    def fclose(self,handle,*args):
        assert handle not in self.closed,'Released firmware handle closed twice'
        p,m,stream=self.handles[handle];self.ops.append(['close',p,m,handle]);self.closed.append(handle)
        rc=super().fclose(handle,*args)  # valid handle is removed before failure
        assert handle not in self.handles
        if '+' in m and self.once('end_then_close_fail'):return -1
        if '+' in m and not self.writer_seen and self.once('length_then_close_fail'):return -1
        if m!='rb' and self.writer_seen and self.once('writer_close_fail'):return -1
        return rc

    def fwrite(self,ptr,size,count,handle,*args):
        p,m,stream=self.handles[handle];n=size*count;self.writer_seen=True;self.ops.append(['write',p,m,n])
        if self.cut is not None:
            data=self.read(ptr,n);assert 0<=self.cut<=n
            old=self.files.get(p,b'') if '+' in m else b''
            # A power cut may persist any prefix while old suffix remains.
            # Full persistence before close is also legal and must recover.
            self.files[p]=data[:self.cut]+old[self.cut:]
            self.power_lost=True;self.uc.emu_stop();return 0
        if self.once('short_write'):return stream.write(self.read(ptr,n//2))
        if self.once('zero_write'):return 0
        return super().fwrite(ptr,size,count,handle,*args)

    def fread(self,ptr,size,count,handle,*args):
        p,m,stream=self.handles[handle];self.ops.append(['read',p,count])
        got=super().fread(ptr,size,count,handle,*args)
        if self.writer_seen and p.endswith('.s1') and count>28 and self.once('corrupt_readback'):
            self.write(ptr,bytes([self.read(ptr,1)[0]^1]))
        return got


def setup(kind='same',n=8320,fault=None,cut=None):
    rom=make_rom();rom_id=zlib.crc32(rom)^len(rom)
    old=bytes((i*19+3)&255 for i in range(n));new=bytes(x^0x5a for x in old)
    files={ROM:rom,ROM+'.s0':slot(old,20,rom_id)}
    if kind!='absent':files[ROM+'.s1']=slot(bytes([7])*n,19,rom_id)
    if kind=='short':files[ROM+'.s1']=b'old-format'
    if kind=='long':files[ROM+'.s1']+=b'trailing'
    if kind=='first':files={ROM:rom}
    m=SaveMachine(files,fault,cut)
    m.write(PATH,ROM.encode()+b'\0');m.write(DATA,old)
    expected=0 if kind=='first' else 1
    assert m.call('save_load',STORE,PATH,DATA,n)==expected
    if kind!='first':assert m.read(DATA,n)==old
    m.write(DATA,new);m.ops=[]
    return m,files,old,new,rom_id


def recover(files,n):
    m=SaveMachine(files);m.write(PATH,ROM.encode()+b'\0');m.write(RESTORED,bytes(n))
    rc=m.call('save_load',STORE,PATH,RESTORED,n)
    assert not m.handles and not m.allocations
    return rc,m.read(RESTORED,n),m.read(STORE,284)


def normal_case(kind='same',n=8320,fault=None):
    m,files,old,new,rom_id=setup(kind,n,fault)
    before=m.read(STORE,284);rc=m.call('save_checkpoint',STORE,DATA,n)
    if ROM+'.s0' in files:assert m.files[ROM+'.s0']==files[ROM+'.s0'],'active slot modified'
    assert not m.handles and not m.allocations and m.read(DATA,n)==new
    modes=[op[2] for op in m.ops if op[0]=='open']
    writes=[op for op in m.ops if op[0]=='write']
    if rc==1:
        assert len(writes)==1 and writes[0][3]==n+28
        target=ROM+('.s0' if kind=='first' else '.s1');generation=1 if kind=='first' else 21
        assert m.files[target]==slot(new,generation,rom_id)
        recovered,data,_=recover(m.files,n);assert recovered==1 and data==new
    else:assert rc==-1 and m.read(STORE,284)==before
    if fault:assert m.fault_hit,fault
    return m,dict(case=kind,fault=fault,payload_bytes=n,rc=rc,modes=modes,operations=m.ops,
        active_preserved=kind!='first',no_handle_or_heap_leak=True,live_sram_unchanged=True)


def main():
    rows=[]
    for kind in ('same','first','absent','short','long'):
        m,row=normal_case(kind);assert row['rc']==1
        assert ('wb' not in row['modes']) if kind=='same' else ('wb' in row['modes'])
        if kind in ('short','long'):
            close=next(i for i,x in enumerate(m.ops) if x[0]=='close')
            create=next(i for i,x in enumerate(m.ops) if x[0]=='open' and x[2]=='wb');assert close<create
        rows.append(row)
    for n in (32,8192):
        m,row=normal_case(n=n);assert row['rc']==1 and 'wb' not in row['modes'];rows.append(row)
    for fault in ('reject_r+b','unsupported_update','end_fail','rewind_fail','rewind_wrong_position'):
        m,row=normal_case(fault=fault);assert row['rc']==1
        assert ('rb+' in row['modes'] and 'wb' not in row['modes']) if fault=='reject_r+b' else 'wb' in row['modes']
        rows.append(row)
    for kind,fault in (('same','end_then_close_fail'),('short','length_then_close_fail'),('same','writer_close_fail'),('absent','create_fail'),('same','short_write'),('same','zero_write'),('same','corrupt_readback')):
        m,row=normal_case(kind,fault=fault);assert row['rc']==-1,(kind,fault,row['rc'])
        if fault in ('end_then_close_fail','length_then_close_fail'):
            assert 'wb' not in row['modes'] and not any(x[0]=='write' for x in m.ops)
        if fault=='writer_close_fail':assert not any(x[0]=='read' for x in m.ops)
        if fault in ('end_then_close_fail','length_then_close_fail','short_write','zero_write'):
            rc,data,_=recover(m.files,8320);assert rc==1 and data==bytes((i*19+3)&255 for i in range(8320))
        rows.append(row)
    # Unchanged save retains the existing memory-only fast path.
    m,files,old,new,_=setup();m.write(DATA,old)
    assert m.call('save_checkpoint',STORE,DATA,len(old))==0 and not m.ops and m.files==files
    rows.append(dict(case='unchanged',rc=0,no_IO=True))
    for kind in ('same','absent'):
        for cut in (0,1,24,27,28,29,4096,8347,8348):
            m,files,old,new,_=setup(kind,cut=cut);rc=m.call('save_checkpoint',STORE,DATA,len(new))
            assert rc is None and m.power_lost and m.files[ROM+'.s0']==files[ROM+'.s0']
            rc,data,recovered=recover(m.files,len(new));assert rc==1 and data==(new if cut==8348 else old)
            rows.append(dict(case='interrupted_write_reboot',kind=kind,persisted_prefix_bytes=cut,
                active_preserved=True,recovered='complete_new_generation' if cut==8348 else 'previous_active_generation',
                no_torn_payload_loaded=True))
    result=dict(ok=True,no_USB=True,cases=rows,case_count=len(rows),source_sha256=sha(ROOT/'src/platform/save.c'),
        test_sha256=sha(Path(__file__)),bda_sha256=sha(BUILD/'H1FC.bda'),elf_sha256=sha(BUILD/'H1FC.elf'),
        build_metadata=json.loads((BUILD/'H1FC.build.json').read_text()),
        limitations=['Filesystem services and interrupted persistence modeled in RAM; no claim of physical power-loss or FTL proof.',
            'The old active slot is never touched. Partial inactive writes fail header/payload CRC; a fully persisted newer generation may load even if no close acknowledgement arrived.',
            'The existing read-slot close behavior is intentionally unchanged; this patch only changes the inactive writer open.'])
    OUT.mkdir(parents=True,exist_ok=True);(OUT/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(ok=True,cases=len(rows),bda_sha256=result['bda_sha256'])))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--build',action='store_true')
    p.add_argument('--build-dir',type=Path,help='Test an existing ordinary build without replacing dist')
    p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--toolchain',type=Path,default=Path(os.environ.get('H1_GNU_BIN',ROOT/'.tools/toolchain/bin')))
    a=p.parse_args();OUT=a.output_dir;BUILD=a.build_dir or OUT/'ordinary'
    if a.build:build(a.toolchain)
    main()
