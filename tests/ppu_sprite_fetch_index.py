"""Exact differential of the prepared OAM index and original sprite fetches."""
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--prepared', type=Path, required=True)
    p.add_argument('--output', type=Path, default=ROOT/'build/verification/v0113-ppu-sprite-fetch-index')
    p.add_argument('--cc', default=os.environ.get('HOST_CC') or shutil.which('gcc') or 'C:/msys64/ucrt64/bin/gcc.exe')
    a = p.parse_args()
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#define V_FLIP 128
#define Sprite16 (PPU[0]&32)
typedef struct {uint8_t y,no,atr,x;} SPR;
typedef struct {uint8_t ca[2],atr,x;} SPRB;
static uint8_t PPU[4], SPRAM[256], SPRBUF[256], chr[0x4000], mmc5[0x4000];
static uint8_t numsprites,SpriteBlurp,PPU_status;
static unsigned scanline,maxsprites,MMC5Hack,geniestage;
static uint32_t h1_sprite_rows[240][2];
static uint8_t h1_sprite_oam[256];
static unsigned h1_sprite_height,h1_sprite_rows_valid;
static unsigned h1_sprite_oam_dirty = 1;
static void *InputScanlineHook, *GameHBIRQHook, *GameHBIRQHook2;
#define VRAMADR(v) (chr+((v)&0x3fff))
#define MMC5SPRVRAMADR(v) (mmc5+((v)&0x3fff))
static unsigned trace[32],traces;
static void hook(uint32_t address) {
 assert(traces<32);trace[traces++]=address;
 /* The hook path must retain its original between-plane ordering. */
 chr[(address+8)&0x3fff]^=0x33;
 mmc5[(address+8)&0x3fff]^=0x55;
 SPRAM[(address>>2)&255]^=0x99;
}
static void (*PPU_hook)(uint32_t);
'''
    helper_signature = ('static int' if 'static int H1_PPU_SpriteMask(' in
                        (a.prepared/'src/ppu.c').read_text() else 'static void')
    source += function(a.prepared/'src/ppu.c', helper_signature + ' H1_PPU_SpriteMask(')
    source += function(a.baseline/'src/ppu.c', 'static void FetchSpriteData(void) {').replace('FetchSpriteData(', 'reference(')
    source += function(a.prepared/'src/ppu.c', 'static void FetchSpriteData(void) {').replace('FetchSpriteData(', 'candidate(')
    source += r'''
typedef struct {
 uint8_t oam[256],buffer[256],count,blurp,status;
 unsigned trace[32],traces;
} Result;
static uint32_t seed=0x474013;
static uint32_t rnd(void) {seed=seed*1664525u+1013904223u;return seed;}
static Result run(unsigned which,const uint8_t *oam,const uint8_t *buffer,unsigned status) {
 Result r;memset(&r,0,sizeof r);
 memcpy(SPRAM,oam,256);memcpy(SPRBUF,buffer,256);
 for(unsigned i=0;i<sizeof chr;++i) {chr[i]=(i*33)^(i>>8);mmc5[i]=(i*19)^(i>>3);}
 numsprites=SpriteBlurp=0;PPU_status=status;traces=0;memset(trace,0,sizeof trace);
 /* The host fixture writes OAM directly rather than entering B2004 or the
    public frame boundary. Revoke that interval just as those writers do. */
 if(which) { h1_sprite_oam_dirty=1; candidate(); } else reference();
 memcpy(r.oam,SPRAM,256);memcpy(r.buffer,SPRBUF,256);memcpy(r.trace,trace,sizeof trace);
 r.count=numsprites;r.blurp=SpriteBlurp;r.status=PPU_status;r.traces=traces;
 return r;
}
int main(void) {
 uint8_t oam[256]={0},buffer[256];unsigned cases=0;
 for(unsigned i=0;i<100000;++i) {
  /* Many equal rows, then arbitrary changed OAM (DMA or state restore),
     with independent height/bank/palette fields and cache reuse. */
  if(i%11==0) for(unsigned j=0;j<256;++j) oam[j]=rnd()>>24;
  if(i%31==0) memset(oam,0,256);
  if(i%47==0) memset(oam,255,256);
  for(unsigned j=0;j<256;++j) buffer[j]=rnd()>>24;
  PPU[0]=rnd()>>24;PPU_hook=(i%7==0)?hook:0;
  scanline=i%270;maxsprites=(i%3==0)?64:8;MMC5Hack=(i/5)%2;geniestage=i%3;
  unsigned status=rnd()>>24;
  Result old=run(0,oam,buffer,status),current=run(1,oam,buffer,status);
  if(memcmp(&old,&current,sizeof old)) {fprintf(stderr,"Mismatch at %u\n",i);return 1;}
  ++cases;
 }
 printf("%u\n",cases);
}
'''
    out = a.output
    out.mkdir(parents=True,exist_ok=True)
    cfile,exe = out/'fetch.c',out/'fetch.exe'
    cfile.write_text(source,encoding='utf-8')
    environment=os.environ.copy()
    if os.name=='nt': environment['PATH']=str(Path(a.cc).resolve().parent)+os.pathsep+environment.get('PATH','')
    subprocess.run([a.cc,'-std=c11','-O2','-Wall','-Wextra','-Werror',str(cfile),'-o',str(exe)],check=True,env=environment)
    result=subprocess.run([str(exe)],check=True,text=True,capture_output=True,env=environment)
    report={'ok':True,'cases':int(result.stdout),'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
            'scope':'Actual FetchSpriteData and row-index helper: full fetched bytes, OAM, sprite-zero/overflow, all hook addresses; mutated hook planes/OAM; state-like full OAM changes',
            'limitation':'Hosted arithmetic differential only; actual MIPS and complete application measured separately.'}
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
