# H1 当前运行版本的 RAM 诊断与优化

发布范围：本文保留开发期本地实验记录。涉及特定旧 ELF、设备 RAM 布局的 `tests/hardware_clock/`、安装计划及旧快照解码脚本不随 v0.1.16 发布；文中的 `build/hardware-*` 是维护者本地证据索引，不是公开源码所包含的输入。正式应用的完整构建源码、通用测试与精简发布证据另行提供。以下历史数字和哈希不作改写。

用户在 2026-10-16 明确要求：保持 H1Debug 运行；根据当前版本的内存变量和 PC 找热点，上传优化代码到 RAM 运行并复测；最终 BDA 更新及关闭调试器只由用户操作。本任务不再调用 stop，不自动切换磁盘、不安装测试 BDA。历史 `hardware-comparison-plan.md` 中主动停止调试器和安装 NES-D 的流程不适用于本次工作。

## 已识别基线

- 实机已加载的是 0.1.12 profile/JIT，BDA SHA256 `2a246e5cf3653016c07aece597373acaea0a59bf94c7efd9c79f4cd6bf720893`，并非工作区最新普通版 0.1.15。由 ELF 与三处不可变代码片段核对。不能把此数据称为普通版性能。
- H1Debug resident image_id `7143fbe9`，设备身份 `H1DEBUG/1 V1.41 RESIDENT`。
- ROM 为用户的 `SGZ2-CN.NES`，PRG＋CHR MD5 `de319a7a8099c388810c59cb038e8c9d`，Mapper 19。
- 城市菜单场景，配置跳帧 1；音频追赶实际会增加到跳帧 5。
- 安静窗口 56.781 秒：2308 模拟帧，约 40.65 FPS；385 次显示，约 6.78 FPS。窗口内没有持续 PC 采样或截图。
- 相邻音频计数窗口欠载增加 579，存储暂停 5 次、69 raw ticks；欠载次数不等于缺音时间比例。
- raw tick 实测约 40 Hz，不能按 ABI 字段名假定 80 Hz。

证据保存在 `build/hardware-20261016/city-quiet-window-analysis.json`，原始 wire、RAM、截图及 ELF 身份证据同目录保留。

## 热点证据与边界

三组共 960 个新鲜、就绪的前台任务暂停点：生成的 JIT 代码 376，`h1_cpu_run_jit` 144，`RefreshLineBody` 73，精灵提取 49；`memcpy` 40 点的保存返回地址均来自缩放输出，`memset` 37 点中 36 来自 `ResetRL`。

这些是被中断前台的位置分布，不是整机 CPU 时间百分比；IRQ 耗时不在其内，40 Hz 唤醒也可能产生相位偏差。`h1_cpu_run_jit` 包含解释器回退，不能将全部样本归因为分发。原始记录没有通用寄存器，不能猜测生成代码对应的 6502 PC；后续扩展采样需先核对固件保存上下文布局。

## RAM 优化验证方式

1. 单一 Client 串行持有设备；所有分析与构建代理只离线工作。
2. 每次读取或修改的地址都来自当前已核对的 ELF、已确认存活的分配，或当前 EXEC 返回的模块地址。
3. 优化以局部可恢复 RAM 代码覆盖测试；完整 BDA 不作为 EXEC 模块运行。代码覆盖前检查原指令、调用 ABI、前台安全边界及 I/D cache 维护。
4. 保存原始字节，测试模块保留明确的 apply/status/rollback 状态；只在已确认安全的暂停边界修改调用点。任何结果未知先读回确认，不能盲目重发。
5. 活跃覆盖引用 EXEC 代码期间不上传其他模块、不清除该模块。采样与覆盖管理需要共存；先回滚、读回并验证原应用正常，再考虑更换模块。
6. 基线／候选保持同一 ROM、场景、配置、音频、调试连接与测量方式。吞吐窗口只读两端；PC 采样另开窗口，不把其干扰混入安静窗口。
7. 先验证状态、画面、PCM 与保存内容一致，再测真实完整应用 FPS、显示频率和欠载。工作量减少不能替代真机收益，也不能用小幅优化宣称已满速。

## 已验证的 ResetRL 优化

`ResetRL` 在跳过颜色输出、无精灵零候选且无观察者时，可省略行缓冲填充。隔离普通构建通过完整应用输入路径的 1800 帧开局至城市流程、精灵状态恢复及观察者负控；模拟执行工作量减少 0.698%。对应 RAM 单调用点版本已完成独立审核、真机 A/B/A 与原指令回滚验证，结果及尚未安装的产品候选见下文。

目标仍未达到。后续应优先细分 JIT 回退及生成代码的真实热点，避免只围绕小比例的缓冲操作反复优化。

## 2026-10-17 运行中验证

调试器保持原 image `7143fbe9`，没有安装 BDA 或切换 USB 模式。后来工作区的 `dist/H1Debug.build.json` 被其它构建更新，客户端改为核对先前保存的驻留器身份、分配和固件指纹，不能因此要求重装调试器。

新一轮同进程城市基线 A0/A1 分别为 42.084 / 42.096 FPS，显示约 7 FPS；配置跳帧 1，音频追赶使实际跳帧增加到 5。安静窗口内 JIT 编译数、代码用量和回绕数不变，持续卡顿不能解释为反复编译或缓存容量耗尽。

