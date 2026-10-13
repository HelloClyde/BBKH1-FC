# 开发与验证

所有普通构建、打包和自动化测试命令都在本仓库根目录执行，产物仅写入
`build/` 和 `dist/`。不依赖其他应用工作区、Windows 系统字体或私有固件。

## 固定依赖与构建

Git 克隆使用 `git submodule update --init --recursive` 取得固定的 `sdk/`
和 `third_party/fceumm/`。`tools/build.py` 要求其提交与固定值一致且工作区
干净，不会自行下载或升级依赖。FCEUmm 补丁仅应用到 `build/fceumm-h1/`。

Release 完整源码 ZIP 自带这两个依赖以及 `source-manifest.json`。无 Git 元数据
时，构建器按清单验证依赖的固定提交和每个文件的 SHA-256，再进行离线构建。
若修改了依赖，请使用 Git 工作区明确管理改动，不能绕过原始依赖检查。

已验证的工具链为 Windows `mipsel-none-elf` GCC 15.2.0。
`python tools/install_toolchain.py` 下载固定 ZIP、验证 SHA-256 后解压到
`.tools/toolchain/`；构建默认使用此目录。已有工具链可用 `--toolchain path/to/bin`
或环境变量 `H1_GNU_BIN` 指定，路径相对于当前命令的工作目录。

```console
python -m pip install -r requirements.txt
python tools/install_toolchain.py
python tools/build.py
python tools/package.py
python tests/check_icon.py --zip dist/H1FC-install.zip
```

构建版本读取 `VERSION`；BDA 时间戳默认固定，也可用 `SOURCE_DATE_EPOCH`
指定 UTC Unix 时间戳。编译路径映射、固定字体位图和图标转换使同版本构建
不包含开发机路径。字体来源、许可和可选再生成步骤见 [资源说明](../assets/README.md)。

## 不需要私有固件的测试

```console
python tests/mips_smoke.py --bda dist/H1FC.bda --menu --quick
python tests/pause_menu_smoke.py
python tools/build.py --test-frames 20
python tests/mips_smoke.py
python tests/options_smoke.py
```

MIPS 测试使用 Unicorn 执行实际 BDA 并模拟 H1 服务表。测试 ROM 由
`tests/make_test_rom.py` 生成，文件、存档和音频留在内存或 `build/`。
这些测试覆盖运行时、CPU/APU、按键、选择取消、存档 CRC、暂停和即时存档等；
不代表真机速度或完整 H1 固件兼容性。`pause_menu_smoke.py` 默认使用同一次正式
构建的 `dist/H1FC.bda` 和 `build/release/H1FC.map`。

另有主机 C 测试，需要原生 GCC，可用 `HOST_CC` 指定完整可执行文件路径：

```console
python tests/run_platform_tests.py
```

## 可选的完整 H1 固件验证

`tools/native_smoke.py` 和 `tools/classic_smoke.py` 是本地固件测试驱动，
不属于公开 CI 的前置条件。需要自行准备 H1 V1.41 固件目录、对应 `project.bin`、
支持 `bbkh1` 的 QEMU、`h1_emulator.py` 前端，以及本地固件支持脚本
`build_h1_test_image.py` 和 `read_h1_test_file.py`。这些输入未包含在应用仓库。
固件支持脚本来自 H1 GBA 开发测试环境；其所需模拟器源码和本地主机编译器
由该环境配置。不能仅凭编译成功声称通过完整固件或真机测试。

脚本不搜索开发机默认目录，必须用以下参数明确指定输入：

```console
python tools/native_smoke.py --dump path/to/H1-dump --kernel path/to/project.bin --qemu path/to/qemu-system-mipsel.exe --emulator path/to/h1_emulator.py --image-builder path/to/build_h1_test_image.py --nand-reader path/to/read_h1_test_file.py --rebuild-image
```

Windows QEMU 如需额外 DLL，可加 `--dll-dir path/to/dll-directory`。
脚本仅在 `build/` 下生成独立 NAND 副本，检查指定端口是否可用，并且只停止
自己创建的进程。默认使用端口 8813/8814；`--port`、`--gdb-port` 可调整。
已有镜像中的 BDA 不一致时必须显式 `--rebuild-image`，避免把旧镜像误当作新版本证据。
`--collect-only --nand-reader ...` 仅从已停止的私有镜像收集原诊断 ROM 的证据。

`classic_smoke.py` 还必须提供 `--rom`、`--title` 和本次 BDA 的 `--expected-sha`，
其余固件路径参数同上。每次使用新的 `--work-dir build/run-name` 保留历史证据。
`--about` 检查触摸、实体键进入关于页和返回；`--from-below` 只适用于脚本中固定
SHA-256 的作者版 From Below。其他游戏的操作结果需要逐张查看截图，不能只凭
画面有变化就判断游戏正常。游戏 ROM、音频、固件和存档均不进入安装包或源码发布。

公开验证记录以测试 BDA SHA-256 标识；完整固件模拟器和真机结果分别记录。
