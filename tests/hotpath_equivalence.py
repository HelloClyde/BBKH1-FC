"""Native differential checks of the actual prepared palette/read functions."""
from pathlib import Path
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def function(path, signature):
    text = path.read_text(encoding='utf-8')
    begin = text.index(signature)
    opening = text.index('{', begin)
    depth = 1
    end = opening+1
    while depth:
        depth += (text[end]=='{')-(text[end]=='}')
        end += 1
    return text[begin:end]


def main():
    source = r'''
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <stdio.h>
#define INLINE inline
static uint8_t PALRAM[32];
static uint16_t fceu_bg_pair_lut[256];
typedef uint8_t (*readfunc)(uint32_t);
static readfunc ARead[65536];
static uint8_t rom[65536],other[65536],*Page[32];
static struct {uint8_t DB;} X;
#define _DB X.DB
static unsigned calls;
static uint8_t CartBR(uint32_t a) {return Page[a>>11][a];}
static uint8_t custom(uint32_t a) {++calls;return X.DB^(a>>8)^a;}
'''
    upstream = ROOT/'third_party/fceumm/src'
    prepared = ROOT/'build/release/fceumm-h1/src'
    for tree,label in [(upstream,'reference'),(prepared,'optimized')]:
        source += function(tree/'ppu.c','static void FCEU_BuildBgPairLUT(void)\n{').replace('FCEU_BuildBgPairLUT',label+'_palette')
        source += function(tree/'x6502.c','static INLINE uint8_t RdMemNorm(uint32_t A) {').replace('RdMemNorm',label+'_read')
    source += r'''
static uint32_t seed=0x4740;
static uint32_t rnd(void) {seed=seed*1664525u+1013904223u;return seed;}
int main(void) {
    uint16_t expected[256];
    for(unsigned i=0;i<30000;++i) {
        /* Repeated palettes, priority OR/AND transitions, writes to any of
           sixteen background pens, unrelated sprite writes, state restore. */
        if(i%9==0) for(unsigned j=0;j<32;++j) PALRAM[j]=rnd()>>24;
        if(i%11==0) PALRAM[rnd()%32]=rnd()>>24;
        for(unsigned j=0;j<16;j+=4) PALRAM[j]|=64;
        reference_palette();memcpy(expected,fceu_bg_pair_lut,sizeof expected);
        optimized_palette();assert(!memcmp(expected,fceu_bg_pair_lut,sizeof expected));
        for(unsigned j=0;j<16;j+=4) PALRAM[j]&=63;
    }
    for(unsigned i=0;i<65536;++i) {rom[i]=rnd()>>24;other[i]=rnd()>>24;ARead[i]=i%3?CartBR:custom;}
    for(unsigned i=0;i<32;++i) Page[i]=rom;
    for(unsigned i=0;i<300000;++i) {
        unsigned address=rnd()&65535, before=calls;
        uint8_t bus=rnd()>>24;
        if(i%7==0) Page[address>>11]=Page[address>>11]==rom?other:rom;
        if(i%19==0) ARead[address]=ARead[address]==CartBR?custom:CartBR;
        X.DB=bus;uint8_t old=reference_read(address),old_bus=X.DB;
        unsigned reference_calls=calls-before;calls=before;
        X.DB=bus;uint8_t result=optimized_read(address);
        assert(old==result && old_bus==X.DB && calls-before==reference_calls);
    }
    puts("PASS exact palette values through priority/write/restore transitions and ROM/custom-handler bus side effects");
}
'''
    out=ROOT/'build/verification/hotpaths'
    out.mkdir(parents=True,exist_ok=True)
    c=out/'hotpaths.c'
    c.write_text(source,encoding='utf-8')
    gcc=os.environ.get('HOST_CC') or shutil.which('gcc')
    assert gcc, 'HOST_CC or native gcc required'
    exe=out/('hotpaths.exe' if os.name=='nt' else 'hotpaths')
    for endian in ['little','big']:
        subprocess.run([gcc,'-O2','-Wall','-Wextra',*(['-DMSB_FIRST'] if endian=='big' else []),str(c),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
    (out/'result.json').write_text(json.dumps({'ok':True,'palette_states':30000,'read_accesses':300000,
        'endians':['little','big'],'kind':'Extracted upstream versus prepared C functions'},indent=2)+'\n',encoding='utf-8')


if __name__=='__main__': main()