新增 GPR 采样与完整 JIT arena 解码把 334 个 native 点对应到具体编译块。其中 71 点落在严格入口校验，221 点在主体/事件/连接/退出，剩余为私有入口和返回。第三轮采样间隔与前两轮不同，不能直接按总点数称 CPU 时间比例。`h1_cpu_run_jit` 也包含解释器回退，不能把全部样本当分发成本。无条件统计累加模板仅命中 2/800 个点，没有支持其为主要热点的证据。

关闭旧 profile 构建的主要计时探针（enabled/detail_mask/ppu_sample 清零）后测得 42.644 FPS；相邻原状态为 42.096，约 +1.3%。两端 `pc_fault=0`。这仍保留无条件计时器/RTC 读取和 profile 编译布局，不能称普通版性能。后续正常暂停/恢复已重建计时状态，未直接写 enabled=1。

PPU 单调用点 RAM 覆盖已在真机上传、检查、应用并回滚成功。B 窗口为 43.239 FPS、欠载 8.787 次/秒；原代码 A1 为 42.096 FPS、9.398 次/秒，回滚 A2 为 42.135 FPS、9.400 次/秒。这一轮 A/B/A 支持该场景的小幅提升，尚未达到满速，也没有据此推广至其他游戏。所有覆盖有持久所有权记录，只有显式回滚并直接读回原指令后才允许替换 EXEC 模块。

离线测试曾发现 FrameCore 未处理整手柄查询 `id=256`，使某轮所谓城市回放实际停在标题。该结果已撤回并保留为失败证据。修正后重新验证 1800 总帧的开局至城市流程，逐帧状态、PCM、显示和 SRAM 一致，并核对终点城市截图；这不是 1800 个纯城市帧。早先普通版 NES-X 使用完整应用输入路径，独立复核后确认未受此问题影响。

屏蔽 IRQ 时保留已验证的 JIT 入口凭证这一候选已经否决。实际 IRQ/NMI/RESET 一律保持原撤销的保守版本，通过旧/当前核心差分、300 个已确认城市帧及 39 项管理器故障测试；但硬件 B 为 41.829 FPS，回滚 A 为 42.102，未见收益。城市回放也显示 serial 重建少 8.23%，private reuse/native_ops/links 不变，指令入口反而增加 0.042%。只减少一个计数不足以证明性能改善，产品不集成此候选。更宽的 frame-only 方案还被实际 MIPS 负控否决：旧编译器已删除后续 opaque 向量前的冗余撤销，不能直接热改前一条 store。

PPU 回滚 A 为 42.135 FPS，与 A1 的 42.096 接近；B 相对两次 A 均值约 +2.67%。同一窄门逻辑已集成到 `tools/prepare_ppu.py`，独立普通候选保存在 `build/hardware-20261016/resetrl-product/build/release-jit/`，BDA SHA `e1808bcc5e8254d367633c2bbd0d5889232a38799768ed6c629d659ba66bb1f1`。标题 NES，未安装、未发布。原 release-jit 和 dist 产物未更新，不能拿它们当新包。

进一步城市回放将热点块 E066/E87A 的严格入口与真实有效 revoke 关联：大量来自 `$E800/$F000` 的 Mapper 19 写操作，且 32 个 CPU Page 指针在调用前后完全相同。该证据来自旧核心的实际 MIPS 回放，不是硬件历史中缺失变量的推测。保留实际 handler 和 APU flush、仅对已证明安全的无 PRG 映射变化写保留原有效凭证的方案，仍在验证。

内部 `SyncPRG→setprg8r→setpageptr` 也会重复撤销凭证，因此仅跳过 caller 外层清零不足以解决问题。离线窄范围原型同时覆盖两个已识别 native 写片段和四个 Mapper 内部调用点；只在全部映射与 PRGIsRAM 仍一致的受控调用中省去重复设置，保留真实 handler、E800 的 FixCRR 和原事件边界。原 epoch 为零时始终保持零，不能从 serial 重建有效凭证。首轮 30 个城市帧状态、PCM、视频和 SRAM 一致，凭证复用增加约 48%，但尚不能把它换算成真机提速。

现有 0.1.15 的 FB28 等待循环 HLE 已为当前 0.1.12 制作可回滚的 dispatcher 回植，用于同进程因果测量，并非新增产品算法。144 组 ABI、512 项状态负控、44 项管理器故障测试，以及 100 预热＋300 城市测量＋6 回滚帧对照通过；300 帧中接受 35,382 次，合并 271,836 对等待指令，状态、PCM、视频和 SRAM 逐帧一致。物理覆盖还需由完整应用安静窗口评估，不能把离线合并周期数当作速度收益。

HLE 真机 B1 为 42.409 FPS，5 次存储暂停/70 raw ticks；回滚 A4 为 42.410 FPS，4 次/54 ticks。之前 A3 为 42.102 FPS，5 次/70 ticks。当前没有稳定的完整应用 FPS 提升证据，存储次数差异也使 A4 不能单独否定小幅计算收益。已显式回滚，两条原指令与未改的延迟槽均直接读回确认。这个额外的旧版 RAM 回植不保留；工作区原有 0.1.15 HLE 未被删除或关闭。

Mapper 保守原型的 300 城市帧对照已完成：凭证复用约 +47%，但总核心 MIPS 工作量仅 -0.476%，因为新增映射预检查抵消了大部分节省。它没有上机或进入产品，不能据复用计数声称提速。

