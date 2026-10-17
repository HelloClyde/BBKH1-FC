"""Portable CI reference: compile pinned scalar and prepared LQ generators.

Requires only a host C compiler and the pinned core checkout; no private ROM,
historical BDA, firmware, or target toolchain is needed. Actual MIPS register/
DMA/PCM comparisons are separately covered by apu_lq_smoke.py.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from prepare_apu import prepare_apu, LIMIT


def generators(text):
    square = text[text.index('static void RDoSQLQ(void) {'):text.index('\nstatic void RDoTriangle(void)')]
    mixed = text[text.index('static void RDoTriangleNoisePCMLQ(void) {'):text.index('\nstatic void RDoNoise(void)')]
    return square + '\n' + mixed


FIELDS = 'PSG EnvUnits lengthcount curfreq RectDutyCount sqacc lq_triacc lq_noiseacc lq_tcout tristep nreg TriCount RawDALatch ChannelBC Wave PAL soundtsinc nesincsize FSettings'.split()
PREAMBLE = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define INLINE inline
typedef struct { uint8_t Speed, Mode, DecCountTo1, decvolume; int reloaddec; } ENVUNIT;
typedef struct {
 int32_t Wave[2560], lengthcount[4], curfreq[2], RectDutyCount[2], sqacc[2];
 int32_t lq_triacc, lq_noiseacc, tristep, nesincsize;
 uint32_t lq_tcout, ChannelBC[5], soundtsinc, sound_timestamp, soundtsoffs;
 uint8_t PSG[16], TriCount, RawDALatch;
 uint16_t nreg;
 int PAL;
 ENVUNIT EnvUnits[3];
 struct { int TriangleVolume, SquareVolume[2], NoiseVolume, PCMVolume; } FSettings;
} State;
static State *current;
static uint32_t wlookup1[32], wlookup2[203];
static int CheckFreq(uint32_t cf, uint8_t sr) {
 if (!(sr&8) && (((cf>>(sr&7))+cf)&0x800)) return 0;
 return 1;
}
static uint32_t wl2(uint32_t n) { return wlookup2[n<203?n:202]; }
#define SOUNDTS (current->sound_timestamp + current->soundtsoffs)
'''
MAIN = r'''
static uint32_t seed=2205032000u;
static uint32_t random_word(void) { seed=seed*1664525u+1013904223u; return seed; }
static int equal(const State *ref,const State *opt,unsigned test,unsigned chunk,const char *name) {
 if (!memcmp(ref,opt,sizeof(*ref))) return 1;
 for(unsigned i=0;i<sizeof(*ref);++i) if(((const uint8_t*)ref)[i]!=((const uint8_t*)opt)[i]) {
  printf("FAIL case=%u chunk=%u channel=%s byte=%u ref=%u opt=%u\n",test,chunk,name,i,
         ((const uint8_t*)ref)[i],((const uint8_t*)opt)[i]);break;
 }
 return 0;
}
int main(void) {
 static State ref,opt;
 static const int periods[]={0,3,4,5,7,8,15,31,255,1023,2047,2048};
 static const int phases[]={-1000000,-1,0,1,664934,664935,664936,10638960,30000000};
 static const int volumes[]={0,1,64,128,255,256};
 unsigned calls=0;
 for(unsigned i=0;i<32;++i)wlookup1[i]=(i*i*19+17)*37;
 for(unsigned i=0;i<203;++i)wlookup2[i]=(i*i*3+19)*7;
 for(unsigned test=0;test<10000;++test) {
  memset(&ref,0,sizeof(ref));
  ref.soundtsinc=65536;ref.nesincsize=(test&1)?664935:617685;
  ref.PAL=test&1;ref.TriCount=test%3;ref.tristep=test&31;
  ref.lq_tcout=(test%16)*3;ref.RawDALatch=random_word()&255;ref.nreg=random_word()&32767;
  for(unsigned i=0;i<16;++i)ref.PSG[i]=random_word()>>24;
  for(unsigned i=0;i<4;++i)ref.lengthcount[i]=(test&(1u<<i))?10:0;
  for(unsigned i=0;i<2;++i) {
   ref.curfreq[i]=periods[random_word()%12];ref.sqacc[i]=phases[random_word()%9];
   ref.RectDutyCount[i]=(int)(random_word()%32)-8;
  }
  for(unsigned i=0;i<3;++i) {
   ref.EnvUnits[i].Speed=random_word()&15;ref.EnvUnits[i].Mode=random_word()&3;
   ref.EnvUnits[i].decvolume=random_word()&15;
  }
  ref.FSettings.TriangleVolume=volumes[random_word()%6];
  ref.FSettings.SquareVolume[0]=volumes[random_word()%6];
  ref.FSettings.SquareVolume[1]=volumes[random_word()%6];
  ref.FSettings.NoiseVolume=volumes[random_word()%6];ref.FSettings.PCMVolume=volumes[random_word()%6];
  ref.lq_triacc=phases[random_word()%9];ref.lq_noiseacc=phases[random_word()%9];
  ref.ChannelBC[0]=ref.ChannelBC[2]=ref.sound_timestamp=test%64;
  for(unsigned i=0;i<2560;++i)ref.Wave[i]=(int)(random_word()%200001)-100000;
  opt=ref;
  for(unsigned chunk=0;chunk<5;++chunk) {
   ref.sound_timestamp+=random_word()%1000;opt.sound_timestamp=ref.sound_timestamp;
   current=&ref;ref_sq();current=&opt;opt_sq();
   if(!equal(&ref,&opt,test,chunk,"square"))return 1;++calls;
   current=&ref;ref_mix();current=&opt;opt_mix();
   if(!equal(&ref,&opt,test,chunk,"triangle/noise/DAC"))return 1;++calls;
   /* A write boundary flushes first; only subsequent time sees new values. */
   ref.PSG[14]=opt.PSG[14]=random_word()>>24;
   ref.RawDALatch=opt.RawDALatch=random_word()&127;
   ref.curfreq[0]=opt.curfreq[0]=periods[random_word()%12];
  }
 }
 printf("PASS cases=10000 exact_generator_calls=%u\n",calls);return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc', default=os.environ.get('HOST_CC', 'cc'))
    parser.add_argument('--output', type=Path, default=ROOT/'build/verification/apu-lq-reference.json')
    a = parser.parse_args()
    upstream = (ROOT/'third_party/fceumm/src/sound.c').read_text()
    output = ROOT/'build/apu-lq-reference'; output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as directory:
        work = Path(directory); (work/'src').mkdir()
        (work/'src/sound.c').write_text(upstream)
        prepare_apu(work)
        patched = (work/'src/sound.c').read_text()
        tables = '\n'.join(re.search(r'static const (?:int|uint32_t) '+name+r'\[.*?\n?\};', upstream, re.S).group()
                           for name in ('RectDuties', 'NTSCNoiseFreqTable', 'PALNoiseFreqTable'))
        code = PREAMBLE + tables + '\n'
        code += '\n'.join('#define '+field+' (current->'+field+')' for field in FIELDS) + '\n'
        code += '#define RDoSQLQ ref_sq\n#define RDoTriangleNoisePCMLQ ref_mix\n'+generators(upstream)
        code += '\n#undef RDoSQLQ\n#undef RDoTriangleNoisePCMLQ\n'
        code += '#define RDoSQLQ opt_sq\n#define RDoTriangleNoisePCMLQ opt_mix\n'+LIMIT+generators(patched)
        code += '\n'+'\n'.join('#undef '+field for field in FIELDS)+'\n'+MAIN
        source=output/'reference.c'; source.write_text(code)
        exe=output/('reference.exe' if os.name=='nt' else 'reference')
        subprocess.run([a.cc, '-std=c99', '-O2', '-fwrapv', str(source), '-o', str(exe)], check=True)
        result=subprocess.run([str(exe)],check=True,capture_output=True,text=True)
    report={'ok':True,'cases':10000,'exact_generator_calls':100000,
            'reference':'Pinned upstream sound.c, extracted unmodified LQ generators',
            'candidate':'Same pinned source transformed by tools/prepare_apu.py',
            'scope':'Exact full output buffer and internal oscillator state, including changing register-derived values between flushes',
            'output':result.stdout.strip()}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
