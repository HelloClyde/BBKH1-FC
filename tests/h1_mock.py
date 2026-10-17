"""Run the built MIPS BDA against mocked H1 tables, not a full H1 firmware.

Requires unicorn and Pillow. All guest files stay in memory. Exercises the
actual cross-compiled runtime, FC core, H1 table calls, UI, input and save path.
"""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import struct
import sys
import time
import zlib
from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
from unicorn.mips_const import *
from PIL import Image
from make_test_rom import make_rom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'sdk'))
from h1_bda.resources import PAYLOAD_OFFSET

class Machine:
    def __init__(self, bda, files=None, press_a=False, menu=False, path='A:\\FC\\H1TEST.nes', switch=False):
        self.uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.uc.mem_map(0, 64 * 1024 * 1024)
        self.uc.mem_map(0x10003000, 4096)
        self.physical_keys=set()
        self.word(0xb0003000, 1)
        self.word(0xb0003004, 1791199800)  # Valid H1 wall clock, independently settable.
        self.uc.reg_write(UC_MIPS_REG_CP0_STATUS, 0)
        self.files = dict(files) if files is not None else {path: make_rom()} if path else {}
        self.rom_path = path
        self.select_calls = []
        self.switch = switch
        self.switched = False
        self.handles, self.callbacks, self.next_handle = {}, {}, 1
        self.gui_active = False
        self.gui_opens = self.gui_closes = 0
        self.audio_initialized = set()
        self.audio_active = False
        self.audio_submissions = []
        self.audio_observed = []
        self.audio_queue = []
        self.audio_consumes = True
        self.audio_cursor = self.audio_base = 0
        self.audio_samples = 4096
        self.system_volume = 37
        self.volume_changes = []
        self.loading_frames = []
        self.menu_frames = []
        self.heap = 0x81000000
        self.allocations = set()
        self.ticks, self.blits, self.presents = 0, 0, 0
        self.screen = bytes(480 * 272 * 2)
        self.dialogs = []
        self.press_a = press_a
        self.events = []
        self.injected = False
        self.menu = menu
        self.exit_menu_started = False
        self.exit_menu_actions = [(10, 31), (9, 37), (10, 37), (9, 39)]
        self.gameplay_screen = None
        self.finished = False
        self.write(0x83c00020, bda.read_bytes()[PAYLOAD_OFFSET:])
        from mxu_model import install
        self.mxu=install(self,bda.read_bytes()[PAYLOAD_OFFSET:])
        self.uc.reg_write(UC_MIPS_REG_SP, 0x83aff000)
        self.uc.reg_write(UC_MIPS_REG_RA, 0x80001ff0)
        self.saved_regs = [UC_MIPS_REG_S0, UC_MIPS_REG_S1, UC_MIPS_REG_S2, UC_MIPS_REG_S3,
                           UC_MIPS_REG_S4, UC_MIPS_REG_S5, UC_MIPS_REG_S6, UC_MIPS_REG_S7,
                           UC_MIPS_REG_FP, UC_MIPS_REG_GP]
        for i, reg in enumerate(self.saved_regs): self.uc.reg_write(reg, 0x12340000 + i)
        self.write(0x80001ff0, struct.pack('<II', 0x03e00008, 0))
        self.uc.hook_add(UC_HOOK_CODE, self.dispatch, begin=0x80001000, end=0x80001fff)
        for slot, table in [(0x83c00004, 0x80002000), (0x83c00008, 0x80003000),
                            (0x83c00010, 0x80004000), (0x83c0000c, 0x80005000)]: self.word(slot, table)
        for table, offset, fn in [
            (0x80002000, 0x2b8, self.message), (0x80002000, 0x400, self.blit),
            (0x80002000, 0x070, self.present), (0x80002000, 0x750, self.event),
            (0x80002000, 0x9d8, self.game_key),
            (0x80002000, 0x6d8, self.clock), (0x80002000, 0x714, lambda *a: 0),
            (0x80002000, 0x718, lambda *a: 0), (0x80002000, 0x71c, self.clock),
            (0x80002000, 0x9ec, self.select_file),
            (0x80002000, 0x84c, self.gui_open), (0x80002000, 0x850, self.gui_close),
            (0x80002000, 0x8f4, self.game_buffer),
            (0x80003000, 0x000, self.fopen), (0x80003000, 0x004, self.fclose),
            (0x80003000, 0x008, self.fread), (0x80003000, 0x00c, self.fwrite),
            (0x80003000, 0x010, self.fseek), (0x80003000, 0x014, self.ftell),
            (0x80004000, 0x008, self.alloc), (0x80004000, 0x00c, self.free),
            (0x80005000, 0x50, self.pcm_init), (0x80005000, 0x54, self.pcm_destroy),
            (0x80005000, 0x58, self.pcm_open), (0x80005000, 0x5c, self.pcm_submit),
            (0x80005000, 0x60, self.pcm_start), (0x80005000, 0x64, self.pcm_stop),
            (0x80005000, 0x68, self.pcm_close),
        ]:
            stub = 0x80001000 + len(self.callbacks) * 16
            self.callbacks[stub] = fn
            self.write(stub, struct.pack('<II', 0x03e00008, 0)); self.word(table + offset, stub)
        submit_stub = struct.unpack('<I', self.read(0x8000505c, 4))[0]
        # Resolver evidence is separate from executed service stubs.
        submit_stub = 0x80001800
        self.callbacks[submit_stub] = self.pcm_submit
        self.word(0x8000505c, submit_stub)
        self.write(submit_stub, struct.pack('<II', 0x03e00008, 0))
        self.write(submit_stub + 0x70, struct.pack('<3I', 0x3c068000, 0x24c66000, 0x00c43021))
        # Native volume instruction fingerprints; services execute via mocks.
        for offset, address, callback, code in [
            (0x40,0x80001a00,self.volume_set,(0x28850065,0x24030064)),
            (0x44,0x80001a40,self.volume_get,(0x3c028000,0x03e00008,0x8c426100)),
        ]:
            self.word(0x80005000+offset,address);self.callbacks[address]=callback
            self.write(address,struct.pack('<'+'I'*len(code),*code))
    def volume_set(self, value, *a):
        assert 0<=value<=100
        self.system_volume=value;self.volume_changes.append(value)
        self.uc.reg_write(UC_MIPS_REG_PC,self.uc.reg_read(UC_MIPS_REG_RA));return 0
    def volume_get(self, *a):
        self.uc.reg_write(UC_MIPS_REG_PC,self.uc.reg_read(UC_MIPS_REG_RA));return self.system_volume
    def pcm_init(self, descriptor, *a):
        assert descriptor % 32 == 0
        pcm, size = struct.unpack('<2I', self.read(descriptor, 8))
        assert pcm % 32 == 0 and size in (8192,16384,32768)
        self.audio_samples = size//2
        self.audio_initialized.add(descriptor)
        return 1
    def pcm_destroy(self, descriptor, *a):
        assert not self.audio_active and not self.audio_queue
        self.audio_initialized.remove(descriptor)
        return 1
    def pcm_open(self, config, *a):
        assert struct.unpack('<9I', self.read(config, 36)) == (32000, 1, 4096, 0, 0, 0, 0, 0, 0)
        return 1
    def pcm_start(self, *a): self.audio_active = True; return 1
    def pcm_stop(self, *a): self.audio_active = False; return 1
    def pcm_close(self, *a): assert not self.audio_queue; return 1
    def pcm_submit(self, route, descriptor, repeats, flags, *a):
        assert route == 0 and flags == 0
        if not descriptor:
            if self.audio_queue: self.audio_observed.append(self.read(self.audio_base, self.audio_samples*2))
            self.audio_queue.clear(); self.word(0x80006000, 0); return 1
        assert self.audio_active and repeats == 0 and descriptor in self.audio_initialized
        assert descriptor not in self.audio_queue
        pcm, size = struct.unpack('<2I', self.read(descriptor, 8))
        self.audio_submissions.append(self.read(pcm, size))
        self.audio_queue.append(descriptor)
        self.audio_base = pcm; self.audio_cursor = 0
        self.word(0x80006000, 0x80006100)
        for i, pointer in enumerate(self.audio_queue):
            node = 0x80006100 + i * 24
            self.word(node, node + 24 if i + 1 < len(self.audio_queue) else 0)
            self.word(node + 4, pointer)
            self.word(node + 8, pcm)
        return 1
    def read(self, addr, n): return bytes(self.uc.mem_read(addr & 0x1fffffff, n))
    def write(self, addr, data): self.uc.mem_write(addr & 0x1fffffff, data)
    def word(self, addr, val): self.write(addr, struct.pack('<I', val & 0xffffffff))
    def string(self, addr):
        s = bytearray()
        while True:
            c = self.read(addr + len(s), 1)[0]
            if not c: return s.decode('gbk', errors='replace')
            s.append(c)
            if len(s) > 1024: raise AssertionError('unterminated guest string')
    def dispatch(self, uc, addr, size, data):
        if addr == 0x80001ff0:
            self.finished = True; uc.emu_stop(); return
        fn = self.callbacks.get(addr)
        if fn:
            args = [uc.reg_read(r) for r in [UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3]]
            args += [struct.unpack('<I', self.read(uc.reg_read(UC_MIPS_REG_SP) + 16, 4))[0]]
            rc = fn(*args)
            if fn==self.event:
                code,key=struct.unpack('<i',self.read(args[0],4))[0],struct.unpack('<i',self.read(args[1],4))[0]
                if code==9:self.physical_keys.add(key)
                if code==10:self.physical_keys.discard(key)
            uc.reg_write(UC_MIPS_REG_V0, (rc or 0) & 0xffffffff)
    def game_key(self,code,*args):
        codes=[0,16,17,18,19,20,21,22,30,31,32,33,34,35,36,104,44,45,46,47,48,49,109,57,1,28,105,108,106,23,24,25,37,38,86,106,50,103,111,28,105,1,42]
        return int(any(k<=42 and codes[k]==code for k in self.physical_keys))
    def alloc(self, n, *a):
        p = self.heap; self.heap += (n + 15) & ~15
        assert self.heap < 0x83800000
        self.allocations.add(p); return p
    def free(self, p, *a):
        assert p in self.allocations, hex(p)
        self.allocations.remove(p); return 0
    def fopen(self, path, mode, *a):
        p, m = self.string(path), self.string(mode)
        if 'w' not in m and p not in self.files: return 0
        handle = self.next_handle; self.next_handle += 1
        stream = io.BytesIO(b'' if 'w' in m else self.files[p])
        self.handles[handle] = (p, m, stream); return handle
    def fclose(self, handle, *a):
        p, m, f = self.handles.pop(handle)
        if 'w' in m or '+' in m: self.files[p] = f.getvalue()
        return 0
    def fread(self, ptr, size, count, handle, *a):
        data = self.handles[handle][2].read(size * count)
        self.write(ptr, data); return len(data)
    def fwrite(self, ptr, size, count, handle, *a):
        return self.handles[handle][2].write(self.read(ptr, size * count))
    def fseek(self, handle, off, origin, *a):
        if off & 0x80000000: off -= 0x100000000
        try: return self.handles[handle][2].seek(off, origin)
        except ValueError: return -1
    def ftell(self, handle, *a): return self.handles[handle][2].tell()
    def message(self, parent, text, title, flags, *a):
        self.dialogs.append(self.string(text)); print('DIALOG:', self.dialogs[-1], flush=True); return 0
    def blit(self, x, y, w, h, ptr):
        assert not self.gui_active, 'H1 skips the normal RGB565 path in native game mode'
        assert (x, y, w, h) == (0, 0, 480, 272)
        self.screen = self.read(ptr, w * h * 2); self.blits += 1; return 0
    def present(self, *a): self.presents += 1; return 0
    def capture_game_buffer(self):
        if self.blits:
            rgb = struct.unpack('<' + 'I' * (480 * 272), self.read(0x82000000, 480 * 272 * 4))
            self.screen = struct.pack('<' + 'H' * len(rgb), *[
                ((p >> 8) & 0xf800) | ((p >> 5) & 0x07e0) | ((p >> 3) & 0x001f) for p in rgb])
    def game_buffer(self, info, context, *a):
        assert self.gui_active and context == 0
        self.capture_game_buffer()
        self.write(info, struct.pack('<6H', 0, 480, 272, 1920, 0, 32))
        log=self.files.get('A:\\FC\\h1fc.log',b'')
        if log.rfind(b'LOADING_BEGIN')>log.rfind(b'LOADING_DONE'):
            self.loading_frames.append(self.read(0x82000000,480*272*4))
        elif log.rfind(b'PAUSE_MENU_BEGIN')>log.rfind(b'PAUSE_MENU_END'):
            assert not self.audio_active, 'Paused menu must stop PCM'
            self.menu_frames.append(self.read(0x82000000,480*272*4))
        else:
            if not self.blits and self.loading_frames:
                self.loading_frames.append(self.read(0x82000000,480*272*4))
            self.blits += 1
        return 0x82000000
    def select_file(self, directory, extension, output, *a):
        assert not self.gui_active, 'Close the native game window before choosing another ROM'
        # The synthetic M tap is released while the firmware picker is open.
        self.physical_keys.discard(36)
        self.select_calls.append((self.string(directory), self.string(extension)))
        assert self.string(extension) == 'nes'
        self.write(output, (self.rom_path.encode('gbk') if self.rom_path else b'') + b'\0')
        # Original BBVM ignores v0; success must be determined from path output.
        return 0
    def gui_open(self, *a):
        assert not self.gui_active
        self.gui_active = True; self.gui_opens += 1
        return 1
    def gui_close(self, *a):
        assert self.gui_active
        self.capture_game_buffer()
        self.gui_active = False; self.gui_closes += 1
        return 1
    def event(self, code, key, *a):
        if self.blits >= 1 and not self.injected:
            if self.press_a: self.events.append((9, 16))
            self.injected = True
        if self.menu and self.switch and self.blits >= 10 and not self.switched:
            self.events.append((9, 36)); self.switched = True
        if self.menu and self.blits >= (20 if self.switch else 10):
            log = self.files.get('A:\\FC\\h1fc.log', b'')
            paused = log.rfind(b'PAUSE_MENU_BEGIN') > log.rfind(b'PAUSE_MENU_END')
            if not self.exit_menu_started:
                # Keep the final gameplay frame for graphics assertions. The
                # real LCD correctly ends on the menu before returning home.
                self.capture_game_buffer()
                self.gameplay_screen = self.screen
                self.events.append((9, 31))
                self.exit_menu_started = True
            elif paused:
                # P opens the menu. Wait for it before releasing the
                # key, moving up to Exit (cursor 7), and confirming normally.
                assert self.exit_menu_actions, 'Exit menu did not close'
                self.events.append(self.exit_menu_actions.pop(0))
        event = self.events.pop(0) if self.events else (-1, -1)
        self.word(code, event[0]); self.word(key, event[1]); return 0
    def clock(self, *a):
        self.ticks += 1
        if self.audio_active and self.audio_queue and self.audio_consumes:
            for i in range(32):
                self.audio_observed.append(self.read(self.audio_base + ((self.audio_cursor + i) % self.audio_samples) * 2, 2))
            self.audio_cursor = (self.audio_cursor + 32) % self.audio_samples
            self.word(0x80006108, self.audio_base + self.audio_cursor * 2)
        return self.ticks
    def run(self):
        started = time.monotonic()
        # The independent Python MXU model is expensive on CI hosts; retain a
        # finite instruction limit while allowing linear-mode restore tests.
        self.uc.emu_start(0x83c00020, 0, timeout=180_000_000, count=500_000_000)
        if not self.finished:
            raise AssertionError(f'MIPS did not return, PC={self.uc.reg_read(UC_MIPS_REG_PC):08x}')
        assert self.uc.reg_read(UC_MIPS_REG_V0) == 0
        assert self.uc.reg_read(UC_MIPS_REG_SP) == 0x83aff000
        for i, reg in enumerate(self.saved_regs): assert self.uc.reg_read(reg) == 0x12340000 + i
        assert not self.handles and not self.allocations, (self.handles, self.allocations)
        assert not self.gui_active and self.gui_opens == self.gui_closes
        assert not self.audio_active and not self.audio_initialized and not self.audio_queue
        assert not self.dialogs, self.dialogs
        return time.monotonic() - started
