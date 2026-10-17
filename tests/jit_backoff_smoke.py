"""Actual MIPS policy, compiler-budget, retry, and slice selection checks."""
from pathlib import Path
import argparse,json,struct,sys,hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests')]
from jit_smoke import Core
ARGS=None
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
MASK=0xffffffff
NAMES=['disabled','emitted','initialized','mode','cooldown','next_delay','probe_frames','stable_frames','active_frames','last_epoch','last_wraps','last_compiles','last_emitted','have_wrap','wrap_frame','wrap_compiles','wrap_emitted','probe_compiles','probe_emitted','trips','probes','last_invalidations','frame_active','cheap_streak','recovery_left','budget_active','budget_hit','frame_compiles','frame_emitted','frame_native_ops','frame_wraps','frame_invalidations','frame_epoch']
class Policy:
 def __init__(self):
  self.c=Core(ARGS.bda,ARGS.elf,ARGS.nm)
  self.addr=self.c.s['h1_jit_backoff_state']
 def get(self):return dict(zip(NAMES,struct.unpack('<33I',self.c.m.read(self.addr,132))))
 def set(self,**kw):
  for k,v in kw.items():self.c.m.word(self.addr+4*NAMES.index(k),v&MASK)
 def word(self,n):return struct.unpack('<I',self.c.m.read(self.c.s[n],4))[0]
 def write(self,n,v):self.c.m.word(self.c.s[n],v&MASK)
 def stats(self):return list(struct.unpack('<8I',self.c.m.read(self.c.s['h1_jit_stats'],32)))
 def begin(self):self.c.call(self.c.s['h1_nes_jit_frame_begin'])
 def end(self):self.c.call(self.c.s['h1_nes_jit_frame_end']);return self.get()
 def frame(self,compiled=0,emitted=0,wraps=0,invalidations=None,native=0):
  self.begin();a=self.stats();a[0]=(a[0]+compiled)&MASK;a[3]=(a[3]+native)&MASK
  a[6]=(a[6]+(wraps if invalidations is None else invalidations))&MASK
  self.c.m.write(self.c.s['h1_jit_stats'],struct.pack('<8I',*a))
  self.write('h1_jit_wraps',self.word('h1_jit_wraps')+wraps)
  self.write('h1_jit_epoch',self.word('h1_jit_epoch')+(wraps if invalidations is None else invalidations))
  self.set(emitted=self.get()['emitted']+emitted);return self.end()
 def sample(self,s):
  self.begin();a=self.stats();a[0]=s['jit'][0];a[3]=s['jit'][3];a[6]=s['jit'][6]
  self.c.m.write(self.c.s['h1_jit_stats'],struct.pack('<8I',*a));self.write('h1_jit_wraps',s['wraps'])
  self.write('h1_jit_epoch',100+a[6]);self.set(emitted=s['emitted']);return self.end()
 def reset(self):self.c.m.write(self.addr,bytes(132));self.frame()
 def enter(self):
  self.reset();self.frame(256,8388608,1);self.frame(256,8388608,1)
  assert self.get()['mode']==1 and self.get()['cooldown']==120,self.get()
 def probe(self):
  while self.get()['mode']==1:self.frame()
  assert self.get()['mode']==2 and not self.get()['disabled']
 def entry(self,pc):return self.c.m.read(self.word('h1_jit_entries')+pc*16,16)
 def compile(self,pc,force=0):
  page=struct.unpack('<I',self.c.m.read(self.c.s['Page']+(pc>>11)*4,4))[0]
  self.c.prg(pc,b'\xe8'*32);self.c.call(self.c.s['h1_nes_jit_compile'],pc,0xe8,page,force)
 def close(self):self.c.close();assert self.c.m.read(self.addr,132)==bytes(132)