完整模板解码进一步细分 221 个 native 主体暂停点：95 个事件预算检查、43 个时钟提交。138 点中 97 点属于独立逐指令路径，其中孤立纯指令 38、条件分支 29、可变周期 indexed 读 15、末尾 IO 写 9、间接跳转 3、其它读 3；没有观测到 region 检查失败后单指令回退。这解释了为什么继续优化已批处理路径的门槛未必有效。工作区 0.1.15 已有分支尾批处理，实际加载的 0.1.12 尚无此能力，后续 RAM 试验必须明确区分回植与新增优化。

Mapper 19 E800 CHR 合并候选已真机测试并完整回滚。它保留第一次旧映射下的 LineUpdate，省去同一时刻随后七次重复调用；离线300城市帧状态、PCM、视频、SRAM一致。硬件B1=42.3114FPS、9.2957欠载/秒、5次存储/69rawticks；回滚A5=42.0847FPS、9.4046欠载/秒、5次存储/72rawticks，单对约+0.539%。此前A4的4次存储与本组不同，不能直接用它否定或证实微小增益。本候选未进入产品，当前只算小幅初步证据，未达到全速；debug7143fbe9持续运行。

整块 FB28 branch-region 回植也完成100预热+300城市帧及6回滚帧一致、192边界/ABI验证，但30帧核心指令工作仅-0.808%，代码1740→3660B。未部署此单变体，继续在完整原生块的共同入口覆盖busy-wait HLE，避免块内backedge绕过dispatcher HLE。另一个当前普通核心WRAM inline/region候选300帧只省0.0338%核心指令，也不作为主要优化部署；这些是离线指令量，不是实际周期或FPS。

Native FB28 HLE 的完整候选已完成100预热+300城市帧+6回滚帧对照，150显示帧，所有state/PCM/video/SRAM逐字节一致；总比对digest a20b1a8c3a8c13cd16081bd1ec236df4d99060e9b255fab5c0a45f9aa828a1be。旧核心指令755350953→719193659（-4.786820%），dispatcher142032556、SoundCPUHook21746及NamcoIRQHook1500完全相同。HLE接受38727次、294826对循环，原生块1848B+helper1056B，214边界通过。收益还未真机验证，不能换算FPS。原生适配保留native路径已有APU绝对deadline；早期沿用dispatcher重置deadline的错误版已记录并弃用。

跨块共享事件预算的两版隔离完整编译器原型均尚无收益：第一版核心工作+1.70%；第二版整段记账恢复调用/ops/links/dispatcher一致，但仍+1.245%。30帧实际30969次native调用、6970次links，平均约1.225块/调用，预算初始化/flush难以摊薄且代码cache从1.577M到2.044M。不能仅凭整体设计名称推断提速；当前拒绝部署。下一步优先把Mapper19无实际映射变化的写操作从“外围全页预检查”改为“在既有映射更新点检查真实变化”，保留全部原PPU/APU同步和未知回调撤销。

Native FB28 HLE 真机 A/B/A 已完成：A6=42.083726 FPS，B1=42.738214，回滚A7=42.120924，B相对两次A均值+1.510%。欠载分别9.41848、8.80546、9.40459事件/秒；B有6次存储暂停/84rawticks，两次A为5次/73和69ticks，不能把模型工作量-4.79%换算为真机速度。屏幕hash三组一致，原native入口及stamp已直接读回确认恢复，Debug7143fbe9持续运行。此为当前0.1.12已加载应用的RAM优化，普通0.1.15产品的native回退/等待优化还需单独验证；尚未满速。

第二次独立应用 native FB28 覆盖，B2=43.009114FPS，对相邻A7=42.120924约+2.109%，均5次存储暂停（71/69rawticks）。欠载8.92848对9.40459事件/秒，失败和drop均0；再次显式回滚并读回成功。Matched A/B各320个poll1新鲜就绪暂停点，旧FB28 11点变成新native3点+helper1点；这只支持热点迁移的定性结论，不能说耗时下降63.6%。剩余native主体仍较集中在事件检查和时钟提交。

当前普通0.1.15的独立native等待候选没有收益：允许编译FB28并在区域入口调用语义helper、保留native APU绝对deadline，100预热+30城市帧state/PCM/video/SRAM一致，但core slots 66000914→66212036（+0.3199%）。原0.1.15dispatcher HLE已经包含主要优化，因此不集成这轮候选，也不把旧版真机+2.1%算作新版增益。证据native-hle-current/city-report.json。

Mapper19实际映射变化再撤销凭证的新setter候选在普通0.1.15上完成192实际MIPS边界及100预热+300城市帧四域一致；core slots659772689→649203587（-1.60193%）。native调用/ops/link/编译和调度次数完全相同，strict复用82450→226849。第一版被独审发现GCC将epoch清零下沉到Page写后，v2通过编译器memory barrier修复并在真实store前观察验证。尚未真机部署、未进入产品。

PPU调度切片证据（ppu-slice-histogram/）：旧0.1.12与普通0.1.15在6城市帧中均5796次X6502_Run，即966次/帧。可见行是256、6、63、16 PPU dots。6-dot窗口1440次中566次有效CPU credit≤0，剩余只够启动1条最短指令；63-dot最多21个CPU周期。APU/DMC horizon为正、无pendingDMA，短credit主要来自PPU调度。6与63之间的Fixit2及16/下一行之间的扫描线状态是实际事件，不能直接删除边界。整块fastbody首版12块即使快体代码48948→23212B（旧冷体仍保留），100warm30city工作只-0.4233%，743次冷退里631次来自CPU-prefix不足。后续优先在单份快体内支持短CPU前缀并统一提交，保留原事件与零副作用冷退；只有真机测试才能判定代码体积对缓存的收益。

