# Sources and licenses

- H1 FC is by HelloClyde and is a GPL-2.0 project; see `LICENSE`. Its native runtime, audio,
  input, scaler, menu, save system and tests are adapted from the parent
  [H1 GBA project](https://github.com/HelloClyde/BBKH1-GBA). The new FC adapter and homebrew test ROM
  generation scripts are included under the same license.
- The original freestanding runtime in H1 GBA was adapted from
  [HelloClyde/BBK9588-gba](https://github.com/HelloClyde/BBK9588-gba), commit
  `57c2792ec374b394df24c0ee898bb8624cf95a85`. No BBK9588 firmware addresses
  are used by this H1 application.
- [libretro/FCEUmm](https://github.com/libretro/libretro-fceumm), commit
  `ac0d00a7fc01166f84c7bac01d1084ebfd2b865e`, supplies the NES CPU, PPU,
  APU and mappers. Preserve its `Copying`, `Authors` and all source notices.
  Included libretro-common components retain their individual notices.
  `tools/prepare_core.py` applies H1 changes to a generated build copy.
- [H1 BDA SDK](https://github.com/MrDefinition1999/bbk-h1-bda-sdk), commit
  `067fe072477861dfc8949d7b1a55279fb92d2548`, is Apache-2.0, with its own
  LICENSE and NOTICE. The build uses the pinned `sdk/` submodule.
- `src/platform/menu_font.h` is a rendered glyph subset of Noto Sans CJK SC,
  Copyright 2014–2021 Adobe. It is distributed under SIL Open Font License 1.1;
  see `assets/licenses/NotoSansCJK-OFL.txt` and `assets/README.md` for the fixed
  source revision, checksum and regeneration command. No Windows font is needed.
- `assets/fc-icon.png` was generated for this project with the built-in
  imagegen tool. See `assets/README.md` for the prompt and use.
- No commercial game ROM, Nintendo BIOS, BBK firmware or NAND image is
  distributed. The generated `H1FC-TEST.nes` is original 6502 homebrew.
- Some validation screenshots show Matt Hughson's
  [From Below](https://mhughson.itch.io/from-below), with music/sound by Tui
  and art by Haller Zoltan. Its author-published ROM was used locally for
  emulator testing and is not included in the installation archive.

These projects and their trademarks belong to their respective authors.
