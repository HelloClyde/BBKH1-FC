"""Host-only contract tests; no firmware or copyrighted ROM is required."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    gcc = os.environ.get('HOST_CC') or shutil.which('gcc')
    if not gcc:
        raise SystemExit('HOST_CC or gcc is required')
    out = ROOT / 'build/host-tests'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('profile_diag_test', 'audio_diag_test'):
        exe = out / (name + ('.exe' if os.name == 'nt' else ''))
        subprocess.run([gcc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-pointer-to-int-cast', '-Wno-int-to-pointer-cast',
                        '-I', str(ROOT / 'tests/mock'), str(ROOT / f'tests/{name}.c'),
                        '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

if __name__ == '__main__':
    main()
