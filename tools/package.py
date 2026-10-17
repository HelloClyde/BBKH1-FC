"""Package the release BDA, original diagnostic ROM, instructions and licenses."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import sys
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from make_test_rom import make_rom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', action='store_true', help='Package the diagnostic build under the same installed application name')
    parser.add_argument('--jit', action='store_true', help='Package the JIT backend as NES (H1NES.bda)')
    args = parser.parse_args()
    name = 'H1FC' + ('-jit' if args.jit else '') + ('-profile' if args.profile else '')
    release = ROOT / ('dist/' + name + '.bda')
    metadata = json.loads(release.with_suffix(".build.json").read_text())
    data = release.read_bytes()
    assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert metadata["test_frames"] == 0 and not metadata["benchmark"]
    assert bool(metadata['profile']) == args.profile and not metadata['verbose_log']
    assert bool(metadata.get('jit', False)) == args.jit
    assert metadata["version"] == (ROOT / "VERSION").read_text(encoding="ascii").strip()
    installed = 'H1NES.bda' if args.jit else 'H1FC.bda'
    public_name = ('H1NES' if args.jit else 'H1FC') + ('-profile' if args.profile else '')
    files = {
        "应用/程序/" + installed: data,
        installed + ".sha256": (metadata['sha256']+'  '+installed+'\n').encode('ascii'),
        installed.replace('.bda', '.build.json'): release.with_suffix(".build.json").read_bytes(),
        "FC/H1FC-TEST.nes": make_rom(),
        "README.md": (ROOT / "README.md").read_bytes(),
        "NOTICE.md": (ROOT / "NOTICE.md").read_bytes(),
        "assets/README.md": (ROOT / "assets/README.md").read_bytes(),
        "LICENSES/H1FC-GPL-2.0.txt": (ROOT / "LICENSE").read_bytes(),
        "LICENSES/FCEUmm-Copying.txt": (ROOT / "third_party/fceumm/Copying").read_bytes(),
        "LICENSES/FCEUmm-Authors.txt": (ROOT / "third_party/fceumm/Authors").read_bytes(),
        "LICENSES/H1SDK-Apache-2.0.txt": (ROOT / "sdk/LICENSE").read_bytes(),
        "LICENSES/H1SDK-NOTICE.txt": (ROOT / "sdk/NOTICE").read_bytes(),
    }
    for path in sorted((ROOT / "assets/licenses").glob("*")):
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in sorted((ROOT / "docs").rglob("*")):
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    if args.profile:
        files['DIAGNOSTIC.txt'] = (
            'NES v'+metadata['version']+' 诊断包（profile=1）\n'
            '解压后将“应用”和“FC”合并到设备 A 盘，覆盖 '+installed+'。\n'
            '进入发生卡顿的场景（如新游戏后的城市操作），连续运行约 30～60 秒。\n'
            '然后用 P 或触摸画面打开菜单，选择“退出游戏”，提供 A:\\FC\\h1fc.log。\n'+
            ('细分计时布局：core_detail_v1；请确认日志含 PROFILE_LAYOUT revision=core_detail_v1。\n'
             if metadata.get('profile_layout') == 'core_detail_v1' else '')+
            '本包用于定位热点；细分计时会增加开销，不代表普通版的实际速度。非诊断安装包为 '+
            ('H1NES-install.zip' if args.jit else 'H1FC-install.zip')+'。\n'
        ).encode('utf-8')
    if args.jit:
        files['使用说明.txt'] = (
            'NES v'+metadata['version']+'\n'
            '作者：HelloClyde\n'
            '将“应用”和“FC”合并到 A 盘，启动桌面上的 NES。\n'
            '选择自己的 .nes 游戏；游戏中 Escape／返回键默认为 B，用 P 或触摸画面打开暂停菜单。\n'
            '菜单内按返回可继续游戏；选择“退出游戏”才保存并退出模拟器。\n'
            '首次运行默认跳帧1（每两帧显示一帧），已有设置保留；可在显示菜单调整或恢复默认。\n'
            '从旧版升级后，可移除旧的 H1FC-JIT.bda 程序文件，避免出现重复图标。\n'
            '游戏、存档和配置继续使用原路径，无需移动。\n'
            '运行异常时正常退出，日志位于 A:\\FC\\h1fc.log。\n'
        ).encode('utf-8')
    output = ROOT / ('dist/' + public_name + '-install.zip')
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in sorted(files.items()):
            entry = ZipInfo(name, (2026, 10, 13, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, payload)
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert hashlib.sha256(archive.read("应用/程序/" + installed)).hexdigest() == metadata["sha256"]
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"PASS: {output}\nSHA256 {digest}")


if __name__ == "__main__":
    main()
