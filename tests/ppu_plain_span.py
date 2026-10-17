"""Compare the actual displayed-span helper with upstream tile-by-tile output.

Checks both generic and aligned helpers, their actual lookup-table builders,
all first/last boundaries, fine-X offsets and coarse-X origins, including
nametable wraps. Palette, CHR data/banks and scroll inputs change between calls
so a cache accidentally surviving a CPU synchronization boundary is detected.
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
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--cc', default=os.environ.get('HOST_CC') or shutil.which('gcc') or 'C:/msys64/ucrt64/bin/gcc.exe')
    args = parser.parse_args()
    helper = ''.join(function(args.prepared/'src/ppu.c', signature) for signature in (
        'static void makeppulut(', 'static void FCEU_BuildBgPairLUT(',
        'static uint8_t *H1_PPU_PlainSpan(', 'static uint8_t *H1_PPU_AlignedSpan('))
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
static uint16_t fceu_bg_pair_lut[256];
static uint16_t h1_bg_plane2[256];
static uint32_t h1_bg_quad_lut[1024];
static uint8_t PALRAM[32];
static uint32_t ppulut1[256], ppulut2[256], ppulut3[128];
static uint8_t ram[0x1000], chr[0x2000], *vnapage[4], *VPage[8], XOffset;
#define VRAMADR(v) (VPage[(v)>>10] + ((v)&1023))
typedef struct {uint32_t shift[2], attr, addr, written; uint32_t pixels[68];} Result;
'''
    source += helper
    source += '''
static Result reference(uint32_t s0, uint32_t s1, uint32_t at,
                        uint32_t address, int first, int end, unsigned vofs) {
    uint32_t pshift[2] = {s0,s1}, atlatch = at, RefreshAddr = address;
    uint32_t output[68]; memset(output,0xa5,sizeof output);
    uint8_t *P = (uint8_t *)output;
    for (int X1=first;X1<end;++X1) {
'''
    source += (ROOT/'third_party/fceumm/src/pputile.h').read_text(encoding='utf-8')
    source += '''
    }
    Result r; memset(&r,0,sizeof r);
    r.shift[0]=pshift[0];r.shift[1]=pshift[1];r.attr=atlatch;r.addr=RefreshAddr;
    r.written=(uint32_t)(P-(uint8_t*)output);memcpy(r.pixels,output,sizeof output);return r;
}
static Result optimized(uint32_t s0, uint32_t s1, uint32_t at,
                        uint32_t address, int first, int end, unsigned vofs) {
    Result r;memset(&r,0,sizeof r);memset(r.pixels,0xa5,sizeof r.pixels);
    r.shift[0]=s0;r.shift[1]=s1;r.attr=at;r.addr=address;
    r.written=(uint32_t)((XOffset ? H1_PPU_PlainSpan : H1_PPU_AlignedSpan)
        ((uint8_t*)r.pixels,r.shift,&r.attr,&r.addr,first,end,vofs)-(uint8_t*)r.pixels);
    return r;
}
static uint32_t seed=0x4740;
static uint32_t rnd(void) {seed=seed*1664525u+1013904223u;return seed;}
int main(void) {
    unsigned cases=0;
    makeppulut();
    for(unsigned i=0;i<sizeof ram;++i)ram[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof chr;++i)chr[i]=rnd()>>24;
    for(unsigned first=0;first<34;++first)
      for(unsigned end=first+1;end<=34;++end)
        for(unsigned xo=0;xo<8;++xo)
          for(unsigned coarse=0;coarse<32;++coarse)
            for(unsigned attr=0;attr<16;++attr) {
                if (!(cases&31)) {
                    uint8_t palette[16];for(unsigned i=0;i<16;++i)palette[i]=rnd()>>24;
                    memcpy(PALRAM,palette,16);
                    FCEU_BuildBgPairLUT(); FCEU_BuildBgPairLUT();
                    /* Independent reference also checks cached (unchanged)
                     * and rebuilt palettes, including priority marker bits. */
                    for(unsigned i=0;i<1024;++i) {
                        unsigned p=(i>>8)<<2;
                        uint32_t expected=palette[p|(i&3)] |
                            ((uint32_t)palette[p|((i>>2)&3)]<<8) |
                            ((uint32_t)palette[p|((i>>4)&3)]<<16) |
                            ((uint32_t)palette[p|((i>>6)&3)]<<24);
                        assert(h1_bg_quad_lut[i]==expected);
                    }
                    for(unsigned i=0;i<256;++i)fceu_bg_pair_lut[i]=palette[i&15]|(palette[i>>4]<<8);
                    for(unsigned i=0;i<4;++i)vnapage[i]=ram+1024*((rnd()>>27)&3);
                    for(unsigned i=0;i<8;++i)VPage[i]=chr+1024*((rnd()>>27)&7);
                    chr[rnd()&8191]=rnd()>>24;ram[rnd()&4095]=rnd()>>24;
                }
                uint32_t address=(rnd()&0x7fe0)|coarse,s0=rnd(),s1=rnd();
                unsigned vofs=(rnd()&7)|((rnd()&1)<<12);XOffset=xo;
                Result old=reference(s0,s1,attr,address,first,end,vofs);
                Result now=optimized(s0,s1,attr,address,first,end,vofs);
                assert(!memcmp(&old,&now,sizeof old));++cases;
            }
    printf("PASS %u displayed spans: pixels, shifters, attributes, address and output pointer exact\\n",cases);
}
'''
    output=ROOT/'build/verification/ppu-plain-span'
    output.mkdir(parents=True,exist_ok=True)
    c=output/'ppu-plain-span.c';c.write_text(source,encoding='utf-8')
    exe=output/('ppu-plain-span.exe' if os.name=='nt' else 'ppu-plain-span')
    environment=os.environ.copy()
    if os.name=='nt': environment['PATH']=str(Path(args.cc).resolve().parent)+os.pathsep+environment.get('PATH','')
    subprocess.run([args.cc,'-O2','-Wall','-Wextra',str(c),'-o',str(exe)],check=True,env=environment)
    subprocess.run([str(exe)],check=True,env=environment)
    report={'ok':True,'cases':2437120,'helper_sha256':hashlib.sha256(helper.encode()).hexdigest(),
            'scope':'Actual generic/aligned displayed helpers and LUT builders versus pinned upstream pputile.h',
            'boundaries':['all first/end tiles','all fine X','all coarse X','all attribute latches',
                          'horizontal nametable wrap','CHR writes/banks and palette changes between spans'],
            'limitation':'Hosted exact arithmetic test; MIPS whole-core state/audio/video differential is separate.'}
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
