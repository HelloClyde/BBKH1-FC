"""Actual MIPS nearest rendering across frame mutations and allocation failure."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import struct

from indexed_scale_v4 import Scaler, nearest, PIXELS, EMPHASIS, PALETTE, FRAME, OUTPUT, LCD_BYTES
from pause_menu_smoke import symbol


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bda',type=Path,required=True)
    p.add_argument('--map',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    rng=random.Random(474011)
    records=[]
    for fail in (False,True):
        s=Scaler(a.bda,a.map)
        s.addresses['h1_video_release']=symbol('h1_video_release',a.map)
        m=s.machine
        if fail:
            for address, callback in list(m.callbacks.items()):
                if getattr(callback,'__name__','')=='alloc':
                    m.callbacks[address]=lambda *args: 0
        width,height,pitch,crop,mask,mode=256,240,263,1,255,1
        pixels=bytearray(rng.randrange(256) for _ in range(pitch*(height+crop)))
        emphasis=bytearray(len(pixels))
        palette=[rng.randrange(65536) for _ in range(768)]
        def draw(label):
            m.write(PIXELS,bytes(pixels));m.write(EMPHASIS,bytes(emphasis))
            m.write(PALETTE,struct.pack('<768I',*palette))
            m.write(FRAME,struct.pack('<7I',PIXELS+crop*pitch,EMPHASIS+crop*pitch,
                                    PALETTE,width,height,pitch,mask))
            # Overwrite the LCD on every call, as a menu or another app could.
            # Reuse must still copy the row; it may never trust LCD contents.
            m.write(OUTPUT-16,b'\x5a'*16+b'\xa5'*LCD_BYTES+b'\x5a'*16)
            count=s.call('scale_nes_indexed_rgb32',OUTPUT,FRAME,mode,0)
            actual=m.read(OUTPUT,LCD_BYTES)
            assert actual==nearest(pixels,emphasis,palette,width,height,pitch,crop,mask,mode,0),(fail,label)
            assert m.read(OUTPUT-16,16)==b'\x5a'*16
            assert m.read(OUTPUT+LCD_BYTES,16)==b'\x5a'*16
            records.append(dict(failed_allocation=fail,case=label,instructions=count,
                                lcd_sha256=hashlib.sha256(actual).hexdigest()))
        draw('cold');draw('identical-after-lcd-overwrite')
        for x in (0,15,16,127,128,254,255):
            pixels[(crop+height-1)*pitch+x]^=0x7f
            draw(f'last-row-byte-{x}')
        pixels[crop*pitch]^=0xff;draw('first-row-mutation')
        for row in (0,119,239):
            emphasis[(crop+row)*pitch]=7
        draw('emphasis-change');draw('emphasis-reuse')
        palette[:]=list(reversed(palette));draw('palette-change')
        mask=63;draw('pixel-mask-change')
        for changed_mode in (0,2,1):
            mode=changed_mode;draw(f'mode-{mode}')
        crop=0;draw('cropped-source-change')
        s.call('h1_video_release')
        assert not m.allocations,('unreleased render allocations',m.allocations)
        draw('after-release');s.call('h1_video_release')
        assert not m.allocations
    report=dict(ok=True,bda_sha256=hashlib.sha256(a.bda.read_bytes()).hexdigest(),
                cases=records,limitation='Actual MIPS software execution, not physical H1 timing.')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print('PASS',len(records),'frame mutation / lifecycle / allocation-failure cases')


if __name__=='__main__': main()
