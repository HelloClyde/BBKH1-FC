"""Compare the prepared N163 mixer with the pinned upstream on identical states."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def mixer(path, name):
    text = path.read_text(encoding="utf-8")
    begin = text.index("static void DoNamcoSound(int32_t *WaveBuf, int Count) {")
    end = text.index("\nstatic void Mapper19_StateRestore", begin)
    return text[begin:end].replace("DoNamcoSound(", name + "(", 1)


def main():
    sys.path.insert(0, str(ROOT / "tools"))
    from prepare_core import prepare
    prepared = prepare(ROOT / "third_party/fceumm", ROOT / "build/fceumm-h1")
    out = ROOT / "build/verification/n163"
    out.mkdir(parents=True, exist_ok=True)
    source = r'''
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#include <time.h>
static uint8_t IRAM[128];
static uint32_t FreqCache[8], EnvCache[8], LengthCache[8], PlayIndex[8];
static int32_t vcount[8];
static struct { unsigned SndRate; } FSettings;
#define SND_N163 0
/* A deterministic nonlinear lookup exercises all amplitude transitions.
   Both mixers use this same lookup; production uses GetExpOutput unchanged. */
static uint32_t GetExpOutput(unsigned id, uint32_t x) {
    (void)id; return x * 17 + (x*x % 101);
}
'''
    source += mixer(ROOT / "third_party/fceumm/src/boards/n106.c", "reference")
    source += mixer(prepared / "src/boards/n106.c", "optimized")
    source += r'''
static uint32_t seed=0x195163;
static uint32_t rnd(void) { seed=seed*1664525u+1013904223u; return seed; }
int main(void) {
    int32_t a[700],b[700],saved_v[8],ref_v[8];
    uint32_t saved_i[8],ref_i[8];
    unsigned cases=0; uint64_t samples=0;
    const unsigned rates[]={22050,32000,44100,48000,96000};
    const unsigned lengths[]={0,1,2,15,16,17,31,32,33,532,640};
    for (unsigned k=0;k<2200;++k) {
        for (unsigned i=0;i<128;++i) IRAM[i]=rnd()>>24;
        IRAM[0x7f]=(IRAM[0x7f]&15)|((k%8)<<4);
        FSettings.SndRate=rates[k%5];
        for (unsigned p=0;p<8;++p) {
            FreqCache[p]=(rnd()%262144u);
            if (k%13==0) FreqCache[p]=0;
            if (k%17==0) FreqCache[p]=1+p; /* int32 inc overflow cases */
            LengthCache[p]=4*((rnd()%64)+1);
            EnvCache[p]=(rnd()%16)<<16;
            vcount[p]=rnd()%1000000u;
            PlayIndex[p]=rnd()%LengthCache[p];
        }
        for (unsigned n=0;n<sizeof lengths/sizeof *lengths;++n) {
            unsigned count=lengths[n];
            for (unsigned i=0;i<700;++i) a[i]=b[i]=(int32_t)(rnd()%2000)-1000;
            memcpy(saved_v,vcount,sizeof saved_v); memcpy(saved_i,PlayIndex,sizeof saved_i);
            reference(a,count);
            memcpy(ref_v,vcount,sizeof ref_v); memcpy(ref_i,PlayIndex,sizeof ref_i);
            memcpy(vcount,saved_v,sizeof saved_v); memcpy(PlayIndex,saved_i,sizeof saved_i);
            optimized(b,count);
            assert(!memcmp(a,b,sizeof a));
            assert(!memcmp(vcount,ref_v,sizeof ref_v));
            assert(!memcmp(PlayIndex,ref_i,sizeof ref_i));
            ++cases; samples+=count;
        }
    }
    /* Informational native timing; never presented as H1 frame rate. */
    memset(IRAM,0xff,sizeof IRAM); IRAM[0x7f]=0x7f;FSettings.SndRate=32000;
    for(unsigned p=0;p<8;++p) {FreqCache[p]=1000+p*100;LengthCache[p]=128;EnvCache[p]=15<<16;}
    clock_t begin=clock();
    for(unsigned k=0;k<2000;++k) {memset(a,0,sizeof a);reference(a,532);}
    double old_ms=1000.0*(clock()-begin)/CLOCKS_PER_SEC;
    begin=clock();
    for(unsigned k=0;k<2000;++k) {memset(a,0,sizeof a);optimized(a,532);}
    double new_ms=1000.0*(clock()-begin)/CLOCKS_PER_SEC;
    printf("{\"ok\":true,\"cases\":%u,\"sample_frames\":%llu,\"reference_host_ms\":%.3f,\"optimized_host_ms\":%.3f}\n",
           cases,(unsigned long long)samples,old_ms,new_ms);
}
'''
    c = out / "mix.c"
    c.write_text(source, encoding="utf-8")
    gcc = os.environ.get("HOST_CC") or shutil.which("gcc")
    if not gcc:
        raise SystemExit("HOST_CC or gcc required")
    exe = out / ("mix.exe" if os.name == "nt" else "mix")
    subprocess.run([gcc, "-O2", "-fwrapv", "-Wall", "-Wextra", str(c), "-o", str(exe)], check=True)
    result = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    report["kind"] = "Native differential test, pinned upstream versus prepared mixer"
    report["assertions"] = ["Exact PCM including guard samples", "Exact phase counters and wavetable indices",
                            "1..8 channels, all rates, zero frequency, inc overflow, split callbacks"]
    (out / "result.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
