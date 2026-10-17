# 开发与验证

所有普通构建、打包和自动化测试命令都在本仓库根目录执行，产物仅写入
`build/` 和 `dist/`。不依赖其他应用工作区、Windows 系统字体或私有固件。

## 固定依赖与构建

Git 克隆使用 `git submodule update --init --recursive` 取得固定的 `sdk/`
和 `third_party/fceumm/`。`tools/build.py` 要求其提交与固定值一致且工作区
干净，不会自行下载或升级依赖。FCEUmm 补丁仅应用到各构建目录的 `fceumm-h1/`，
正式、诊断、LTO 等变体使用独立的生成目录，不互相覆盖正在编译的源码。

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

## 默认执行路径与实验 JIT

v0.1.16 的普通构建对所有 ROM 关闭原生代码分发，保留优化 C 执行循环、事件调度、
PPU/APU 优化、既有严格 ROM 身份匹配的 HLE，以及不满足已验证优化条件时的保守回退。
`prepare_jit` 仍参与所有构建；这不是切回未经适配的上游核心。
旧 `--jit` 参数仅兼容旧命令，与不传该参数的默认产物相同，不会启用原生执行。

默认版保留原有缓存分配和准入生命周期，尚未省去约 9.41 MiB 的相关堆分配。
这是为了沿用已验证的事件与状态恢复边界，不应把“未执行原生块”等同于“没有缓存”。

只有 `--native-jit` 开启实验原生后端，同时加入通用纯 `EA` NOP 块拒编及编译抖动退避。
退避在连续高频缓存回收后暂用 C，保留缓存，并以间隔探测判断恢复；探测中的编译预算
只限制后续自动准入，当前时间片仍正常结束，因此不是硬实时上限，也不能保证没有断音。
没有编译抖动的慢原生代码也可能不触发退避。普通发布不依赖这套策略来选择后端。

下面涉及 `--require-jit`、原生路径计数或强制编译的测试，只适用于显式实验构建。
默认 C 版不能以这些结构性断言未通过被误判为语义失败，也不能通过关闭这些断言冒称 JIT 已覆盖。

```console
python tools/build.py --native-jit
python tools/package.py --jit
python tools/build.py --native-jit --test-frames 20
python tests/jit_smoke.py --nm .tools/toolchain/bin/mipsel-none-elf-nm.exe
python tests/jit_event_smoke.py --nm .tools/toolchain/bin/mipsel-none-elf-nm.exe
python tests/jit_addressing_smoke.py --nm .tools/toolchain/bin/mipsel-none-elf-nm.exe
python tests/jit_run_smoke.py --bda build/test-20-jit/H1FC.bda --elf build/test-20-jit/H1FC.elf --nm .tools/toolchain/bin/mipsel-none-elf-nm.exe
python tests/ppu_data_smoke.py
python tests/core_equivalence.py --baseline-bda dist/H1FC.bda --baseline-elf build/release/H1FC.elf --bda dist/H1FC-jit.bda --elf build/release-jit/H1FC.elf --nm .tools/toolchain/bin/mipsel-none-elf-nm.exe --frames 24 --sprites --require-jit --cost-census
```

`tests/jit_run_state_smoke.py` 是手动历史比较工具：它要求通过
`--baseline-bda` / `--baseline-elf` 提供独立的旧原生 JIT 构建，与当前原生构建
比较原生返回包。默认优化 C 版没有对应的原生编译入口，不能作为该工具的基线；
同一构建与自身比较也不能代替历史回归。因此自动 CI 不调用此工具，仍运行
`jit_smoke.py` 的指令语义测试、`jit_run_smoke.py` 的 C/native CPU 与序列化状态
差分及其余适用检查，不将这些结果声称为独立的历史原生返回 ABI 比较。

