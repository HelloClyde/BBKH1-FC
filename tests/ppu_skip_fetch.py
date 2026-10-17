"""Exhaust skip-span boundaries using the actual prepared plain-fetch body.

This supplements actual MIPS whole-core differential tests; it is not a frame
rate benchmark. All retained shifters, address bits, pointer advances and bytes
are checked, including the discarded-output path's untouched buffer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--cc', default=os.environ.get('HOST_CC') or shutil.which('gcc') or 'C:/msys64/ucrt64/bin/gcc.exe')
    args = parser.parse_args()
    ppu = (args.prepared/'src/ppu.c').read_text(encoding='utf-8')
    tile = (args.prepared/'src/pputile.h').read_text(encoding='utf-8')
    begin = ppu.index('\t\tint firstfetch = firsttile;')
    end = ppu.index('\n\t\tif (PEC586Hack)', begin)
    skip = ppu[begin:end]
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
static uint16_t fceu_bg_pair_lut[256];
static uint32_t ppulut1[256], ppulut2[256], ppulut3[128];
static uint8_t ram[0x1000], chr[0x4000], *vnapage[4];
static uint8_t XOffset;
static unsigned h1_skip_colour_output = 1;
static int sphitx = 0x100, PEC586Hack;
#define VRAMADR(v) (chr + ((v)&0x3fff))
typedef struct {uint32_t shift[2], attr, addr, written; uint32_t pixels[64];} Result;
'''
    for optimized in (False, True):
        name = 'optimized' if optimized else 'reference'
        source += f'''
static Result {name}(uint32_t s0, uint32_t s1, uint32_t at,
                     uint32_t address, int firsttile, int lasttile, unsigned vofs) {{
    uint32_t pshift[2] = {{s0,s1}}, atlatch = at, RefreshAddr = address;
    uint32_t output[64]; memset(output, 0xa5, sizeof output);
    uint8_t *P = (uint8_t *)output;
    int X1;
'''
        source += skip if optimized else '\tint firstfetch = firsttile;\n'
        source += '\n\tfor (X1 = firstfetch; X1 < lasttile; X1++) {\n' + tile + '\n}\n'
        source += '''
    Result r; memset(&r,0,sizeof r);
    r.shift[0]=pshift[0]; r.shift[1]=pshift[1]; r.attr=atlatch; r.addr=RefreshAddr;
    r.written=(uint32_t)(P-(uint8_t*)output); memcpy(r.pixels,output,sizeof output); return r;
}
'''
    source += r'''
static uint32_t seed=0x4740;
static uint32_t rnd(void) {seed=seed*1664525u+1013904223u;return seed;}
int main(void) {
    uint32_t cases=0;
    for(unsigned i=0;i<4;++i) vnapage[i]=ram+1024*i;
    for(unsigned i=0;i<sizeof ram;++i) ram[i]=rnd()>>24;
    for(unsigned i=0;i<sizeof chr;++i) chr[i]=rnd()>>24;
    for(unsigned first=0;first<34;++first)
        for(unsigned last=first+1;last<=34;++last)
            for(unsigned coarse=0;coarse<32;++coarse)
                for(unsigned attr=0;attr<16;++attr) {
                    /* All 15-bit scroll/address bits, including both horizontal
                     * tables and fine/coarse Y, vary independently of tile span. */
                    uint32_t addr=(rnd()&0x7fe0)|coarse;
                    uint32_t s0=rnd(),s1=rnd();
                    unsigned vofs=(rnd()&7)|((rnd()&1)<<12);
                    XOffset=rnd()&7;
                    Result old=reference(s0,s1,attr,addr,first,last,vofs);
                    Result now=optimized(s0,s1,attr,addr,first,last,vofs);
                    assert(!memcmp(&old,&now,sizeof old)); ++cases;
                }
    printf("PASS %u plain skip spans: shifters, attr, scroll, pointer and buffer exact\n",cases);
}
'''
    output = ROOT/'build/verification/ppu-skip-fetch'
    output.mkdir(parents=True,exist_ok=True)
    c = output/'ppu-skip-fetch.c'
    c.write_text(source,encoding='utf-8')
    exe = output/('ppu-skip-fetch.exe' if os.name=='nt' else 'ppu-skip-fetch')
    environment = os.environ.copy()
    if os.name == 'nt':
        environment['PATH'] = str(Path(args.cc).resolve().parent) + os.pathsep + environment.get('PATH','')
    subprocess.run([args.cc,'-O2','-Wall','-Wextra',str(c),'-o',str(exe)],check=True,env=environment)
    subprocess.run([str(exe)],check=True,env=environment)
    report = {'ok':True, 'cases':304640,
              'prepared_ppu_sha256': hashlib.sha256(ppu.encode()).hexdigest(),
              'scope':'Extracted actual skip-span code and tile fetches; all tile/coarse-X/attribute boundaries',
              'limitation':'Hosted arithmetic check; whole-core MIPS state/audio/video differential is separate.'}
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