只读 EXEC 快照已取得实际加载版的现有 profile 变量，没有换诊断 BDA。复制在 GUI 任务被正常优先级调度挂起时完成，前后任务/帧/计时字段一致、第二遍逐词验证及 CRC 通过。首版因误将 TCB 四个状态/优先级/位图字节当成单个优先级字段而拒绝（-4，未复制、未写应用）；修正为固件证明的完整值 `0x01020a00`，另加六条固件指纹，38 实际 MIPS 负控通过后重新上传。成功记录 `profile-snapshot-v2-run.bin` / `profile-snapshot-v2-data.bin`，CRC4287215293；3049 完成帧的 scope 总72.5866秒、TCU72.6906秒、RTC73秒，未发现几秒级未归因间隙。

这轮轻量模式2950帧：跳过显示的核心均值20.107ms，显示帧32.692ms；即使不输出画面也超出16.639ms预算。coarse20帧的CPU入口整体18.18ms、PPU其余5.87ms；CPU细模式20帧native独占12.54ms、dispatcher/fallback独占6.97ms。它们是含探针及中断的墙钟范围，且CPU范围含部分CPU总线触发的PPU工作；不是无扰动机器指令成本，不能跨模式相加。按显示比例校正，coarse/CPU/PPU/sound被采帧分别比control多3.16/5.81/7.59/4.03ms，样本仅19至20帧，应结合PC与安静窗口判断。sound模式合成、事件、finalize与PCM合计约1.31ms/帧，音频本身不是主热点。

全3049帧共7次同步存档耗时2.5029秒，平均357.55ms/次，占TCU区间3.44%。它确定会造成周期停顿，但不足以解释总体速度缺口。真实自动追帧使effective_skip=5（配置仍为1），以上均需按实际显示/跳过分母解释。A9安静55秒41.901963FPS、9.363965欠载事件/秒，5次存储/71rawticks，context通过；A8按键租期尚未结束的起点被排除正式对比。

OAM行索引回植的真正六入口RAM版本通过100预热+300城市帧逐帧state/PCM/video/SRAM一致、120 linked ABI、29写入/DMA/失败状态/观察者场景及420独立Fetch比较。模型工作755350953→724246123（-4.11793%）。管理器101故障场景、host93场景通过后在真机应用，B1=43.028556FPS、9.259332欠载事件/秒、4次存储/56rawticks；画面像素hash与原城市相同。与A9相比存储次数不同，尚需回滚对照，不能据此宣称满速或普通0.1.15获得同等新增收益（普通版本已有OAM索引）。

OAM回滚A10=42.156306FPS、9.418472欠载事件/秒，5次存储/69rawticks。第二次独立应用B2=42.755215FPS、9.109975欠载事件/秒，5次/70ticks，相对A10约+1.420686%。两次均显式恢复六入口的全部十二条原指令并独立读回，模块所有权已释放，debug始终运行。第一次B2回滚因暂停按键未被应用接收而返回not-quiescent、零写入；查看仍在城市的截图后重新正常暂停，确认菜单再回滚成功。这个小幅实测收益支持优化有效，但远未达到满速。

后续十二块整体CPU前缀候选保留旧冷路径，将候选代码48948→25536B，100预热+300城市帧模型工作仅-0.4304%。1482核心边界、5编译负控、109多入口管理故障、312最终物理链接ABI、232host故障场景均通过。仍需真机测I-cache/访存影响，不能以体积或指令数推算实际速度。

十二块候选现已完成真机两次独立应用、两次回滚。A11=41.829509 FPS（6次存储/83 raw ticks）；B1=43.373607（5/72）；回滚A12=42.066531（5/70）；B2=43.409960（5/72）。对存储次数相同的A12，B1/B2分别+3.1072%/+3.1936%，欠载事件由9.38644降至8.80545/8.75098次每秒。四个55秒窗口均通过上下文和USB静默检查；模型指令量-0.43%不能替代这组物理结果，也不能单凭差异断定具体cache miss数量。模块SHA35a66d10c37916bc44ee9ca2f9963b6b060bd3843295951cb7dff2b840d62366，原始记录与校验脚本见whole-prefix-physical-report.json及tests/hardware_clock/whole_prefix_physical_report.py。

第二次回滚在raw tick710383..710384完成，12项native引用恢复，L1/L2全扫描无残留，12个入口和stamp均由主机再次直接读取确认。没有更换最终BDA或关闭Debug。上述仍是0.1.12实际加载版、同一城市命令菜单静止场景，配置跳帧1但追赶时effective_skip=5，尚未达到满速或验证完整城市操作流程。

普通0.1.15的精灵范围缓存发现一项独立正确性问题：InputScanlineHook收到可写sprlinebuf后，可以修改缓存范围外的像素并自行清空回调；后续CopySprites仍按旧范围裁剪，漏掉该像素。原创测试ROM在实际MIPS运行中复现旧0.1.12输出0xAA、普通b5输出0xA1（关闭渲染路径为0x8F），完整状态相同但画面相差一个像素。产品生成器现于两处公开可写精灵行之前，在spork有效时将范围扩为0..256，并保留至下一次RefreshSprites重建；无观察者路径不增加执行工作。

