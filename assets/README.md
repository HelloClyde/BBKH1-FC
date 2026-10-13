# H1 FC 素材

## 中文菜单字体

从 0.1.2 起，菜单使用 Noto Sans CJK SC Regular 的 18×21 单色字形子集，
按 SIL Open Font License 1.1 分发。字体版权属于 Adobe（2014–2021），
许可全文见 [licenses/NotoSansCJK-OFL.txt](licenses/NotoSansCJK-OFL.txt)。

字体固定于 [noto-cjk 提交 f8d1575](https://github.com/notofonts/noto-cjk/tree/f8d157532fbfaeda587e826d4cd5b21a49186f7c)，
文件为 `Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf`，SHA-256：
`2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`。

普通构建直接使用仓库内的 `src/platform/menu_font.h`，无需系统字体或联网下载字库。
修改菜单文字后，在本仓库根目录重新生成：

```console
python -m pip install Pillow==12.3.0
python tools/generate_menu_font.py
python tools/generate_menu_font.py --check
```

生成器从 `src/platform/*.c` 收集中文和 ASCII 字符，下载固定 OTF 到忽略的
`build/font-cache/` 并验证 SHA-256。也可通过 `--font path/to/NotoSansCJKsc-Regular.otf`
使用已下载且摘要一致的字体。绘制参数为 18 像素、基线 16、阈值 96，保留拉丁字宽，
中文步进 20 像素；超出单元格的字形会使生成失败，避免静默裁切。

## 应用图标

`fc-icon.png` 为本项目使用内置 imagegen 生成、编辑的原创手柄图标，已目视检查。
0.1.1 使用 RGBA PNG，四角 alpha=0；橙色圆角底板外部透明。
H1 SDK 在打包时从此图片生成四种原生菜单资源，不依赖外部图片服务。
资源 0（45×45）和资源 1（57×57）均为 RGB565 + alpha，保留透明度；
资源 2/3 是固件定义的无 alpha RGB565 格式。

初版生成提示词（0.1.0；其中 opaque background 导致原来的黑底问题）：

> Use case: logo-brand. Asset type: square app icon for an original homebrew FC / NES emulator on a vintage handheld dictionary. A single chunky cream and burgundy red rectangular 8-bit game controller, black plus-shaped d-pad on left, two red round buttons on right, two tiny dark start/select pills in middle. Polished small 3D object, softly beveled, friendly glossy 2000s handheld app icon look. Centered front view with slight tilt, on a rounded square warm orange gradient tile. Strong clear silhouette readable at 48 pixels, large subject fills about 80 percent, no cable, no console screen, no text, no logos, no watermark. 1:1 square opaque background.

0.1.1 编辑提示词（`transparent_background=true`）：

> Edit target: the attached H1 FC app icon. Make ONLY the area OUTSIDE the orange rounded-square tile fully transparent with actual alpha=0. Preserve the entire original orange rounded-square glossy tile, red/cream controller, its black d-pad, buttons, colors, tilt, lighting and proportions. Remove the nearly black exterior around all four rounded corners and edges completely, including any dark outer halo. Keep dark details and shadows INSIDE the orange tile unchanged. Do not redesign or redraw the icon, do not remove the orange tile, do not add text. Clean anti-aliased boundary of the rounded square against true transparent pixels. Output RGBA PNG with transparent exterior, never black or white painted background, no checkerboard baked in.

验证源码与实际 BDA/ZIP：`python tests/check_icon.py --zip dist/H1FC-install.zip`。
