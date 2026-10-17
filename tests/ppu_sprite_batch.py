"""Compare prepared sprite row and compositing functions with the frozen BDA source.

Runs the actual generated C bodies with deterministic arbitrary sprite planes,
palette bytes, clipping, priority, flip, sprite-zero and disabled-render states.
The final MIPS whole-core and complete-application tests remain independent.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from hotpath_equivalence import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--cc', default=os.environ.get('HOST_CC') or shutil.which('gcc') or 'C:/msys64/ucrt64/bin/gcc.exe')
    args = parser.parse_args()
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#define H_FLIP 0x40
#define SP_BACK 0x20
typedef struct { uint8_t ca[2], atr, x; } SPRB;
static uint32_t ppulut1[256], ppulut2[256];
static uint8_t PALRAM[32], SPRBUF[256], sprlinebuf[264];
static uint8_t PPU[4], PPU_status, numsprites, SpriteBlurp, sphitdata;
static unsigned h1_skip_colour_output, h1_sprite_first, h1_sprite_end;
static int spork, rendis, sphitx;
static void FCEU_dwmemset(void *d,uint32_t v,unsigned n) {
  for(unsigned i=0;i<n;i+=4) memcpy((uint8_t*)d+i,&v,4);
}
'''
    for path, prefix in ((args.baseline, 'reference'), (args.prepared, 'candidate')):
        for name in ('RefreshSprites', 'CopySprites'):
            signature = 'static void ' + name + ('(void) {' if name == 'RefreshSprites' else '(uint8_t *target) {')
            source += function(path / 'src/ppu.c', signature).replace(name + '(', prefix + '_' + name + '(')
    source += r'''
typedef struct {
 uint8_t line[264], target[256], count, blurp, hitdata, status;
 int hitx, spork;
} Result;
static uint32_t seed=0x4740113;
static uint32_t rnd(void) { seed=seed*1664525u+1013904223u;return seed; }
static Result run(unsigned which, const uint8_t *initial, const uint8_t *background,
                  unsigned count, unsigned blurp, unsigned status,
                  unsigned hitx, unsigned hitdata) {
  Result r;
  memset(&r,0,sizeof r);
  memcpy(sprlinebuf,initial,sizeof sprlinebuf);
  memcpy(r.target,background,sizeof r.target);
  numsprites=count; SpriteBlurp=blurp; PPU_status=status;
  sphitx=hitx; sphitdata=hitdata; spork=1;
  if(which) candidate_RefreshSprites(); else reference_RefreshSprites();
  if(!h1_skip_colour_output) {
    if(which) candidate_CopySprites(r.target); else reference_CopySprites(r.target);
  }
  memcpy(r.line,sprlinebuf,sizeof sprlinebuf);
  r.count=numsprites;r.blurp=SpriteBlurp;r.status=PPU_status;
  r.hitx=sphitx;r.hitdata=sphitdata;r.spork=spork;
  return r;
}
typedef struct { Result row; unsigned first,end; } Sequence;
static void step(Sequence *seq,unsigned which,unsigned iteration,const uint8_t *background) {
  Result *r=&seq->row;
  memcpy(sprlinebuf,r->line,sizeof sprlinebuf);
  memcpy(r->target,background,sizeof r->target);
  numsprites=r->count; SpriteBlurp=r->blurp; PPU_status=r->status;
  sphitx=r->hitx;sphitdata=r->hitdata;spork=r->spork;
  h1_sprite_first=seq->first;h1_sprite_end=seq->end;
  /* The pre-render frame boundary clears spork/count; state restore only
     updates emulated PPU fields, leaving the row and its metadata together. */
  if(iteration%19==0) spork=numsprites=0;
  if(PPU[1]&16) {
    if(h1_skip_colour_output) spork=0;
    else if(which) candidate_CopySprites(r->target);
    else reference_CopySprites(r->target);
  }
  sphitx=256;
  if(PPU[1]&24) { numsprites=iteration%65;SpriteBlurp=iteration&1; }
  if(PPU[1]&16) {
    if(which) candidate_RefreshSprites();else reference_RefreshSprites();
  }
  memcpy(r->line,sprlinebuf,sizeof sprlinebuf);
  r->count=numsprites;r->blurp=SpriteBlurp;r->status=PPU_status;
  r->hitx=sphitx;r->hitdata=sphitdata;r->spork=spork;
  seq->first=h1_sprite_first;seq->end=h1_sprite_end;
}
int main(void) {
  unsigned cases=0;
  uint8_t initial[264],background[256];
  for(unsigned x=0;x<256;++x) {
    for(unsigned y=0;y<8;++y) ppulut1[x]|=((x>>(7-y))&1)<<(y*4);
    ppulut2[x]=ppulut1[x]<<1;
  }
  for(unsigned iter=0;iter<200000;++iter) {
    for(unsigned i=0;i<sizeof initial;++i) initial[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof background;++i) background[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof SPRBUF;++i) SPRBUF[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof PALRAM;++i) PALRAM[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof PPU;++i) PPU[i]=rnd()>>24;
    if(iter%4==0) memset(SPRBUF,0,sizeof SPRBUF); /* All transparent. */
    if(iter%4==1) { /* Exercise every edge x and exact word boundary. */
      SPRBUF[3]=iter&255;SPRBUF[0]=255;SPRBUF[1]=255;
    }
    if(iter%4==2) for(unsigned i=3;i<sizeof SPRBUF;i+=4) SPRBUF[i]=255;
    if(iter%4==3) for(unsigned i=3;i<sizeof SPRBUF;i+=4) SPRBUF[i]=0;
    h1_skip_colour_output=iter%9==0;rendis=(rnd()>>24)&3;
    unsigned count=iter%65,blurp=(rnd()>>24)&1,status=rnd()>>24;
    unsigned hitx=iter%257,hitdata=rnd()>>24;
    Result before=run(0,initial,background,count,blurp,status,hitx,hitdata);
    Result after=run(1,initial,background,count,blurp,status,hitx,hitdata);
    if(memcmp(&before,&after,sizeof before)) { fprintf(stderr,"Mismatch at %u\n",iter);return 1; }
    ++cases;
  }
  /* Every possible byte pair, staggered across all four byte lanes. */
  for(unsigned s=0;s<256;++s) for(unsigned b=0;b<256;++b) {
    uint32_t sprite=s*0x01010101u,background=b*0x01010101u;
    uint32_t select=(~sprite&(~(sprite<<1)|(background<<1)))&0x80808080u;
    select=(select>>7)*255u;
    uint32_t got=background^((background^sprite)&select);
    unsigned expected=(!(s&128)&&(!(s&64)||(b&64)))?s:b;
    assert(got==expected*0x01010101u);
    ++cases;
  }
  Sequence old={0},current={0};
  memset(old.row.line,128,sizeof old.row.line);
  current=old;
  for(unsigned iter=0;iter<50000;++iter) {
    for(unsigned i=0;i<sizeof background;++i) background[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof SPRBUF;++i) SPRBUF[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof PALRAM;++i) PALRAM[i]=rnd()>>24;
    PPU[1]=rnd()>>24;
    if(iter%4==0) memset(SPRBUF,0,sizeof SPRBUF);
    if(iter%4==1) for(unsigned i=3;i<sizeof SPRBUF;i+=4) SPRBUF[i]=255;
    h1_skip_colour_output=iter%3==0;rendis=(rnd()>>24)&3;
    step(&old,0,iter,background);step(&current,1,iter,background);
    if(memcmp(&old.row,&current.row,sizeof old.row)) { fprintf(stderr,"Sequence mismatch at %u\n",iter);return 1; }
    ++cases;
  }
  printf("%u\n",cases);
}
'''
    output = ROOT / 'build/verification/v0113-ppu-sprite-batch'
    output.mkdir(parents=True, exist_ok=True)
    cfile, exe = output / 'sprite_batch.c', output / 'sprite_batch.exe'
    cfile.write_text(source, encoding='utf-8')
    subprocess.run([args.cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(cfile), '-o', str(exe)], check=True)
    result = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    report = {'ok': True, 'cases': int(result.stdout),
              'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'scope': 'Actual prepared RefreshSprites and CopySprites against frozen baseline; exact entire row, target, sprite-zero and flags',
              'limitation': 'Hosted arithmetic differential; MIPS whole-core and application checks are separate.'}
    (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