该修复通过4项旧核心/b5/修复版实际MIPS回归（范围内、范围外、回调自清、关闭渲染路径），100预热+30城市帧state/PCM/video/SRAM一致，核心slots均为66000914；完整prepare_core生成也通过。证据目录为`build/hardware-20261016/sprite-observer-fix/`，`validation-summary.json` SHA256为`7d974dea1648b8875cf8ea3caa52ed5652fc931c6e92b0df1b0d4133ae1007f3`。`tools/prepare_ppu.py` SHA256为`0d4d777725d98401d2df88d0cf451d47b68339dc433a061213ebd0766c7444ae`；单改动候选ELF为`7c3a448958f652f1f004271b05070bc38cf1c92fe7f4fd1ff7321e62b33f5335`，BDA为`5a9fe1def799cf29cf5cc5fa572c6e8ed867757633d2698a0470418ac45c1099`。独立复审通过。原`build/release-jit`的b5 BDA/ELF及设备安装均未改变，此处候选只验证该正确性修复，不含其它待发布修改。
## 存档停顿的真机分段证据

保持原存档逻辑与频率，通过 RAM 调用点包装记录 7 次完整存档，28 个阶段样本均通过 TCU/RTC 一致性校验。写入打开平均 2.433 ms、8348 字节 fwrite 平均 1.853 ms、写文件 fclose 平均 **346.187 ms**（最大 370.972 ms）、回读验证 fclose 平均 0.044 ms。写关闭占这四个阶段总耗时约 98.8%；不能将 fwrite 函数的短耗时当成数据已持久化。

这确定了周期性大停顿的位置，但不改变持续运行约 42 FPS 的 CPU/PPU 主瓶颈。下一候选是在已存在、长度严格匹配的非活动存档槽上使用更新打开，减少截断、重新分配及延迟提交的 FAT 元数据；仍保留实际写入、关闭、完整回读校验及原保存频率。真实收益待测，不能根据阶段占比保证提速，也不能跳过关闭或持久化。

取样模块已显式回滚，5 个调用点的 10 个原指令均由主机直接读回确认。28 条记录和汇总已下载，FNV 与统计重算通过；证据为 `build/hardware-20261016/storage-timing-live-analysis.json`。H1Debug `7143fbe9` 保持运行，没有更新 BDA。

## 四项 RAM 优化合并验证

将 12 个 CPU 前缀短块、FB28 native 等待循环、OAM 行缓存和 ResetRL 窄门合并，完成 100 帧预热＋300 城市帧＋6 回滚帧四域一致、128 项管理器故障及最终链接 ABI/交互验证。真机仍是同一已加载 0.1.12 profile 应用，未安装新 BDA。

| 场景/轮次 | 模拟 FPS | 存档次数 / raw ticks |
| --- | ---: | ---: |
| 城市命令 A13 | 42.102 | 5 / 68 |
| 组合 B1 | 45.915 | 5 / 70 |
| 回滚 A14 | 42.083 | 5 / 69 |
| 组合 B2 | 45.661 | 6 / 82 |
| 开发子菜单组合 B | 45.677 | 6 / 84 |
| 开发子菜单回滚 A | 42.412 | 4 / 56 |

同为 5 次存档的 B1 对 A14 提升 9.11%；B2 提升 8.50%，但多一次同步存档。开发子菜单单对提升 7.70%，保存次数不同，不能单独当精确算法增益。B1 欠载为 7.55 事件/秒，对照约 9.31；仍明显欠载，尚未满速。配置跳帧 1，运行时追赶使实际跳帧 5，显示仅约 7.6 FPS。

硬件操作已从城市命令进入开发子菜单并返回，没有执行农业/商业等资源变更；这验证了菜单转换，仍不能称完成整段城市运营或战斗。基线和候选城市命令截图像素完全相同；子菜单截图经人工检查一致，闪烁相位使像素 hash 不同。

两轮均显式回滚并直接核对 13 个入口/凭证与 7 个固定调用点；完整 L1/L2 扫描无剩余模块引用，安静计时窗口内 JIT 编译数、arena 用量和回绕数未变；进入开发子菜单曾触发额外正常编译，未把切换过程计入这些窗口。原始安静窗口、控制结果及读回证据可由 `tests/hardware_clock/combined_physical_report.py` 重建；报告位于 `build/hardware-20261016/combined-physical-report.json`。H1Debug 全程保持运行。

### 同长度存档更新的真机试验（SU，2026-10-17）

沿用已验证的双槽、关闭后完整读回、CRC 和逐字节校验，仅在非活动槽已存在且长度恰好为 8348 字节时，改用更新打开并从头写入，避免每次截断重建；条件不符保留原写入路径。本轮 11 次全部命中，44 个计时记录均有效，没有打开、定位、关闭或校验故障。

| 阶段 | 原路径 7 次平均 | 更新路径 11 次平均 |
|---|---:|---:|
| 打开 | 2.433 ms | 1.534 ms |
| 写入 | 1.853 ms | 1.160 ms |
| 写入后关闭 | 346.187 ms | 238.525 ms |
| 校验后关闭 | 0.044 ms | 0.033 ms |

关闭阶段减少约 31.1%，但仍是一次约 239 ms 的同步停顿。55 秒完整应用窗口为 42.319 FPS、欠载事件 9.469 次/秒；该试验未叠加四项核心优化，且不能据单个窗口认定整体音频改善。剩余关闭路径会同步提交设备缓存，尚未对其内部各阶段计时，不把全部耗时称为 NAND 擦除成本。

