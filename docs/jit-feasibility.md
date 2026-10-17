# H1 FC：6502 → MIPS32 JIT 方案

调研日期：2026-10-14。v0.1.5 普通版保留解释器，另已实现独立 JIT 实验版。
本文件保留接入前的调研和路线图；实际实现以[整体设计](jit-design.md)和[验证记录](jit-experimental.md)为准。

## 参考项目及适配边界

- [beebjit](https://github.com/scarybeasts/beebjit/tree/c608a18562a50cf693ab41d4bab9cf9fb38f4401)：6502 原生 JIT，检查的提交为 `c608a18562a50cf693ab41d4bab9cf9fb38f4401`。README 和 `asm/` 实际后端为 x64、ARM64，没有 MIPS。`jit_compiler.h/.c` 的倒计时、状态修复、代码失效机制值得参考。它模拟 BBC Micro，不能直接替换 NES 核心；COPYING 为 GPLv3，本次没有复制其实现代码。
- [Dotnet6502](https://github.com/KallDrexx/Dotnet6502/tree/d610cf439d96a8ad3d8df984c247b57b6d4d96d8)：检查提交 `d610cf439d96a8ad3d8df984c247b57b6d4d96d8`。生成 .NET MSIL，依赖 CLR，不能直接在 H1 裸机应用中运行。`src/Dotnet6502.Nes/NesJitCustomizer.cs` 展示了在指令前插入 NES 周期更新的设计；其硬件模型和时序约定不等同于 FCEUmm。
- [NESRecomp](https://github.com/mstan/nesrecomp)：项目文档介绍 6502 → C 的静态重编译、按 PRG bank/CPU 地址区分编译入口以及解释器回退。它需要在电脑上为每个 ROM 生成并编译代码，属于 AOT；本次只检查其公开文档，不声称已验证生成器或 Mapper 19 游戏。此方案不能直接满足 H1 任意选择 ROM 后运行的现有流程。
- 现有 H1 GBA 平台的 `src/platform/jit.c` 提供了本机更直接的参考：对齐的缓存分配、KSEG0 地址检查、D-cache 写回后 I-cache 失效。这部分可以复用平台经验，但 ARM→MIPS 指令翻译器不能当作 6502→MIPS 翻译器。

## 实际执行统计

用 `tests/jit_census.py` 执行 v0.1.5 的真实 MIPS FCEUmm，在不改变 ROM、CPU 状态或执行代码的前提下，观察 CPU 在每个 opcode 边界写出的 PC。原始 ROM和完整统计仅保存在忽略的 `build/` 内。

输入为用户提供的 SGZ2-CN，SHA-256 为 `7b4fcd1c0be33e69943dcf9c81cca67bac634a9de2881aa2f3d8f9b6a3864117`。采样 32 帧、不开输出跳帧、无按键，结果：

| 统计 | 数值 |
| --- | --- |
| 主 opcode 边界采样 | 286,892 |
| 经只读 CartBR 取指 | 286,892 |
| 不同 PC / 映射页视图 | 1,066 |
| 仅简单立即数、传送、标志位和累加器移位指令覆盖 | 35,959，约 12.5% |
| `$FB28` / `$FB2A` 两个热点入口 | 各 46,836 次，合计约 32.7% |

本次 BDA SHA-256 为 `51d805b05c6cb834a5ca4a6b6c6eda2efe2257e8e1deb56acecbfbb3cc1a13e5`。统计的主 PC 写入地址为 `0x83c1f5e0`；对最终 ELF 反汇编确认，该 `sh` 位于 `FCEU_SoundCPUHook` 之后、opcode 分派之前，其他 CPU run 退出处的 PC 写入被按不同宿主地址分开统计。

热点所在固定 PRG 页的字节为 `A5 62 D0 FC 60`，即 `LDA $62; BNE -4; RTS`。因此，只做 LDA immediate、寄存器运算的演示 JIT 并不能覆盖主要等待循环。这个统计仅代表开场片段，不能推广到整款游戏、其他 ROM 或真实 H1 的加速比例。

## 推荐架构：短基本块原生 JIT，保留原解释器

以下为接入前的路线图。当前实验版已完成零页/寄存器/比较/分支及部分绝对读取，并接入 Mapper 19；
已验证的快速层将周期检查直接生成到原生块，按 ROM 不变约束折叠操作数，保留逐指令 APU/IRQ。

继续使用 FCEUmm 的 PPU、APU、Mapper、存档和输入，JIT 仅替换部分 CPU opcode 分派。自己编写小型 MIPS32 指令发射器，无需把 CLR、LLVM 或完整桌面 JIT 框架移植到 H1。

### 编译范围

1. 首先支持 Mapper 0 的只读 ROM 代码，用 4～16 条指令的短基本块验证运行契约。支持常见立即数运算、寄存器操作、零页 RAM 读写、条件分支；条件分支/跳转结束当前块。
2. 仅对 `ARead[PC] == CartBR` 且对应 `PRGIsRAM == 0` 的入口编译，拒绝自修改 RAM 代码、特殊取指处理器和加密 opcode 模式。跨映射页的指令必须验证所有相关页，首版可以直接回退。
3. 所有暂未支持的合法或非法 opcode 都执行原解释器，不能用 NOP 代替。PPU/APU/手柄 I/O、Mapper 寄存器写入、DMA、BRK/RTI 等先走慢路径。
4. 第二阶段再覆盖 Mapper 19，检查 Namco IRQ 与代码页切换的完整行为；通过《三国志2》CPU/PPU/PCM/存档差分后才进入实际设备实验。

### 周期和可观察状态

必须保留 FCEUmm 当前的指令级执行约定，包括：

- `X.count` 的 NTSC/PAL 比例及指令超出本次预算时的原有行为。
- `_tcount`、`timestamp`、`sound_timestamp`、分支 taken/page-cross 和其他附加周期。
- 原时点调用 `MapIRQHook(temp)`、`FCEU_SoundCPUHook(temp)`，保留 DMC DMA 引起的取数和周期变化。
- 在同一时点检查 IRQ/NMI、保留 `_PI` 的延迟行为，正确更新 `X.PC`、开放总线 `X.DB`、栈和标志位。
- 取指、操作数读、dummy read、RMW 的读/写次数和顺序。只有已确认纯 ROM 的读才可折叠为常量，并仍要正确更新总线状态。

不能为了让等待循环更快而直接跳过 N 个 CPU 周期。循环中的条件可能由 NMI/IRQ 或硬件状态改变，提前合并会改变游戏行为和 PCM。

FCEUmm 在读取 opcode 后、读取操作数前调用 Mapper/APU 钩子。如果该钩子改变映射，JIT 必须在安全点恢复到已完成取指/周期更新的正确阶段，不能从整条指令开始再次执行造成重复计时。这是接入核心时需要先建立的契约；Mapper 0 阶段能够先缩小此问题的范围。

### 缓存和失效

缓存键至少包含 CPU PC、参与取指/操作数的 PRG 页身份、映射代数与 ROM 世代。不能只按 16 位 PC 查找：Mapper 切换后同一 PC 会指向不同字节。

首版在任何 Mapper 写、handler 安装变更、ROM 切换、reset、状态恢复时保守失效或退出块。先做正确的全缓存失效，再优化细粒度失效。读写 RAM 代码暂不编译，因而无需先解决自修改代码写屏障。

H1 的总内存不是 4 MiB；SDK 的应用映像默认从 `0x83c00020` 开始，链接脚本检查的是该地址以上的静态代码、数据和栈。系统堆位于另一段可分配内存，大块堆申请不受这段静态范围的 4 MiB 数字限制。本版已把备用颜色表的 256 KiB 移到按需堆分配，O2/LTO 与 O3/LTO 都重新编译成功。

JIT 缓存建议从系统堆申请，初始 1 MiB，并支持 2/4/8 MiB 或按需要申请约 10 MiB，32 字节对齐。容量以实际分配结果和地址检查为准；采用有界块数与回收策略，退出和换游戏时释放。内存不足、编译失败或服务无法验证时自动保持原解释器运行。缓存容量更大可以减少重新编译，但不能用容量代替执行性能测试。

代码写入完成后，按 H1 已验证的平台约定完成 D-cache 写回、同步与 I-cache 失效，再进入生成代码。避免在整个块生成期间关闭 IRQ，否则可能加重音频欠载。MIPS ABI 的 callee-saved 寄存器、延迟槽、软浮点配置均需保持；不使用后续芯片的 MIPS32r2 指令。

## 验证及启用门槛

先做独立 backend，不加入本次普通安装包：

1. 用随机寄存器、边界值、页跨越和分支条件，对比每条已支持指令的寄存器、标志、DB、内存读写轨迹及周期；不支持指令确认回退。
2. 在 Mapper 0 下逐块或逐帧比较原解释器与 JIT，覆盖 sprite-zero hit、溢出、NMI/IRQ、DMC、保存/恢复。
3. 用 Mapper 19 在代码换页、IRQ、中断返回和状态恢复后重复差分，逐帧比较 CPU/PPU、保存数据和 PCM。
4. 以相同 ROM、相同输入、相同显示设置，在 H1 实机分别测解释器/JIT 的 CPU 时间、代码缓存命中、编译停顿和音频欠载。模拟器只用于正确性与固件兼容性，不能代替实机测速。

结论：H1 的 MIPS 平台具备运行生成代码的基础，存在可测的 CPU 热点；但没有找到能直接接入当前 FCEUmm 的成熟 6502→MIPS 后端。推荐从零页读写和条件分支的短块 JIT 开始，保留逐指令硬件检查。具体加速幅度要等 backend 原型和真实设备对比，不能从 opcode 覆盖率推导。
