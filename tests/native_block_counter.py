"""Optional host-C implementation of the same Unicorn size/4 block counter.

This only removes Python callback overhead. It never changes guest code/state.
The pinned Unicorn binding's raw hook ABI is checked before use. Exact per-frame
parity with the Python collector must be verified when changing this collector.
"""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unicorn
from unicorn import UC_HOOK_BLOCK
from unicorn.unicorn_py3.unicorn import uclib, uc_hook_h

SOURCE = r'''
#include <stdint.h>
#ifdef _WIN32
__declspec(dllexport)
#endif
void h1_count_block(void *uc, uint64_t address, uint32_t size, void *opaque) {
    (void)uc; (void)address; *(uint64_t *)opaque += size / 4;
}
'''


def application_class(base):
    assert unicorn.__version__ == '2.1.4', 'Revalidate native hook ABI for a new Unicorn version'
    root = Path(__file__).resolve().parents[1]
    output = root / 'build/native-block-counter'
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'counter.c'
    library = output / ('counter.dll' if os.name == 'nt' else 'counter.so')
    stamp = output / 'source.sha256'
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    if not library.exists() or not stamp.exists() or stamp.read_text() != digest:
        cc = os.environ.get('HOST_CC') or shutil.which('gcc')
        if not cc and os.name == 'nt' and Path('C:/msys64/ucrt64/bin/gcc.exe').is_file():
            cc = 'C:/msys64/ucrt64/bin/gcc.exe'
        assert cc, 'Native collector requires a host C compiler; omit --native-counter otherwise'
        env = os.environ.copy()
        env['PATH'] = str(Path(cc).resolve().parent) + os.pathsep + env.get('PATH', '')
        source.write_text(SOURCE, encoding='ascii')
        flags = ['-Wl,--entry,0'] if os.name == 'nt' else ['-fPIC']
        subprocess.run([cc, '-O2', '-shared', '-nostdlib', *flags,
                        str(source), '-o', str(library)], check=True, env=env)
        stamp.write_text(digest)
    dll = ctypes.CDLL(str(library))

    class NativeApplication(base):
        counter_source_sha256 = digest

        @property
        def instructions(self):
            return self.counter.value if hasattr(self, 'counter') else self._initial_instructions

        @instructions.setter
        def instructions(self, value):
            if hasattr(self, 'counter'):
                self.counter.value = value
            else:
                self._initial_instructions = value

        def __init__(self, *args, **kwargs):
            census = args[7] if len(args) > 7 else kwargs.get('census', False)
            assert not census, 'Function census uses the Python collector'
            kwargs['count_instructions'] = False
            super().__init__(*args, **kwargs)
            self.counter = ctypes.c_uint64(self.instructions)
            self.count_instructions = True
            self.counter_library = dll
            handle = uc_hook_h()
            status = uclib.uc_hook_add(self.uc._uch, ctypes.byref(handle), UC_HOOK_BLOCK,
                                      dll.h1_count_block, ctypes.byref(self.counter),
                                      ctypes.c_uint64(1), ctypes.c_uint64(0))
            assert status == 0, ('Native block hook failed', status)
            self.uc._callbacks[handle.value] = dll.h1_count_block

    return NativeApplication