证据：`build/hardware-20261016/storage-update-live-analysis.json`；记录 SHA256 `4cadf3695051b85db9f4fd9725096e1ee55e5ad8d913413b567a48e187ee122e`。已在暂停状态显式回滚并独立读回全部十条原指令；Debug 保持运行，没有更新 BDA 文件。

### 四项组合后的热点复测（CP，2026-10-17）

通过有限 EXEC 拷贝当前应用已有的计时变量，运行中双份验证一致后下载；未安装新的诊断 BDA。捕获在 55 秒静默测速区间之后进行，随后才暂停，避免暂停日志清空已完成帧计数。原版 A1 / 组合 B1 / 回滚 A2 的完整应用吞吐分别为 42.030 / 45.371 / 42.266 FPS。这个模块的代码布局与前轮组合模块不同，单独报告实测值。

轻量模式核心平均耗时 22.215→20.546 ms。组合后的粗采样仍有 68.36% 在 CPU、21.78% 在 PPU 循环；CPU 细分模式中 native 平均 11.460 ms，分发及 fallback 平均 7.324 ms。粗采样和 CPU 细分分别只有 23 帧，包含各自探针开销；不把不同模式相加，也不把计时器耗时称为硬件 CPU 周期。

原始快照 CRC、逐字一致性、窗口上下文和回滚独立读取可由 `tests/hardware_clock/combined_profile_report.py` 重建，报告为 `build/hardware-20261016/combined-profile-physical-report.json`。本轮仍是实际已加载的 0.1.12 PROFILE，不能冒称普通 0.1.15 的提升；仍未达到全速。

### 大 RAM 接管的首次设备拒绝

v3 管理器第一次真机申请返回 `VR_BAD_RESIDENT`，发生在 malloc、冷清 JIT 表和写入入口之前。原因是把 EXEC 的 `api->device_command` 当成了 USB 线上的 STATUS 调用；实际驻留实现的该函数返回不支持。原模型对它的假设错误，不能以离线通过代替设备 API 验证。

已保留原失败版本与日志，通过只读 inspect 确认 EMPTY、无分配和发布记录，并独立读回八处原入口后解除主机的保留状态，没有盲重试。修正版将核验实际驻留头及对应任务信息，不升级或关闭 Debug。对账证据在 `build/hardware-20261016/event-v3-refusal-cleanup.json`。


### 共享事件预算 v3 的实际 RAM 接管

修正 resident API 后，设备实际分配 222735 字节，CPU/JIT blob 连同私有 BSS 共 222704 字节完整传输并读回校验。八处公开入口切换、两方向冷清所有旧 native 引用，原游戏状态保持。B1/B2 完整应用分别 42.628/42.336 FPS，欠载 9.183/9.070 次每秒；回滚 A1 为 41.521 FPS、9.691 次每秒。存档分别 5/6/5 次，仍有 6/3/5 次新增编译，不将单一差值当稳定的精确增益。代码体积下降并未带来大幅速度提升，不能据此宣称满速。

该候选建立在实际加载的 0.1.12 CPU/JIT 上，尚未包含普通 0.1.15 已有的分支/栈覆盖与 HLE，所以也不能称作当前普通版的全部性能。两个 B 窗口之后，八处原指令恢复并逐项独立读取，1476608 字节 metadata 全零检查后显式释放 payload；没有更新 BDA 或关闭 Debug。旧 CM13 native 地址绑定因冷清失效，禁止重新套用。可重建证据：`tests/hardware_clock/event_ram_physical_report.py --variant event-v3-device --windows b1 b2 a1`。


后续 v5（同一旧 CPU 架构，保留 Mapper19 E800/F000 写入事件窗口）完成独立 RAM 接管，55 秒为 42.684 FPS、9.114 欠载事件/秒，5 次存档/70 raw ticks，8 次新增编译。相比 v3 的约 42.3–42.6 FPS 没有明显突破。已恢复八处原入口，读回1476608字节metadata无引用后释放213071字节分配。证据 `event-v5-device-physical-report.json`。不把此单窗口当作 current0.1.15 的性能结果或满速证明。


### 当前普通版 CPU/JIT 单独接管的初测

实际安装BDA仍为0.1.12。将普通0.1.15的两个原始CPU/JIT对象（017=99986d53…、040=d0f8983a…）链接到真实malloc返回的0x82694cc0，完整payload217860字节校验后只切换8入口；HLE的page/12字节统计/enabled独立读回，运行中统计继续增长。没有修改旧PPU/APU/frontend，也没有安装BDA。

两轮55秒为41.294/41.276 FPS、欠载10.144/10.140次每秒，未见CPU部分在真机上的提速。第二轮仍有2块新增编译，保存4次/54rawticks。直接读当前stable_policy=1、clock_mode=1、known_irq=0x83cb2068、status_read=0x83c29b0c，与精确旧导入身份一致，暂不支持“快速策略没开启”解释。新对象关闭CPU细分profile，旧PPU/APU/app仍带profile；此混合测试不能冒称完整0.1.15普通BDA，也不能把差值全归算法。

当前代码工作集、控制片段和覆盖断口继续分析。仅开放单条控制片段的离线首筛虽减少C分发，却增加native工作，核心槽数只下降0.72%、arena增加11.3%，未直接合入。此前小模型指令数改善的结论不能替代本轮物理吞吐。


## 2026-10-17: current CPU regression isolated in agriculture selection

The current CPU/JIT-only RAM transplant completed verified rollback/free; Debug7143fbe9 remains continuously running. No BDA installation. Both scenes were compared in 55-second quiet full-application windows.

