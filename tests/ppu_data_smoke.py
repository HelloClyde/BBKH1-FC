"""Differential checks of the exact prepared PPU tile and colour bodies.

The MIPS full-core comparison additionally covers sprite hit, scroll, mapper
callbacks and serialized timing. This test exhausts every PPU mask combination
and checks randomized tile/fine-scroll/attribute/fetch states against upstream.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from hotpath_equivalence import function

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, default=ROOT/'build/release-jit/fceumm-h1')
    parser.add_argument('--cc', default=os.environ.get('HOST_CC') or shutil.which('gcc') or 'C:/msys64/ucrt64/bin/gcc.exe')
    args = parser.parse_args()
    upstream = ROOT/'third_party/fceumm/src'
    prepared = args.prepared/'src'
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
static uint16_t fceu_bg_pair_lut[256];
static uint32_t ppulut1[256], ppulut2[256], ppulut3[128];
static uint8_t ram[0x1000], chr[0x4000], *vnapage[4];
static uint8_t XOffset;
static unsigned h1_skip_colour_output;
static int sphitx;
#define VRAMADR(v) (chr + ((v)&0x3fff))
typedef struct {uint32_t shift[2], attr, addr; unsigned written; uint8_t pixels[8];} Result;
'''
    source += function(prepared/'ppu.c', 'static void H1_PPU_ColourLine(')
    for tree, label in [(upstream, 'reference'), (prepared, 'optimized')]:
        header = (tree/'fceu-memory.h').read_text(encoding='utf-8')
        begin = header.index('#define FCEU_dwmemset')
        end = header.index('} while (0)', begin) + len('} while (0)')
        source += '\n' + header[begin:end].replace('FCEU_dwmemset', label+'_fill_macro') + '\n'
        source += f'void {label}_fill(void *out, uint32_t value, unsigned n) {{ {label}_fill_macro(out,value,n); }}\n'
        source += f'''
static Result {label}_tile(uint32_t s0, uint32_t s1, uint32_t at, uint32_t address, int X1, unsigned vofs) {{
    uint32_t pshift[2] = {{s0,s1}}, atlatch = at, RefreshAddr = address;
    uint32_t output[2] = {{0xabadcafe,0x12345678}};
    uint8_t *P = (uint8_t *)output;
'''
        source += (tree/'pputile.h').read_text(encoding='utf-8')
        source += '''
    Result r; memset(&r,0,sizeof r);
    r.shift[0]=pshift[0]; r.shift[1]=pshift[1]; r.attr=atlatch; r.addr=RefreshAddr;
    r.written=(unsigned)(P-(uint8_t*)output); memcpy(r.pixels,output,8); return r;
}
'''
    source += r'''
static uint32_t seed=0x4740;
static uint32_t rnd(void) {seed=seed*1664525u+1013904223u;return seed;}
int main(void) {
    uint32_t a[64],b[64],d[64];
    for(unsigned offset=0;offset<8;++offset) for(unsigned count=0;count<=1024;count+=4) {
        uint8_t old[1040],current[1040];
        memset(old,0xaa,sizeof old); memset(current,0xaa,sizeof current);
        uint32_t pattern=rnd();
        reference_fill(old+offset,pattern,count); optimized_fill(current+offset,pattern,count);
        assert(!memcmp(old,current,sizeof old));
    }
    for (unsigned control=0;control<256;++control) {
        for (unsigned iteration=0;iteration<256;++iteration) {
            for (unsigned x=0;x<64;++x) a[x]=b[x]=rnd();
            /* The unmodified upstream three-pass mapping, including retaining
             * bit 7 for partial emphasis and grayscale only when rendering. */
            for (unsigned x=0;x<64;++x) {
                if ((control&0x18)&&(control&1)) a[x]&=0x30303030;
                if ((control>>5)==7) a[x]=(a[x]&0x3f3f3f3f)|0xc0c0c0c0;
                else if (control&0xe0) a[x]|=0x40404040;
                else a[x]=(a[x]&0x3f3f3f3f)|0x80808080;
            }
            H1_PPU_ColourLine((uint8_t*)b,(uint8_t*)d,(uint8_t)control);
            assert(!memcmp(a,b,sizeof a));
            for(unsigned x=0;x<sizeof d;++x) assert(((uint8_t*)d)[x]==control>>5);
        }
    }
    for(unsigned x=0;x<256;++x) {
        for(unsigned y=0;y<8;++y) ppulut1[x]|=((x>>(7-y))&1)<<(y*4);
        ppulut2[x]=ppulut1[x]<<1;
    }
    for(unsigned cc=0;cc<16;++cc) for(unsigned xo=0;xo<8;++xo)
        for(unsigned pixel=0;pixel<8;++pixel)
            ppulut3[xo|(cc<<3)]|=((cc>>(((pixel+xo)/8)*2))&3)<<(2+pixel*4);
    for(unsigned i=0;i<4;++i) vnapage[i]=ram+1024*i;
    for(unsigned i=0;i<sizeof ram;++i) ram[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof chr;++i) chr[i]=rnd()>>24;
    for(unsigned i=0;i<100000;++i) {
        if(i%19==0) {
            uint8_t palette[16]; for(unsigned j=0;j<16;++j)palette[j]=rnd()>>24;
            for(unsigned j=0;j<256;++j)fceu_bg_pair_lut[j]=palette[j&15]|(palette[j>>4]<<8);
        }
        uint32_t s0=rnd(),s1=rnd(),attr=rnd()&15,addr=rnd()&0x7fff;
        unsigned tile=rnd()%34,vofs=(rnd()&7)|((rnd()&1)<<12);
        XOffset=rnd()&7; sphitx=0x100; h1_skip_colour_output=0;
        Result old=reference_tile(s0,s1,attr,addr,tile,vofs);
        Result result=optimized_tile(s0,s1,attr,addr,tile,vofs);
        assert(!memcmp(&old,&result,sizeof old));
        h1_skip_colour_output=1; sphitx=37;
        result=optimized_tile(s0,s1,attr,addr,tile,vofs);
        assert(!memcmp(&old,&result,sizeof old));
        sphitx=0x100;
        result=optimized_tile(s0,s1,attr,addr,tile,vofs);
        assert(!memcmp(&old,&result,5*sizeof(uint32_t)));
        uint32_t blank[2]={0xabadcafe,0x12345678};
        assert(!memcmp(result.pixels,blank,8));
    }
    puts("PASS 2056 alignment/fill cases / 65536 line masks / 300000 tile paths exact against upstream");
}
'''
    output = ROOT/'build/verification/ppu-data'
    output.mkdir(parents=True,exist_ok=True)
    c = output/'ppu-data.c'
    c.write_text(source,encoding='utf-8')
    exe = output/('ppu-data.exe' if os.name=='nt' else 'ppu-data')
    environment = os.environ.copy()
    if os.name == 'nt':
        environment['PATH'] = str(Path(args.cc).resolve().parent) + os.pathsep + environment.get('PATH','')
    subprocess.run([args.cc,'-O2','-Wall','-Wextra',str(c),'-o',str(exe)],check=True,env=environment)
    subprocess.run([str(exe)],check=True,env=environment)
    report = {'ok':True,'fill_cases':2056,'line_cases':65536,'tile_cases':300000,
              'scope':'Extracted actual PPU fill, tile and merged colour bodies; upstream reference',
              'prepared':str(args.prepared)}
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
