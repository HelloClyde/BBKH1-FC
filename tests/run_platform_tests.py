import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
def main():
    gcc = os.environ.get('HOST_CC') or shutil.which('gcc')
    if not gcc: raise SystemExit('HOST_CC or gcc is required')
    out = ROOT / 'build/host-tests'; out.mkdir(parents=True, exist_ok=True)
    exe = out / ('platform_test.exe' if os.name == 'nt' else 'platform_test')
    subprocess.run([gcc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-I', str(ROOT / 'tests/mock'), '-I', str(ROOT / 'src'),
                    '-I', str(ROOT / 'third_party/fceumm/src/drivers/libretro/libretro-common/include'),
                    str(ROOT / 'tests/platform_test.c'), str(ROOT / 'src/platform/frontend.c'),
                    str(ROOT / 'src/platform/save.c'), str(ROOT / 'src/platform/file_selector.c'),
                    str(ROOT / 'src/platform/diagnostics.c'),
                    '-o', str(exe)], check=True)
    with tempfile.TemporaryDirectory(dir=out) as work:
        subprocess.run([str(exe)], cwd=work, check=True)

if __name__ == '__main__': main()