def main():
 global ARGS
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--bda',type=Path,default=ROOT/'build/release-jit/H1FC.bda')
 ap.add_argument('--elf',type=Path,default=ROOT/'build/release-jit/H1FC.elf')
 ap.add_argument('--nm',type=Path,required=True)
 ap.add_argument('--output',type=Path,default=ROOT/'build/verification/jit-backoff.json')
 ARGS=ap.parse_args();p=Policy();rows=[]
 # The real compiler's negative entry must suppress repeated decode work.
 pc=0x9000;page=struct.unpack('<I',p.c.m.read(p.c.s['Page']+(pc>>11)*4,4))[0]
 p.c.prg(pc,b'\xea'*32);before=p.stats()
 p.c.call(p.c.s['h1_nes_jit_compile'],pc,0xea,page,0)
 rejected=p.entry(pc);stats=p.stats()
 assert rejected[:4]==bytes(4) and rejected[13]==255
 assert stats[5]==before[5]+1 and stats[0]==before[0]
 for _ in range(20):p.c.run(pc,b'\xea'*32,[0,0,0,0xfd,4,4],bytes(2048),cycles=7)
 assert p.entry(pc)==rejected and p.stats()[5]==stats[5]
 p.c.call(p.c.s['h1_nes_jit_compile'],pc,0xea,page,1)
 assert p.entry(pc)[:4]!=bytes(4)
 rows.append('legal all-EA rejects once through ordinary dispatch; force validates emitter')
 p.reset();before=p.get();p.end();assert p.get()==before;rows.append('unpaired end is inert')
 p.enter();cache=[p.word(x) for x in ('cache','cache_bytes','used_bytes','h1_jit_entries','h1_jit_epoch')]
 for f in range(119):assert p.frame()['mode']==1
 assert p.frame()['mode']==2 and not p.get()['disabled']
 assert cache==[p.word(x) for x in ('cache','cache_bytes','used_bytes','h1_jit_entries','h1_jit_epoch')]
 # Eight isolated cheap frames are required; 30 entire C frames between each.
 for probe in range(8):
  p.frame(native=1);assert p.get()['cheap_streak']==probe+1
  if probe<7:
   assert p.get()['mode']==1 and p.get()['cooldown']==30
   for f in range(29):assert p.frame()['disabled']==1
   assert p.frame()['mode']==2 and not p.get()['disabled']
 assert p.get()['mode']==0 and p.get()['recovery_left']==120
 for f in range(120):p.frame(native=1)
 assert p.get()['next_delay']==120 and p.get()['recovery_left']==0
 rows.append('eight positive one-frame probes separated by30 C frames restoreRUN;120 sensitive recovery frames resetdelay')
 p.enter();delays=[120]
 for expected in (240,480,960,1800,1800):
  p.probe();p.frame(16,65536,native=1)
  assert p.get()['mode']==1 and p.get()['cooldown']==expected,p.get();delays.append(expected)
 rows.append(dict(case='one expensive probe returns immediately to exponential cooldown',delays=delays))
 p.enter()
 for f in range(120):p.frame(invalidations=1)
 assert p.get()['mode']==2
 p.frame(native=1,invalidations=1)
 assert p.get()['mode']==1 and p.get()['cooldown']==30 and p.get()['cheap_streak']==0
 for f in range(30):p.frame(invalidations=1)
 assert p.get()['mode']==2
 rows.append('external invalidation cannot extendprobe orprevent30-frame recovery;invalidprobe isnotcheap')
 p.frame();assert p.get()['mode']==1 and not p.get()['cheap_streak'];rows.append('zero native work is not cheap evidence')
 p.enter();p.probe();p.frame(native=1);p.probe();p.frame();assert not p.get()['cheap_streak']
 rows.append('empty probe clears prior cheap streak')
 for dc,db,want in ((15,65536,0),(16,65535,0),(16,65536,1)):
  p.enter();p.probe();p.frame(dc,db,native=1)
  assert p.get()['cooldown']==(240 if want else 30)
 rows.append('both16compiles AND65536bytes required, exactthresholds covered')
 p.enter();p.probe();p.begin();p.set(budget_hit=1,disabled=1);p.c.call(p.c.s['h1_nes_jit_invalidate']);p.end()
 assert p.get()['mode']==1 and p.get()['cooldown']==240 and p.get()['budget_hit']==1
 rows.append('latchedbudget survives same-frameexternalinvalidate and cannot countcheap')
 p.reset();p.frame(256,8388608,1);p.frame(256,8388608,1,invalidations=2)
 assert not p.get()['disabled'] and not p.get()['have_wrap'];rows.append('wrap plus external invalidation rebases')
 p.reset();p.frame(512,16777216,2);assert p.get()['disabled'];rows.append('multiplewrap sameframe triggers')
 p.reset();p.frame(256,8388608,1)
 for f in range(121):p.frame(native=1)
 p.frame(256,8388608,1);assert not p.get()['disabled'];rows.append('widelyseparatedwraps notthrash')
 p.reset();p.frame(127,8388608,1);p.frame(127,8388608,1);assert not p.get()['disabled'];rows.append('initialcompile threshold128')
 p.reset();p.frame(256,8388608,1);p.frame(256,4194303,1);assert not p.get()['disabled'];rows.append('initialbyte thresholdcapacityhalf')
 a=p.stats();a[0]=0xffffff00;a[3]=0xfffffff0;a[6]=0xfffffffe
 p.c.m.write(p.c.s['h1_jit_stats'],struct.pack('<8I',*a));p.write('h1_jit_wraps',0xfffffffe);p.write('h1_jit_epoch',0xfffffffe)
 p.c.m.write(p.addr,bytes(132));p.set(emitted=0xffff0000);p.frame()
 p.frame(256,8388608,1,native=32);p.frame(256,8388608,1,native=32)
 assert p.get()['disabled'];rows.append('unsigned rollover counters remain valid')
 p.enter();p.probe();p.begin();p.write('h1_jit_native_depth',1);before=p.get();p.end();assert before==p.get()
 p.write('h1_jit_native_depth',0);p.end();assert p.get()['mode']==1
 rows.append('nonzero native depth retains pendingframe until safeend')
 # Real successful compiler calls: budget crossing, all later automatic calls
 # preserve complete entry metadata and counters; force1 still executes.
 p.enter();p.probe();p.begin();calls=0;start=p.get()['emitted'];compiled=p.stats()[0]
 while not p.get()['disabled']:
  p.compile(0x9000+calls*32);calls+=1;assert calls<100
 hit=p.get();assert hit['budget_hit'] and hit['mode']==2
 assert p.stats()[0]-compiled>=16 and hit['emitted']-start>=65536
 assert p.entry(0x9000+(calls-1)*32)[:4]!=bytes(4)
 pc=0xa000;before=p.entry(pc);stats=p.stats();used=p.word('used_bytes')
 for f in range(3):p.compile(pc)
 assert p.entry(pc)==before and p.stats()==stats and p.word('used_bytes')==used
 # Pre-existing hot or negative metadata must remain untouched too.
 hot=struct.pack('<III4B',0,0x12345678,0x1234,0xea,8,0,0)
 p.c.m.write(p.word('h1_jit_entries')+pc*16,hot);p.compile(pc)
 assert p.entry(pc)==hot
 force_before=p.stats()[0];p.compile(pc,1)
 assert p.stats()[0]==force_before+1 and p.entry(pc)[:4]!=bytes(4) and p.get()['disabled']
 rows.append(dict(case='real compiler stops futureautomaticcalls beforebind/heat,force bypasses withoutreset',calls=calls,generated_bytes=hit['emitted']-start,overshoot_bytes=hit['emitted']-start-65536))
 # Selection is nextslice, not re-entry/unwind of existing native code.
 native_before=p.c.native;stats=p.stats();bank=p.c.m.read(p.c.s['h1_jit_bank_stats'],32)
 p.c.run(0x9000,b'\xea'*32,[0,0,0,0xfd,4,4],bytes(2048),cycles=49)
 assert p.c.native==native_before and p.stats()==stats and p.c.m.read(p.c.s['h1_jit_bank_stats'],32)==bank
 p.end();assert p.get()['mode']==1 and p.get()['cooldown']==240
 p.probe();p.begin();p.compile(0xa040);assert p.entry(0xa040)[:4]!=bytes(4) and not p.get()['disabled'];p.end()
 rows.append('nextslice C leaves native/tablecountersunchanged, subsequent probe cancompile previouslyunvisitedentry')
 p.reset();p.begin()
 for i in range(calls+2):p.compile(0xa100+i*32)
 assert not p.get()['disabled'] and not p.get()['budget_active'];p.end()
 rows.append('ordinary RUN beforethrash has no64KiB compiler cap')
 # Recovery has the same compiler gate; begin refreshes per-frame budgets.
 p.reset();p.set(recovery_left=120);p.begin()
 for i in range(calls):p.compile(0xb000+i*32)
 assert p.get()['disabled'] and p.get()['budget_hit'];p.end();assert p.get()['mode']==1
 rows.append('restoredRUN sensitive120-frame window uses samecompilerbudget')
 p.close();rows.append('actualclose clears all132bytes')
 report=dict(ok=True,no_USB=True,actual_MIPS=True,policy_symbol='h1_jit_backoff_state',policy_bytes=132,field_names=NAMES,cases=rows,ELF_sha256=sha(ARGS.elf),script_sha256=sha(__file__),limits=['64KiB AND16 is a successful compile work threshold, not a hard byte or wall-time cap.','Current CPU slice/native or recursive call completes normally; no forced stack unwinding.','At most one probe frame followed by at least30 Cframes; a single frame may still underload hardware audio.','Captured counters establish admission behavior, not physicalspeed.'])
 ARGS.output.parent.mkdir(parents=True,exist_ok=True)
 ARGS.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
if __name__=='__main__':main()
