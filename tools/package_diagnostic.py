"""Package the isolated, deterministic SGZ2 diagnostic without replacing NES."""
from pathlib import Path
import hashlib
import json
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/release-profile-jit-diagnostic'


def main():
    metadata = json.loads((BUILD / 'H1FC.build.json').read_text())
    bda = (BUILD / 'H1FC.bda').read_bytes()
    assert metadata['diagnostic'] and metadata['diagnostic_revision'] == 1
    assert metadata['title'] == 'NES-D' and metadata['replay_groups'] == 15
    assert metadata['sha256'] == hashlib.sha256(bda).hexdigest()
    assert metadata['log_path'] == 'A:\\FC\\nesdiag.log'
    files = {
        '应用/程序/H1NES-DIAG.bda': bda,
        'H1NES-DIAG.bda.sha256': (metadata['sha256'] + '  H1NES-DIAG.bda\n').encode(),
        'H1NES-DIAG.build.json': (BUILD / 'H1FC.build.json').read_bytes(),
        '使用说明.txt': (ROOT / 'docs/diagnostic-replay.md').read_bytes(),
        'FC/诊断日志位置.txt': '测试完成后提供本目录的 nesdiag.log。此包不包含游戏。\n'.encode('utf-8'),
        'tools/analyze_diagnostic.py': (ROOT / 'tools/analyze_diagnostic.py').read_bytes(),
        'DIAGNOSTIC-VERIFICATION.json': (ROOT / 'docs/evidence/diagnostic-replay.json').read_bytes(),
        'NOTICE.md': (ROOT / 'NOTICE.md').read_bytes(),
        'LICENSES/H1FC-GPL-2.0.txt': (ROOT / 'LICENSE').read_bytes(),
        'LICENSES/FCEUmm-Copying.txt': (ROOT / 'third_party/fceumm/Copying').read_bytes(),
        'LICENSES/FCEUmm-Authors.txt': (ROOT / 'third_party/fceumm/Authors').read_bytes(),
        'LICENSES/H1SDK-Apache-2.0.txt': (ROOT / 'sdk/LICENSE').read_bytes(),
        'LICENSES/H1SDK-NOTICE.txt': (ROOT / 'sdk/NOTICE').read_bytes(),
    }
    for p in sorted((ROOT / 'assets/licenses').glob('*')):
        if p.is_file():
            files[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    output = ROOT / 'dist/H1NES-diagnostic-install.zip'
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in sorted(files.items()):
            entry = ZipInfo(name, (2026, 10, 16, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, payload)
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert archive.read('应用/程序/H1NES-DIAG.bda') == bda
        assert not any(n.lower().endswith(('.nes', '.s0', '.s1', '.sav')) for n in archive.namelist())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(f'PASS: {output}\nSHA256 {digest}')


if __name__ == '__main__':
    main()
