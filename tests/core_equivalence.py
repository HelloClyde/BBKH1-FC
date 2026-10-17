"""Compare actual MIPS core frames against a saved baseline BDA/ELF.

All ROMs stay local. No service table, executable instruction or game state is
patched. Callback stubs replace only the frontend, allowing deterministic core
steps without firmware pacing or framebuffer IO.
"""
import argparse
import bisect
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time
from unicorn import UC_HOOK_BLOCK, UC_HOOK_MEM_WRITE
from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_RA, UC_MIPS_REG_V0, UC_MIPS_REG_PC
from h1_mock import Machine, ROOT
from make_test_rom import make_rom


def symbols(elf, nm):
    text = subprocess.run([str(nm), '-n', str(elf)], check=True, capture_output=True, text=True).stdout
    return {name:int(address,16) for address,name in re.findall(r'^([0-9a-f]+)\s+\w\s+(\S+)$',text,re.M)}


def execute(bda, elf, nm, rom, frames, skip, cpu_census=False, cost_census=False):
    path = 'A:\\FC\\LOCAL.nes'
    m = Machine(bda, files={path:rom}, path=path)
    syms = symbols(elf,nm)
    core = struct.unpack('<19I',m.read(syms['h1_nes_core'],76))
    audio = video = b''
    counts = [0]
    running = [False]
    native_blocks = [0]
    costs={}
    if cost_census:
        listing=subprocess.run([str(nm),'-n',str(elf)],check=True,capture_output=True,text=True).stdout
        functions=[(int(address,16),name) for address,name in re.findall(r'^([0-9a-f]+)\s+[Tt]\s+(\S+)$',listing,re.M)]
        function_starts=[entry[0] for entry in functions]
    census={}
    if cpu_census:
        # FCEUmm stores the guest PC at each opcode boundary and at run exits.
        # Group by host store site so the dominant dispatch site is isolated.
        def pc_write(uc,access,address,size,value,user):
            if not running[0] or size!=2 or (address&0x1fffffff)!=((syms['X']+4)&0x1fffffff): return
            site=uc.reg_read(UC_MIPS_REG_PC)
            data=census.setdefault(site,{'total':0,'pure_rom':0,'opcodes':{},'views':{}})
            data['total']+=1
            pc=value&65535
            handler=struct.unpack('<I',m.read(syms['ARead']+pc*4,4))[0]
            ram=m.read(syms['PRGIsRAM']+(pc>>11),1)[0]
            if handler!=syms['CartBR'] or ram: return
            page=struct.unpack('<I',m.read(syms['Page']+(pc>>11)*4,4))[0]
            opcode=m.read((page+pc)&0xffffffff,1)[0]
            key=f'{opcode:02x}'
            data['pure_rom']+=1
            data['opcodes'][key]=data['opcodes'].get(key,0)+1
            key=f'{pc:04x}:{page:08x}'
            data['views'][key]=data['views'].get(key,0)+1
        pc_address=(syms['X']+4)&0x1fffffff
        m.uc.hook_add(UC_HOOK_MEM_WRITE,pc_write,begin=pc_address,end=pc_address+1)

    def block(uc,address,size,user):
        if running[0]:
            counts[0] += size//4
            if 'h1_jit_stats' in syms and 0x81000000 <= address < 0x83000000:
                native_blocks[0] += 1
            if cost_census:
                if 0x81000000<=address<0x83000000:name='[generated MIPS]'
                elif address<0x83c00020:name='[frontend stubs]'
                else:name=functions[bisect.bisect_right(function_starts,address)-1][1]
                costs[name]=costs.get(name,0)+size//4
    m.uc.hook_add(UC_HOOK_BLOCK,block)

    def call(address,*args):
        m.finished=False
        for register,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3),args):
            m.uc.reg_write(register,value)
        m.uc.reg_write(UC_MIPS_REG_RA,0x80001ff0)
        m.uc.emu_start(address,0,timeout=30_000_000,count=100_000_000)
        assert m.finished, hex(address)
        return m.uc.reg_read(UC_MIPS_REG_V0)

    def receive_audio(data,length,*args):
        nonlocal audio
        audio=m.read(data,length*4)
        return length

    def receive_video(data,width,height,pitch,*args):
        nonlocal video
        video=b''.join(m.read(data+y*pitch,width*2) for y in range(height)) if data else b''
        return 0

    def stub(address,callback):
        m.callbacks[address]=callback
        m.write(address,struct.pack('<II',0x03e00008,0))
        return address

    call(core[1],syms['environment'])
    call(core[2],stub(0x80001b00,receive_video))
    call(core[3],stub(0x80001b20,receive_audio))
    call(core[4],stub(0x80001b40,lambda *a:0))
    call(core[5],stub(0x80001b60,lambda *a:0))
    call(core[6])
    m.write(0x83000000,path.encode()+b'\0')
    m.write(0x83000040,struct.pack('<4I',0x83000000,0,0,0))
    assert call(core[8],0x83000040)==1
    call(core[11],0x83000080)
    sample_rate = struct.unpack('<d',m.read(0x83000080+32,8))[0]
    frame_rate = struct.unpack('<d',m.read(0x83000080+24,8))[0]
    if 'h1_nes_set_frameskip' in syms:
        call(syms['h1_nes_set_frameskip'],skip)
    elif skip:
        raise RuntimeError('This optimized build inlined the frameskip setter; use skip 0 for the core-only comparison')
    state_size=call(core[16])
    assert 0 < state_size < 1000000
    records=[]
    started=time.monotonic()
    for frame in range(frames):
        audio=video=b''
        before=counts[0]
        running[0]=True
        call(core[10])
        running[0]=False
        assert call(core[17],0x83200000,state_size)==1
        call(core[15])
        save_size=call(core[13],0)
        save_ptr=call(core[12],0)
        game_bytes=b''.join(m.read(syms[name],size) for name,size in
                            [('RAM',2048),('X',28),('PPU',4),('NTARAM',2048),('PALRAM',32),('SPRAM',256)])
        if save_size: game_bytes+=m.read(save_ptr,save_size)
        records.append({'frame':frame+1, 'pcm':hashlib.sha256(audio).hexdigest(),
                        'pcm_frames':len(audio)//4,
                        'video':hashlib.sha256(video).hexdigest() if video else None,
                        'state':hashlib.sha256(m.read(0x83200000,state_size)).hexdigest(),
                        'game_state':hashlib.sha256(game_bytes).hexdigest(),
                        'save_probe_status':m.read(save_ptr+6,1)[0] if save_size>6 else None,
                        'block_instructions':counts[0]-before})
    seconds=time.monotonic()-started
    call(core[7])
    assert not m.handles and not m.allocations
    result={'bda_sha256':hashlib.sha256(bda.read_bytes()).hexdigest(),
            'seconds_host':seconds,'frames':records,'sample_rate':sample_rate,
            'frame_rate':frame_rate,'block_instructions':counts[0]}
    if cpu_census:
        site=max(census,key=lambda key:census[key]['total'])
        result['cpu_census']={'host_pc_store_site':hex(site),**census[site],
                             'other_pc_store_sites':{hex(k):v['total'] for k,v in census.items() if k!=site}}
    if 'h1_jit_stats' in syms:
        result['jit_stats'] = dict(zip(('compiled','hits','misses','native_ops','deopts','rejected',
                                       'invalidations','max_words'),
                                      struct.unpack('<8I',m.read(syms['h1_jit_stats'],32))))
        result['native_block_entries'] = native_blocks[0]
        if 'h1_jit_links' in syms:
            result['native_chain_transitions']=struct.unpack('<I',m.read(syms['h1_jit_links'],4))[0]
        if 'h1_jit_schedule_stats' in syms:
            result['jit_schedule'] = dict(zip(('full_runs', 'cpu_prefixes', 'event_prefixes', 'contract_reuse'),
                                             struct.unpack('<4I',m.read(syms['h1_jit_schedule_stats'],16))))
    if cost_census:result['function_costs']=dict(sorted(costs.items(),key=lambda pair:-pair[1]))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-bda',type=Path,required=True)
    p.add_argument('--baseline-elf',type=Path,required=True)
    p.add_argument('--bda',type=Path,default=ROOT/'dist/H1FC.bda')
    p.add_argument('--elf',type=Path,default=ROOT/'build/release/H1FC.elf')
    p.add_argument('--nm',type=Path,required=True)
    p.add_argument('--rom',type=Path)
    p.add_argument('--sprites',action='store_true',help='Use the original sprite-zero/overflow fixture')
    p.add_argument('--dmc',action='store_true',help='Enable original DMC DMA fixture')
    p.add_argument('--dmc-irq',action='store_true',help='DMC IRQ variant instead of repeating DMA')
    p.add_argument('--frames',type=int,default=32)
    p.add_argument('--skip',type=int,default=0)
    p.add_argument('--game-only',action='store_true',help='Compare CPU/PPU/save/video; PCM may use a different declared rate')
    p.add_argument('--cost-census',action='store_true',help='Group MIPS block instruction entries by function')
    p.add_argument('--require-jit',action='store_true',help='Fail if no generated code executed')
    p.add_argument('--max-instruction-ratio',type=float,help='Maximum whole-run MIPS block-entry ratio to baseline')
    p.add_argument('--expect-pal',action='store_true',help='Assert the actual core AV rate is approximately 50 Hz')
    p.add_argument('--output',type=Path,default=ROOT/'build/verification/core-equivalence.json')
    a=p.parse_args()
    rom=a.rom.read_bytes() if a.rom else make_rom(sprites=a.sprites,dmc=a.dmc,dmc_irq=a.dmc_irq)
    old=execute(a.baseline_bda,a.baseline_elf,a.nm,rom,a.frames,a.skip,cost_census=a.cost_census)
    new=execute(a.bda,a.elf,a.nm,rom,a.frames,a.skip,cost_census=a.cost_census)
    if a.require_jit:assert new['jit_stats']['native_ops']>0 and new['native_block_entries']>0
    if a.expect_pal:assert 49<old['frame_rate']<51 and 49<new['frame_rate']<51
    if a.max_instruction_ratio is not None:
        assert new['block_instructions']/old['block_instructions']<=a.max_instruction_ratio
    if a.sprites:
        assert any((f['save_probe_status']&0x60)==0x60 for f in old['frames']), 'Fixture did not exercise both sprite flags'
        assert [f['save_probe_status'] for f in old['frames']]==[f['save_probe_status'] for f in new['frames']]
    for first,second in zip(old['frames'],new['frames']):
        for field in (('video','game_state') if a.game_only else ('pcm','video','state','game_state')):
            assert first[field]==second[field],(first['frame'],field,first[field],second[field])
    if a.game_only:
        assert all(f['pcm_frames']>0 for f in new['frames'])
        old_duration=sum(f['pcm_frames'] for f in old['frames'])/old['sample_rate']
        new_duration=sum(f['pcm_frames'] for f in new['frames'])/new['sample_rate']
        assert abs(old_duration-new_duration)<.003,(old_duration,new_duration)
    report={'ok':True,'kind':'Actual MIPS core differential, identical ROM and frame inputs',
            'rom_sha256':hashlib.sha256(rom).hexdigest(),'skip':a.skip,'game_only':a.game_only,'baseline':old,'current':new,
            'limitation':'Block instruction entries and host timings are not physical H1 frame rates.'}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('baseline','current')},indent=2))
    print('block instruction entries',old['block_instructions'],new['block_instructions'])


if __name__=='__main__': main()