- Agriculture general selection (BEI/YU/FEI): original 35.3670448 emulated FPS vs current CPU-only 32.3567758 (-8.51%). Underruns 12.32763 vs 13.28389 events/s. Both windows had zero new compilations. Original had 6 saves/83 raw ticks versus candidate 5/70, so the candidate regression is not explained by more synchronous saves in that window.
- City command screen: fresh original 41.7752838 FPS; CPU-only B1/B2 41.2942304/41.2758731. No full-speed claim.
- Offline matched agriculture evidence shows native calls +8.72%, executed native address footprint +18.17%, native+dispatcher instruction slots only -0.55%. Cache behavior remains a hypothesis until isolated physical comparisons.

Sources: build/hardware-20261016/current-cpu-device-physical-report.json and build/hardware-20261016/agriculture-locality-comparison.json. Next baseline adds current ordinary PPU algorithms to the current CPU; this remains a RAM transplant into the loaded0.1.12 app, not a byte-identical current ordinary BDA.


## Native admission is the measured bottleneck in agriculture

On the same active currentCPU+PPU RAM payload, a verified single-word branch changes only native admission to its original C fallback; HLE, opcode fetch, IRQ/APU/PPU, settings, warm cache and save policy remain. The first two 55-second physical windows measure 58.497465/58.625491 emulated FPS with zero new underruns, versus the matching native-on 33.539309 FPS and12.6067 underruns/s. All compile/bind/native counters stop incrementing. Original and disabled screenshots103/105 are identical.

This is actual full-application throughput, not model instruction slots. It identifies the current native path as a substantial net slowdown in this scene; it does not isolate I-cache misses from other native-path costs. Storage counts differ: on6/85 raw ticks, firstoff4 completed/56 and an in-progress final save, secondoff4/56. Thus the exact74.4% gross uplift should not be presented as a pure cache speedup. Displays remain13.77/13.06FPS with automatic catchup/skipping, not a claim of30 displayedFPS at configuredskip1. Zero underruns excludes explicit storage pauses from that counter.

Resident write/cache proof: build/hardware-20261017/resident7143-cache-proof/report.json. Native gate differential/manifest: build/hardware-20261017/current-native-gate/. No Debug shutdown or installed BDA modification. The gate must be restored before the active core14 RAM transaction is rolled back/freed.


City reversal confirms the same dependency: native-on43.009/42.974FPS, native-off58.362FPS with zero underruns, restored-native42.972FPS and8.689 underruns/s. Restoration was a verified single-word reverse change, retaining the identical14-entry payload and arena. DisplayedFPS increased7.16→14.08 with off, so the gain did not come from skipping more video. The native path is a measured net regression for this exact ROM/scene; a dedicated C+HLE product policy is now under independent lifecycle/differential tests.


## Dedicated C+HLE product path, first physical check

The separately compiled exact-ROM C+HLE dispatcher replaces native admission without altering CPU/APU/PPU behavior. Its14-entry payload was transferred to allocated RAM and verified byte for byte (286804B, transport SHA c0aa6796fae7f2534efadacebb47dbcb7485c24ba09d46b9e20ee3ace6c9f7a2), then its entries/HLE/PPU seeds were read independently. In agriculture selection, the55s full-app window is58.325524 emulatedFPS,11.686882 displayedFPS, zero new underruns/drop/failures/video errors; all captured native counters stayed constant. Five complete saves consumed69 rawticks. Adaptive catchup occurred in2910/3214 frames, so this is not a claim of configuredskip1 display speed or uninterrupted storage audio. Screenshot113 matches111 exactly. No BDA install or Debug shutdown.


## 组合优化的城市真机结果（C+HLE、追赶恢复、存档更新）

在同一已加载0.1.12 PROFILE应用中，经独立校验的17入口RAM代码包含当前普通版CPU/PPU、专用C+HLE路径、音频追赶恢复及同长度存档更新；没有安装新BDA。城市55秒窗口得到58.833902游戏FPS、29.362509显示FPS，新增欠载/视频错误/音频失败/丢弃均0。对比14入口的58.416216/21.450130，显示恢复明显；追赶帧由40.17%降至1.23%，端点有效跳帧均1。五次保存共49原始tick，14入口同五次为69tick。计时窗口无按键/截图/EXEC，仅两端读取现有变量。

这是完整应用的实机结果，仍包含约四分之一秒一次的同步存档停播，不是持续DAC无中断或完整普通版安装后的保证。组合中的各项分别已有正确性验证，这一窗口不用于拆分单项精确收益。原始证据 current-recovery-device-city-b1-{before,after}-raw.json；独立审计 current-recovery-device-city-b1-telemetry-audit.json。


### 农业操作与操作后完整应用复测

17入口组合在农业选择菜单的55秒为58.970435游戏FPS、28.242684显示FPS，新增欠载0；四次存档39原始tick。随后通过真机按键完成农业：金190→176、农业30→41、命令3→2，回到城市命令菜单，日期仍189年1月。真实消耗14金/增加11，与离线fixture20金/增加15分别记录，不混用结果。

操作后55秒为59.150736游戏FPS、29.520902显示FPS，新增欠载/丢弃/失败/视频错误均0，追赶32帧；四次完整存档40原始tick，native与缓存计数没有增长。该窗口验证操作后稳定性，因游戏资源已改变，不把它与先前城市窗口硬算匹配A/B。125/126截图确认正常城市画面。读取现有8320字节聚合缓冲及8192+128注册RAM作操作后备份；没有向游戏数据RAM写入，也未将此快照称为实际磁盘内容或有效游戏内Continue存档。

