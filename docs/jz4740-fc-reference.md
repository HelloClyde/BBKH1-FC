# JZ4740 FC 模拟器源码对比

调研日期：2026-10-14。对比对象为 H1-FC 本地 v0.1.4 开发树；本次只调研，没有改动运行代码或发布新安装包。

## 实际检查的源码

| 项目 | 固定版本 | 适用范围 |
| --- | --- | --- |
| [FCEU320 / poweravr](https://github.com/poweravr/fceux/tree/6e4da0b45fbff14fc374c5da38a84463cdabe634) | `6e4da0b45fbff14fc374c5da38a84463cdabe634` | README 标明 OpenDingux 移植，含 Dingoo 前端；同包的 legacy 二进制明确不适用于 A320 legacy Dingux |
| [RetroFW FCEUX](https://github.com/retrofw/fceux/tree/b03647e1cfdaf11789b48b7451c3b78610465e81) | `b03647e1cfdaf11789b48b7451c3b78610465e81` | 后续君正掌机参考，不能当作 JZ4740 真机测速证据 |
| [FCEUX for retrogame](https://github.com/plrguez/fceux-for-retrogame/tree/fbeaad2ddcdae3563c1093b08668f8b8f4bd4e2e) | `fbeaad2ddcdae3563c1093b08668f8b8f4bd4e2e` | OpenDingux / RetroFW 后续实现；克隆网络中断后通过 GitHub API 固定提交并下载相关文件 |

完整参考副本或选定文件位于忽略目录 `build/jz4740-reference/`。未取得可核查的 Dingoo 原厂 FC 模拟器源码，不对其内部优化下结论。DingooEmu 是在电脑上模拟 Dingoo 硬件的项目，不是这次要找的掌机 FC 核心。

## 主要发现及 H1 迁移判断

### 1. 快速跳帧确实省掉了 PPU 工作，但牺牲准确性

[FCEU320 的 PPU](https://github.com/poweravr/fceux/blob/6e4da0b45fbff14fc374c5da38a84463cdabe634/src/ppu.cpp#L2035) 在 `FRAMESKIP` 分支绕开正常 `DoLine()` 扫描线循环，主要推进 CPU 并调用部分行中断钩子。它强制设置精灵溢出标志，并根据 `SPRAM[0] + 1` 近似触发 sprite-zero hit；源码自身标注 `Quick and very dirty hack`。RetroFW 和 retrogame 旧 PPU 路径也保留了这一分支。

H1-FC v0.1.4 的跳帧仍运行背景、精灵和 Mapper 相关逻辑，只省略最后颜色/强调色输出、libretro 转换和 LCD 输出。因此，同样显示为“跳 5 帧”，节省的 CPU 工作并不相同。

可以借鉴“分离可见输出与游戏可观察状态”的结构；不可把上述近似分支直接作为兼容性优化。更激进的实现必须核对 sprite-zero hit、溢出、PPU 地址变化、Mapper 钩子、下一帧绘制状态。用 SGZ2 和简单 Mapper 游戏分别验证，不能只比较当前显示图像。

### 2. 融合调色板转换和缩放，值得优先做原型

[FCEU320 视频](https://github.com/poweravr/fceux/blob/6e4da0b45fbff14fc374c5da38a84463cdabe634/src/drivers/dingux-sdl/dingoo-video.cpp#L238) 使用 `palettetranslate` 两像素查表；[缩放器](https://github.com/poweravr/fceux/blob/6e4da0b45fbff14fc374c5da38a84463cdabe634/src/drivers/dingux-sdl/scaler.cpp#L35) 直接读取 FC 索引像素并输出 RGB565，以 32 位访问一次处理两个 16 位像素。

H1 当前路径是 FC 索引像素 → libretro RGB565 中间图 → 前端 RGB888 转换/缩放 → 32 位 LCD。前端已缓存列映射、颜色表、重复扫描行，并使用缓存内存构建行后写入未缓存 LCD，不能再把这些算成新优化。

新增空间是：保留原 RGB565 量化效果，以 FC 索引和每行强调色选择直接得到最终 32 位颜色，融合最近邻缩放，消除一张 256×240 RGB565 中间图及其完整写入/读取。H1 LCD 为 480×272、32 位，不能照抄 Dingoo 的 16 位像素打包。线性滤波需另行适配并保留原有整数舍入。实机收益尚未测量。

### 3. 音频缓冲决定模拟节奏：方向相同，欠载恢复可再研究

[FCEU320 主循环](https://github.com/poweravr/fceux/blob/6e4da0b45fbff14fc374c5da38a84463cdabe634/src/drivers/dingux-sdl/dingoo.cpp#L134) 在缓冲不足时最多连续运行五个不显示帧，然后强制显示一帧；[音频](https://github.com/poweravr/fceux/blob/6e4da0b45fbff14fc374c5da38a84463cdabe634/src/drivers/dingux-sdl/dingoo-sound.cpp#L194) 通过 SDL 音频锁维护环形缓冲，缓冲过多时等待，回调无数据时输出零。

H1 已使用 PCM 消耗游标控制节奏、低水位赶帧和持续环形播放，不能把音频优先调度当成尚未采用的新方案。现有 H1 欠载会取消播放、清环并重新预填 128 ms，这可能扩大听到的断音，但日志本身不能区分持续算力不足与短暂 I/O 阻塞。

[后续 retrogame 音频](https://github.com/plrguez/fceux-for-retrogame/blob/fbeaad2ddcdae3563c1093b08668f8b8f4bd4e2e/src/drivers/dingux-sdl/dingoo-sound.cpp#L60) 保持回调播放，短缺时保留最后样本，暂停时淡出，恢复时淡入。可借鉴持续播放及整数淡入淡出思路，不能照搬浮点噪声门、SDL 线程接口，也不能无限重复旧 PCM。H1 的固件播放游标、读写所有权和清除已消费数据必须先验证；这一改动不会弥补长期平均算力不足。

### 4. 低质量混音已采用；未发现可以直接替换的 N163 加速

FCEU320 和 RetroFW Dingoo 前端默认 32000 Hz、Sound.Quality=0，与 H1 原有 Low 混音思路相同。H1 本地 v0.1.4 已改为核心 22050 Hz、设备 32000 Hz，新增重采样会占用少量时间，是否净获益需设备数据。

FCEU320 `boards/n106.cpp:332` 的低质量 N163 路径仍循环 `Count * 16`，步进系数还使用 `long double`。它不是比当前 H1 整数实现更好的现成补丁。当前 H1 已合并不变输出区间并缓存系数，拥有独立 PCM 等价测试。不能关闭 Mapper 19 扩展音源来宣称断音修复。

### 5. 编译优化和真实设备热点

旧 FCEU320 / RetroFW 构建采用 `-O2 -mips32`，与 H1 主体优化等级相近。[后续 retrogame Makefile](https://github.com/plrguez/fceux-for-retrogame/blob/fbeaad2ddcdae3563c1093b08668f8b8f4bd4e2e/Makefile#L248) 使用 O3、LTO，并提供 profile-generate/profile-use；按设备区分 MIPS32 与 MIPS32r2。

H1 可分别试验 O2+LTO、O3+LTO，并看热点函数代码大小与实机耗时。不能使用后续芯片的 MIPS32r2 参数。不能从主机 QEMU/Unicorn 时间推导 H1 缓存收益；O3 的膨胀可能抵消收益。PGO 是更长期选项，需解决裸机 profile 数据收集，不能复制 Linux 文件写出机制。

[2022-10-16 performance build](https://github.com/plrguez/fceux-for-retrogame/releases/tag/2022-10-16) 说明仅针对 lepus / JZ4770，并且只是更新工具链，不能作为 JZ4740 上某项核心优化已经证明有效的依据。

### 6. 没有找到这些分支里的 MIPS 6502 汇编或 JIT

检查的 CPU 执行路径均为 `x6502.cpp` 的 C/C++ opcode switch 和读写处理器。旧分支 CPU ROM 读仍使用 `ARead[A](A)`；H1 已尝试带处理器身份检查的 CartBR 快路。不能因为设备是 MIPS，就假设这些 FC 模拟器采用与 GBA 相同的动态重编译。当前没有足够证据支持整体换核就能解决断音。

## 建议顺序

1. 以 SGZ2 固定场景测当前各阶段实机耗时，同时做“索引调色板转换 + 最近邻缩放”的可比较原型。
2. 按游戏可观察状态分离 PPU 输出工作，先走准确路径；激进近似跳帧若试验，必须是明确可回退的兼容性取舍。
3. 在确认固件环形播放游标和数据所有权后试验欠载持续播放、受控静音/淡入，避免重复取消播放造成额外预填空窗。
4. 独立比较编译策略，不同时混入多个核心修改，否则难以确定收益来源。

这些是源码核查后的优化候选，没有 JZ4740/H1 实机 FPS 或“已修好”的结论。本次没有运行新性能测试。
