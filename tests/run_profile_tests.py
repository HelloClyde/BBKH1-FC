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
    for name, defines in [('profile_sampling_test', []),
                          ('profile_disabled_test', ['-DH1_PROFILE_DISABLED_TEST'])]:
        exe = out / (name + ('.exe' if os.name == 'nt' else ''))
        subprocess.run([gcc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        *defines, '-I', str(ROOT / 'tests/mock'),
                        str(ROOT / 'tests/profile_sampling_test.c'), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)

if __name__ == '__main__':
    main()
