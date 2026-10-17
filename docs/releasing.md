# 发布流程

维护者在 Windows / Python 3.12 环境中发布。`VERSION` 是应用版本的唯一来源，标签必须为对应的 `vMAJOR.MINOR.PATCH`。本次版本为 v0.1.16，Release 保持 prerelease。最终发布 BDA 必须通过确切二进制的 H1 固件模拟器验证；开发期间的真机 RAM 替换测试单独记录，不代表最终 BDA 已在真机安装，也不代表所有游戏兼容。

## 本地发布检查

在初始化了 `sdk/` 和 `third_party/fceumm/` 固定子模块的干净检出中执行：

```powershell
python -m pip install -r requirements.txt
python tools/install_toolchain.py
python tools/build.py
python tools/package.py
python tests/mips_smoke.py --bda dist/H1FC.bda --menu
python tests/pause_menu_smoke.py
python tests/check_icon.py --zip dist/H1FC-install.zip
python tools/verify_release.py --require-verified
python tools/package_release.py --tag v0.1.16
```

最后两步需要已提交的、与最终 BDA 完全对应的 `docs/evidence/release-v<版本>.json`，以及 `docs/release-v<版本>.md`。更改应用、图标、字体或编译选项后，重新生成最终 BDA 并验证，不能沿用旧散列的完整固件记录。完整固件测试需要维护者自己的合法固件副本；CI 不下载或携带固件、商业 ROM、私人存档。

发布记录包含版本、BDA 和 payload SHA-256、两项依赖提交、`full_firmware_tested: true`、`hardware_tested: false`、实际通过的功能检查、截图及日志的文件散列。`hardware_tested` 指最终发布 BDA，不能因其他 RAM 实验通过而改成 true。已审真机短窗口放在独立的 `physical_experiments` 中，并保留前端、profile 设置、场景和输入的限制。必需功能检查为 `launch`、`about_author`、`about_return`、`cancel_to_desktop`、`clean_exit`；`checks` 中各项必须为 true。

`tools/package_release.py` 只从已提交的 Git 对象打包完整源码，展开两个固定子模块，保存全部文件 SHA-256 至 `source-manifest.json`。源码 ZIP 包含编译需要的源码、字体、图标、许可和构建脚本，不能混入固件、游戏文件、编译成品或本地缓存。无 Git 元数据的源码 ZIP 可通过 manifest 核验依赖后构建；也可使用仓库与子模块进行构建。

## 默认包与实验包

`python tools/build.py` / `python tools/package.py` 生成默认优化 C 包，作为 Release 资产。
`--jit` 只兼容旧构建命令；实验原生后端必须显式使用 `--native-jit`，再以
`tools/package.py --jit` 打包。不要把实验 JIT、profile、benchmark 或有界自动退出包
冒充正式安装包，也不要将开发 RAM 管理器、商业 ROM、固件或设备转储收入源码 ZIP。

文档只记录已经通过的验证；`status: final_binary_validation_pending`、空 SHA 或未通过的
固件检查必须在发布前补齐，不能通过改写检查结论绕过 `--require-verified`。
公开记录使用仓库相对路径，不包含开发机绝对路径、私人存档或设备内存地址表。

## GitHub Actions

main、PR 和手动运行使用 `contents: read`，安装固定工具链和 Python 依赖后构建，执行正式 BDA 的 MIPS 测试、三槽存档及暂停测试、图标透明通道检查，并检查重复构建的 BDA 与安装 ZIP 散列。MIPS 测试模拟 H1 服务调用，不能替代完整固件或真机测试。

标签构建另行核对 VERSION 和维护者保存的最终 BDA 固件验证记录。只有构建成功后，独立的发布 job 才取得 `contents: write`。它下载同一次构建的普通 artifact ZIP 并解压，逐文件核对 `SHA256SUMS.txt`、源码清单、安装包中的 BDA、版本与 Git 提交，最后创建 prerelease，不覆盖已有 Release 资产。

最终资产位于 `dist/release/`：`H1FC.bda`、`H1FC.build.json`、版本化安装 ZIP、包含子模块的完整源码 ZIP、发布说明、验证 manifest 和 `SHA256SUMS.txt`。GitHub 自动生成的 Source code ZIP 不展开子模块；使用我们提供的 `BBKH1-FC-v<版本>-source.zip` 可直接取得对应源码。

工作流于 2026-10-13 查询官方 Release 与 `action.yml` 后固定提交：

| Action | 已核对版本 | 用到的行为 |
| --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout/releases/tag/v7.0.1) | 7.0.1 | `submodules: true`，不保存凭据 |
| [actions/setup-python](https://github.com/actions/setup-python/releases/tag/v7.0.0) | 7.0.0 | Python 3.12 |
| [actions/upload-artifact](https://github.com/actions/upload-artifact/releases/tag/v7.0.2) | 7.0.2 | `archive: true`，上传普通 ZIP artifact |
| [actions/download-artifact](https://github.com/actions/download-artifact/releases/tag/v8.0.2) | 8.0.2 | 默认解压，摘要不匹配时报错 |

Actions artifact 自身的摘要与其内部 BDA 的 SHA-256 含义不同；发布核验以最终交付文件的 `SHA256SUMS.txt` 和 `H1FC.build.json` 为准。
