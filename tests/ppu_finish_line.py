"""Differential of actual sprite/colour bodies before and after pass fusion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from hotpath_equivalence import function
from h1_mock import ROOT


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prepared', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cc', default='C:/msys64/ucrt64/bin/gcc.exe')
    a = p.parse_args(); source = a.prepared/'src/ppu.c'
    bodies = ''.join(function(source, s) for s in (
        'static void CopySprites(uint8_t *target) {', 'static void H1_PPU_ColourLine(',
        'static inline __attribute__((always_inline)) void H1_PPU_ColourWords(',
        'static void H1_PPU_FinishLine('))
    code = r'''
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <stdio.h>
static unsigned h1_sprite_first,h1_sprite_end;
static int spork,rendis;
static uint8_t sprlinebuf[264],PPU[4];
typedef uint32_t h1_ppu_colour_word __attribute__((may_alias));
'''+bodies+r'''
static uint32_t rng=0x474014;
static uint32_t random32(void) {rng=rng*1664525u+1013904223u;return rng;}
int main(void) {
 uint32_t pixels[64], emphasis[64], expected[64], dexp[64];
 unsigned cases=0;
 for(unsigned control=0;control<256;++control)
 for(unsigned trial=0;trial<1024;++trial) {
  PPU[1]=control; h1_sprite_first=(random32()%65)*4;
  h1_sprite_end=h1_sprite_first+(random32()%((256-h1_sprite_first)/4+1))*4;
  rendis=(trial>>2)&3;
  memset(sprlinebuf,128,sizeof sprlinebuf);
  for(unsigned i=h1_sprite_first;i<h1_sprite_end;++i) sprlinebuf[i]=random32()>>24;
  for(unsigned i=0;i<64;++i) {pixels[i]=expected[i]=random32();emphasis[i]=dexp[i]=random32();}
  int old_spork=trial&1;spork=old_spork;
  unsigned skip=(trial>>1)&1;
  if(control&0x10) {if(skip)spork=0;else CopySprites((uint8_t*)expected);}
  if(!skip)H1_PPU_ColourLine((uint8_t*)expected,(uint8_t*)dexp,control);
  int result=spork;spork=old_spork;
  if(!skip)H1_PPU_FinishLine((uint8_t*)pixels,(uint8_t*)emphasis,control);
  else if(control&0x10)spork=0;
  assert(result==spork);
  assert(!memcmp(pixels,expected,sizeof pixels));assert(!memcmp(emphasis,dexp,sizeof emphasis));
  ++cases;
 }
 printf("PASS %u fused colour/sprite/skip/mask/state cases\n",cases);
}
'''
    a.output.mkdir(parents=True, exist_ok=True)
    c = a.output/'finish.c'; c.write_text(code)
    env = os.environ.copy(); env['PATH']=str(Path(a.cc).parent)+os.pathsep+env.get('PATH','')
    results = []
    for opt in ('O0','O2'):
        exe=a.output/f'finish-{opt}.exe'
        subprocess.run([a.cc,'-'+opt,'-Wall','-Wextra',str(c),'-o',str(exe)],check=True,env=env)
        run=subprocess.run([str(exe)],check=True,env=env,text=True,capture_output=True)
        print(run.stdout,end='');results.append(dict(optimization=opt,stdout=run.stdout.strip()))
    report=dict(ok=True,cases_per_build=262144,results=results,
                extracted_functions_sha256=hashlib.sha256(bodies.encode()).hexdigest(),
                scope='Exact prepared functions; all PPU masks, sprite clipping/priority/transparency, spork, render-disable, skipped frames')
    (a.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
