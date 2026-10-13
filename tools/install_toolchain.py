"""Install the tested Windows GNU MIPS 15.2.0 toolchain with a pinned digest."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://static.grumpycoder.net/pixel/mips/g++-mipsel-none-elf-15.2.0.zip'
SHA256 = '8ba866e25c9826ee04ab4310365d264e3e73769e3738bb58ae38fd6740b7ee8d'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest',type=Path,default=ROOT/'.tools/toolchain')
    args = parser.parse_args()
    dest = args.dest.resolve()
    dest.mkdir(parents=True,exist_ok=True)
    archive = dest/'g++-mipsel-none-elf-15.2.0.zip'
    if not archive.is_file():
        print('Downloading',URL,flush=True)
        version = (ROOT/'VERSION').read_text().strip()
        request = urllib.request.Request(URL,headers={'User-Agent':f'BBKH1-FC/{version} (+https://github.com/HelloClyde/BBKH1-FC)'})
        with urllib.request.urlopen(request,timeout=120) as response, archive.with_suffix('.part').open('wb') as output:
            import shutil
            shutil.copyfileobj(response,output)
        archive.with_suffix('.part').replace(archive)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != SHA256:
        raise RuntimeError(f'Toolchain archive hash mismatch: {actual}; remove {archive} and retry')
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (dest/member.filename).resolve()
            if target != dest and dest not in target.parents:
                raise RuntimeError('Invalid toolchain archive path')
        source.extractall(dest)
    version = subprocess.check_output([str(dest/'bin/mipsel-none-elf-gcc.exe'),'--version'],text=True).splitlines()[0]
    if not version.endswith('15.2.0'):raise RuntimeError(version)
    for name in ['gcc','g++','nm','objcopy']:
        if not (dest/f'bin/mipsel-none-elf-{name}.exe').is_file():raise RuntimeError(f'Missing {name}')
    print(version,'\nH1_GNU_BIN='+str(dest/'bin'))

if __name__ == '__main__': main()
