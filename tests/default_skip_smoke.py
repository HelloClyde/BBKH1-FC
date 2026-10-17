"""Actual MIPS checks for fresh/default display settings and old-ROM options.

Uses normal firmware key events to restore display defaults. The game, config
loader and menu machine code are unmodified; initial configs have real CRCs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

from h1_mock import Machine, ROOT
from make_test_rom import make_rom

ROM = 'A:\\FC\\DEFAULT-SKIP.nes'
LOG = 'A:\\FC\\h1fc.log'


def tap(key):
    return [(9, key), (10, key)]


def config_slot(rom, skip, version=3, scale=2, sound=0, volume=55):
    mapping=bytearray(45)
    for key, value in {1:5,16:9,17:1,25:4,23:3,27:6,35:8,37:5,40:7}.items():
        mapping[key]=value
    if version==3:
        mapping[24]=9  # Preserve a deliberate custom Escape=A binding.
    payload=struct.pack('<5I',0x31474643,version,scale,skip,sound)+mapping+bytes([volume,0,0])
    header=struct.pack('<6I',0x31534748,1,zlib.crc32(rom)^len(rom),1,len(payload),zlib.crc32(payload))
    return header+struct.pack('<I',zlib.crc32(header))+payload


def latest_config(files):
    values=[files[p] for p in (ROM+'.cfg.s0',ROM+'.cfg.s1') if p in files]
    if not values:return None
    slot=max(values,key=lambda value:struct.unpack_from('<I',value,12)[0])
    assert len(slot)==96 and zlib.crc32(slot[:24])==struct.unpack_from('<I',slot,24)[0]
    assert zlib.crc32(slot[28:])==struct.unpack_from('<I',slot,20)[0]
    return slot[28:]


class Defaults(Machine):
    def __init__(self,bda,files,restore=False):
        super().__init__(bda,path=ROM,files=files)
        self.started=False
        self.actions=[(10,31)]
        if restore:
            # Main Down twice -> Display; Down twice -> Restore Defaults.
            self.actions+=tap(27)*2+tap(39)+tap(27)*2+tap(39)+tap(41)
        self.actions+=tap(37)+[(9,39)]  # Main Up -> Exit, Confirm.

    def event(self,code,key,*args):
        log=self.files.get(LOG,b'')
        paused=log.rfind(b'PAUSE_MENU_BEGIN')>log.rfind(b'PAUSE_MENU_END')
        event=(-1,-1)
        if self.blits>=4 and not self.started:
            event=(9,31);self.started=True
        elif paused:
            assert not self.audio_active and not self.audio_queue
            assert self.actions,'Menu actions exhausted before normal exit'
            event=self.actions.pop(0)
        self.word(code,event[0]);self.word(key,event[1]);return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda',type=Path,default=ROOT/'dist/H1FC-jit.bda')
    parser.add_argument('--output',type=Path,default=ROOT/'build/verification/default-skip-smoke.json')
    args=parser.parse_args();rom=make_rom();cases=[]
    def run(name,files,skip,status=1,restore=False):
        print('RUN:',name,flush=True)
        machine=Defaults(args.bda,files,restore)
        seconds=machine.run();log=machine.files[LOG].decode('gbk')
        assert re.search(rf'CONFIG_LOAD status={status} enabled=1 scale=\d+ skip={skip} sound=\d',log),log
        assert 'PAUSE_MENU_END action=2' in log and 'APP_END result=0' in log
        assert machine.started and not machine.actions
        case={'name':name,'seconds_host':round(seconds,3),'loaded_skip':skip,
              'presented_frames':machine.blits,'normal_menu_exit':True}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        (args.output.parent/(args.output.stem+'-'+name+'.log')).write_text(log,encoding='utf-8')
        cases.append(case);return machine

    fresh=run('fresh-default',{ROM:rom},1,status=0)
    # Unchanged defaults need no disk write; absence of a config is valid.
    payload=latest_config(fresh.files)
    assert payload is None or struct.unpack_from('<5I',payload)==(0x31474643,3,1,1,1)
    for skip in (0,2):
        encoded=config_slot(rom,skip)
        existing=run('v3-preserve-skip'+str(skip),{ROM:rom,ROM+'.cfg.s0':encoded},skip)
        assert latest_config(existing.files)==encoded[28:]
    legacy=run('v2-preserve-skip2',{ROM:rom,ROM+'.cfg.s0':config_slot(rom,2,version=2)},2)
    migrated=latest_config(legacy.files)
    assert struct.unpack_from('<5I',migrated)==(0x31474643,3,2,2,0)
    assert migrated[20+24]==migrated[20+41]==1 and migrated[65]==55

    original=config_slot(rom,2)
    restored=run('restore-default-display',{ROM:rom,ROM+'.cfg.s0':original},2,restore=True)
    payload=latest_config(restored.files)
    assert struct.unpack_from('<5I',payload)==(0x31474643,3,1,1,0)
    assert payload[20:]==original[28+20:], 'Display reset changed input/audio settings'
    assert b'MENU_PAGE page=4 cursor=2' in restored.files[LOG]
    reloaded=run('reload-restored-display',restored.files,1)
    assert latest_config(reloaded.files)==payload
    report={'ok':True,'kind':'Actual MIPS app/config/menu with mocked firmware',
            'bda_sha256':hashlib.sha256(args.bda.read_bytes()).hexdigest(),'cases':cases,
            'assertions':['Fresh install defaults to skip 1','Existing v3 skip 0 and 2 are preserved',
                          'Legacy v2 migration retains skip 2','Restore display resets scale/skip to 1',
                          'Display restore preserves custom inputs, sound and volume',
                          'Restored display settings persist and reload'],
            'limitation':'Normal mock input and verified settings; not physical firmware timing.'}
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