默认代码池 8 MiB，入口表另占 1 MiB，bank 索引另占 162 KiB，版本验证表另占 256 KiB；
`--jit-cache-mib 1/2/4/8/10` 可选择代码池容量。非默认容量附带目录后缀，例如
`build/release-jit-1m`；对照构建应加 `--no-dist`，避免替换安装文件。
`tests/jit_bank_cache_smoke.py` 在 1/4/8 MiB 三种容量下验证 bank 往返、碰撞淘汰、
链失效、epoch 回绕、缓存回收与分配失败，CI 同时运行三种容量。
实验构建输出 `dist/H1FC-jit.bda`；`tools/package.py --jit` 生成
`H1NES-install.zip`，其中程序路径为 `应用/程序/H1NES.bda`。
默认发布使用 `dist/H1FC.bda` / `H1FC-install.zip`，程序路径为
`应用/程序/H1FC.bda`；两者桌面名称均为 `NES`。
实验构建再加 `--profile`，并执行 `tools/package.py --jit --profile`，可生成
`H1NES-profile-install.zip`。默认 C 加 `--profile` 的对应名称为 `H1FC-profile-install.zip`，
使用 `tools/package.py --profile` 打包；诊断插点开销和时钟有效性必须单独核验。
历史诊断布局在构建元数据和日志中标记为 `core_detail_v1`，详见下文；诊断产物不作为普通版发布。
正常执行只允许已证明可移出检查的代码块；`jit_smoke.py` 用编译 API 的 force 参数额外覆盖通用后端，
不能把此强制测试当作生产自动准入或性能证据。整体运行与性能验收见 [JIT 设计](jit-design.md)。

`tests/jit_app_long_benchmark.py --rom <local-ROM> --frames 600 --cost-census --menu-exit --skip 1`
可对保留的上一版 release BDA/ELF 与当前包进行完整应用长场景比较。通过普通固件按键事件正常退出，
不修改程序指令；对比逐帧 LCD、核心输出 PCM、CPU/PPU 状态、实际读取的音频环形数据和存档。
从 v0.1.7 起 Escape／返回是游戏 B 键，当前安装包的完整应用测试须使用 `--menu-exit`：
在目标帧按 P 打开暂停菜单，再选择退出。该模式使用独立缓存标识，游戏输出在进入菜单前冻结，
总成本仍包含菜单及退出。未加该参数的旧 Escape 退出模式只用于复现历史构建；
`jit_dispatch_census.py` 对当前包也需使用 `--menu-exit`。
`--baseline-bda` / `--baseline-elf` 指定对照，`--baseline-only` 生成对照记录，
`--baseline-cache` 复用 ROM、配置、输入及文件哈希均匹配的记录。每 60 帧的本地截图须人工核对场景，
不能把标题音乐测试写成战斗验证。小于有声阶段的短回归要显式传 `--allow-silent`。
从 v0.1.10 起新配置默认跳帧 1。性能比较用 `--skip 0` 或 `--skip 1` 给两版加载相同的
CRC 配置文件，保证显示工作量相同；该参数进入缓存标识，不修改客体程序或运行状态。
省略 `--skip` 只保留旧版默认跳帧 0 的空文件系统重放，不能用于新版默认设置的比较。
可选 `--native-counter` 使用宿主 C 回调累计相同的 Unicorn 基本块指令量，减少 Python
逐块回调的测试耗时；需要 `HOST_CC` 或 PATH 中的 gcc，且仅支持已核对的 Unicorn 2.1.4。
先用 `tests/native_counter_smoke.py --bda ... --elf ... --nm ...` 验证相同二进制的每帧
计数、状态、PCM、LCD、启动及退出总成本与 Python 计数器完全一致。
该选项不改变客体代码，不改变缓存场景标识；不用于 `--cost-census` 的函数分项统计。

v0.1.6 另用 [三国志 II 新游戏与城市操作脚本](../tests/scenarios/README.md) 运行 1800 帧。
`--input-script` 只向固件队列发送普通按键；`--declared-state` 只读固定核心的
CPU、PPU、APU、输入和 mapper 状态表，不执行存档回调，因此不等同于完整序列化快照。
`--allow-audio-catchup` 允许应用原有的音频追赶跳帧，但必须与基线的显示帧数、追赶数、
逐帧 LCD 和 PCM 完全一致；它不会关闭显示或修改程序。两个开关都纳入缓存场景校验。
`--current-cache` 可复用哈希和场景均匹配的候选记录，避免重复执行已完成的长测试。
最终安装 BDA 的完整核心序列化差分仍由 `core_equivalence.py` 单独验证。

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

## 核心细分诊断 core_detail_v1