完整普通版隔离构建与CPU/PPU/audio/save五对象的已验收候选逐字节一致，必要UI/音频/保存回归通过。普通BDA SHA2569439b239688e9bf9916fc9132ea215adb93f8c0e19f467443af868106b776181；没有安装。设备仍是旧PROFILE前端上的17入口RAM组合，Debug7143fbe9保持运行。仍待消除音乐数据变化触发的周期性同步保存，不能把零欠载计数等同于连续音频没有存档停播。


### 2026-10-17：精确 SGZ2 周期存档比较的第一轮真机结果

在已经完成农业操作的同一城市（176 金、农业 41、2 次命令）中，18 入口 RAM 候选的第一段 55 秒无 USB 干扰窗口达到 60.11298 模拟帧/秒、30.05649 显示帧/秒（配置跳帧 1），新增音频欠载、额外追赶跳帧和完成的存档暂停均为 0。17 入口同场景为 59.15074/29.52090 帧/秒，4 次存档暂停共 40 个设备原始 tick。平均吞吐增加约 1.63%，主要变化是取消音乐状态引起的周期停顿。

此策略只对完整身份匹配的 SGZ2-CN 生效，比较时排除经冷启动续玩验证的 12 个音乐字节；其余 8308 字节精确比较。实际 checkpoint 仍完整写入并验证 8320 字节。该窗口只是稳定城市场景，不代表已经验证新的实际进度操作、所有战斗或真实 DAC 连续性。两张同场景截图有 63 像素差异，不声称逐像素相同。

证据：`build/hardware-20261016/current-persist-device-city-b1-telemetry-audit.json`（SHA256 `083250c1415ca047c8271c87ddcb803df806d28eeadbbd190dd9fa0905c11bff`）。物理执行的是旧 0.1.12 PROFILE 外壳加当前 RAM 闭包；尚未安装普通完整 BDA。Debug 保持开启。


### 2026-10-17：18 入口候选完成真实城市流程

四个独立 55 秒静默窗口期间无 USB 访问、截图、输入或 EXEC：

| 场景 | 模拟 FPS | 实际显示 FPS | 新增音频欠载 | 完成存档暂停 |
|---|---:|---:|---:|---:|
| 城市 176 金 / 农业 41，第一次 | 60.11298 | 30.05649 | 0 | 0 |
| 同一城市重复 | 60.10422 | 30.05211 | 0 | 0 |
| 农业选将 | 60.09496 | 28.52241 | 0 | 0 |
| 完成农业后的城市 160 金 / 农业 58 | 60.09507 | 30.04754 | 0 | 0 |

配置跳帧为 1。农业窗口仍有 644 个追赶批次，不能声称全程只有固定跳帧；其余三个窗口追赶为零。全部窗口的丢样、提交失败和原生编译统计增量为零。稳定城市的平均速度达到该 ROM 的约 60.10 FPS 节拍；这不是所有战斗场景或整场游戏通关证明，也不是物理 DAC 听音验收。

实际输入完成农业：消耗 16 金，农业增加 17，命令从 2 减为 1。owned 比较基线从 1052 代更新至 1054 代，包含新值 160 金 / 农业 58 / 1 命令，完整 8320 字节 CRC 匹配。随后通过游戏自己的“完成”命令执行记录，屏幕显示“189 年 1 月，命令完成”，剩余命令为 0；基线更新到 1055 代、slot0。独立读取 WRAM+IRAM 与该完整基线相等，后 4KiB 游戏备份保留 160 金 / 农业 58，标识为 `4944`，游戏内部校验和为 52778，与存储值一致。这里只把它作为已有完整回读验证路径提交的内存证据，不冒充重新打开磁盘或真机重启测试。

与前面的离线 170 金 / 农业 45 冷 Continue 900 帧实验区分：这次物理操作结果是 160 / 58。两者都证明真实游戏进度未被音乐字节过滤吞掉。

四窗口和物理记录独立汇总：`build/hardware-20261016/current-persist-device-final-telemetry-summary.json`，SHA256 `91fd9b196816bd26ae84211588bb0e6838f3f62633249e8e9dca151c0dacab2e`。

普通版五文件实现已合入，根目录 111 个构建输入与隔离构建逐一一致。候选为 `build/hardware-20261017/NES-v0.1.15-candidate/H1FC.bda`，SHA256 `788c153ee044404ceb20ca7625d911d7ced05d3776127e1c6449c60d380d8835`。无 profile、诊断、benchmark、自动帧数限制。完整普通 BDA 的选择器取消、真实电池存档载入、菜单退出完整保存、重新载入和资源释放均通过离线 MIPS 应用测试；真机速度证据仍对应旧外壳上的等效 RAM 闭包。未覆盖 dist、未安装到 H1、未发布 GitHub。

最终交接：已显式恢复 18 个原始函数入口，独立读回 1,476,608 字节原生缓存引用元数据为零，再释放临时 RAM 分配。游戏停在暂停菜单。H1Debug 7143fbe9 仍运行（state1、usb_fault0、last_error0、usb_resets2），没有关闭 Debug 或安装 BDA。机内恢复的是原已安装版本，优化普通版由用户最终更新。完整交接证据见 `current-persist-device-handoff.json`。