以下记录最初在 v0.1.12 引入的诊断布局；v0.1.14 继续使用该布局。
该布局更新本身不是一次速度优化。普通构建不应用
`prepare_profile_cpu.py`、`prepare_profile_ppu.py` 两个计时补丁；普通 BDA
在最初引入该布局时与此前 v0.1.12 逐字节一致。诊断版存在插点、判断和计时开销，
不能将其运行速度直接当作普通版的速度。

每 31 帧选取一帧，依次轮换五种模式，其余帧记为 `light`。完整周期为
155 帧，与每 2、3、6 帧显示一次的周期互质，减少固定采样相位偏差。
`control` 保留外层计时；`coarse` 划分 CPU 和 PPU；`cpu` 进一步计时
native 调用、慢事件、C 补完、APU 事件和 Mapper 回调；`ppu` 进一步计时
背景、精灵、行初始化和颜色转换；`sound` 计时声音事件与实际 LQ/Namco
合成函数。计时插点不改变声音寄存器写入时刻，也不替换 JIT 发射器和缓存逻辑。

`PROFILE_DETAIL` 及 `PROFILE_DETAIL_COST` 按模式汇总本次连续运行的所有
完整帧，不受 12 个窗口的保留上限影响。每种模式有独立分母；不得把不同
模式的 ticks 相加，也不得把抽样 PPU 时间乘以 31 当作实测总时间。
`PROFILE_DETAIL_CLASS` 再按显示/未显示帧报告均值，`clock_reads` 给出
计时读取次数。模式间的比较可以暴露测量扰动，但场景变化和系统中断也会
影响结果，因此不能把这种比较当成精确的开销扣除。
读取次数包含帧率等待中的读取，不等同于细分插点数量；启动首帧的编译开销
也会进入 control 样本。`loop_mean_us` 是已计时 scope 的合计，不包含
帧末统计等 scope 外工作，整段墙钟速度应另看 `PROFILE_RTC_RUN`。

每个 `ticks` 是扣除嵌套 scope 后的独占时间，同一模式各项可以相加。
`inclusive_ticks` 只用于细分 scope，包含子 scope；递归进入相同类别只
累计最外层一次。inclusive、外层 `core_mean_us` 与独占项不可再相加。
例如 PPU 外层会调用 CPU，CPU 会触发 PPU 更新；在 ppu 模式已开启的子
scope 之间，嵌套计时会扣除这段时间，避免同时计入 CPU 和 PPU 独占项。
coarse/cpu 模式没有背景细分探针，CPU 内触发的这部分 PPU 工作仍归 CPU
或 native，不能将粗分结果直接称为纯 CPU 指令执行成本。

分类仍有明确边界：`cpu_dispatch_fallback_sampled` 包含分发和 C 解释回退，
没有逐条 6502 指令插入计时；native 内联时钟仍归 native。`apu_finalize`
仅为帧末收尾，运行期声音事件和合成需查看 sound 模式。Namco 原生回调及
C Mapper 回调有独立 scope，其他 Mapper 从生成代码直接调用的路径仍归
native。PPU 未单独计时的回调归其调用者，不能将 PPU 类别称为纯像素绘制。

TCU 计时分辨率约 31 微秒，包含 IRQ/系统调度和测量本身的耗时；短函数
单次为零并不表示免费。发生计时或栈故障后，`valid=0` 的整段分项结果
不得用于热点结论，独立 `PROFILE_RTC_RUN` 可用于粗略运行速度观察。
建议真机进入卡顿场景后连续运行 30～60 秒，再从暂停菜单正常退出并取得
`A:\FC\h1fc.log`。保留原有跳帧、缩放和音量设置，便于对照此前日志。

`python tests/run_profile_tests.py` 用原生 GCC 验证独立计账 oracle、递归、
采样相位、窗口淘汰、暂停重启、故障和普通版空接口。
`tests/profile_detail_mips.py --help` 提供实际 MIPS 核心差分入口：指定普通
BDA/ELF 基线、诊断 BDA/ELF、nm 和本地 ROM，逐模式核对完整序列化状态、
PCM、画面及存档。该测试仅替换硬件读时钟函数为确定性 MIPS 计数器，
验证的是计时开启后的语义与 scope 平衡；真实 TCU 的正确性需另做完整
固件和真机验证，不能用其合成 ticks 得出性能结论。

## 0.1.13 新增回归入口

`tests/jit_subroutine_smoke.py` 验证栈、JSR/RTS、控制流末尾完整/部分预算及真实事件边界。
`tests/ppu_sprite_batch.py` 与 `tests/ppu_sprite_fetch_index.py` 接受 `--baseline`、
`--prepared` 两个已准备核心目录，比较实际生成的 C 函数；主机编译器用 `--cc` 指定。
`tests/ppu_cache_restore.py` 与 `tests/ppu_dynamic_oam.py` 接受普通 BDA/ELF 基线及候选，
分别检查真实状态恢复后的派生缓存和持续 `$2004` 写入的动态工作负载。
完整城市性能仍使用 `jit_app_long_benchmark.py` 的普通 release BDA、
`sgz2-newgame-city.json`、1800 帧、严格输出比较和相同跳帧设置。

## 0.1.14 新增回归入口

`tests/jit_trace_smoke.py` 包含 8 种条件分支、目标等于顺序地址、跨页、短预算和
分支附加周期残留；`tests/jit_trace_random.py --terminal-blocks` 以固定随机种子
生成 48 个控制流图，分别对 Mapper 0/19 的实际 MIPS JIT 与解释器比较。
这里的历史文件名不代表正式版启用了跨分支轨迹；当前保留原块边界。
`jit_event_smoke.py` 和 `jit_contract_smoke.py` 同时断言状态相同及必要的原生路径，
防止以新增 C 回退掩盖事件边界退化。

`tests/ppu_oam_interval.py` 比较 29 个实际 MIPS 完整帧，包含真实 OAM DMA、
帧间直接写、损坏状态文件的部分加载、复位、改变精灵高度，以及修改 OAM 后
自行卸载的未知观察者。与恢复、动态 OAM 压力测试一起检查派生索引失效。
最终产物必须在版本、日志和关于页统一后冻结，性能和专项报告均记录该 BDA 哈希。

## v0.1.16 的证据边界与回归重点

本轮选择默认 C 路径依据实际 H1 短场景对照，而不是把 Unicorn 的 MIPS 指令量当作机器周期。
真机实验保留 v0.1.12 PROFILE 前端，在暂停时替换 RAM 中的 CPU/PPU/音频/存档函数；
测量窗关闭计时开关，但编译进旧程序的包装仍在。原生开关使用同一已识别分发边界，
C 窗的 native/compile 计数不增长；ROM、场景和输入另行核验。
这与安装最终 v0.1.16 BDA 是两种证据，不能混写。精简公开数据见
[发布说明](release-v0.1.16.md)和[发布证据](evidence/release-v0.1.16.json)。

比较时分别报告模拟帧率与实际显示帧率。默认跳帧 1，音频追赶还可能减少显示；
约 60 模拟帧/秒受限速影响，不能推算 CPU 余量。死亡重生、内部暂停、演示模式和
不一致的输入会改变工作量，必须标注或排除，不能仅凭一张变化的截图认定实玩。

本轮回归重点是默认 C、原生关闭对照及实验原生三方的 CPU/PPU/APU 状态、PCM、
画面和电池数据一致；同时保留真实 IRQ/NMI/复位、PI 延迟、DMC、短预算和递归回调边界。
PPU 派生索引在写 OAM、DMA、状态恢复及未知观察者边界失效；观察者可以写入精灵行缓冲，
因此范围裁剪不得假定回调返回时 hook 仍然存在。

音频恢复按近期欠载判断追赶强度，在等待限速前观察高水位恢复；生命周期和存档恢复
不得继承过期欠载。保存仍为同步双槽事务：只对同长度备用槽尝试更新打开，
关闭失败不得再次关闭同一固件句柄或转为截断写入，成功后继续完整回读校验。
精确三国志2版本的周期保存变化判定使用已提交快照，排除 12 个已验证音乐字节，
不把即时状态加载当作已保存；写失败不推进快照，未知 ROM 仍走通用完整 CRC 判断。
原事务的 CRC 相等快速返回仍保留，构造出的完整 CRC 碰撞不会获得额外写入保证。

最终 Release 的普通 BDA 必须按同一 SHA 完成发布检查。开发中保留的原生实验、RAM 管理器、
固件、商业 ROM、私人存档及设备转储不属于发布资产；公开 CI 使用原创测试卡带和服务替身。
