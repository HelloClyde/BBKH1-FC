# FCEUmm dependency

Official source: https://github.com/libretro/libretro-fceumm

Pinned commit: `ac0d00a7fc01166f84c7bac01d1084ebfd2b865e`.

License: GNU GPL version 2; see `fceumm/Copying`, `fceumm/Authors`, and the
individual source headers. Bundled libretro-common files carry their own
permissive license notices. H1-FC redistributions must preserve these notices
and provide the corresponding source, including the H1 patches/build scripts.

Recreate the dependency without following a moving branch:

```powershell
git clone https://github.com/libretro/libretro-fceumm.git h1-fc/third_party/fceumm
git -C h1-fc/third_party/fceumm checkout --detach ac0d00a7fc01166f84c7bac01d1084ebfd2b865e
```

`tools/prepare_core.py` verifies the revision and clean checkout, copies it to
the build directory, then applies explicit checked patches. The upstream
checkout remains unmodified. H1 builds include all mapper implementations,
RGB565 video, integer low-quality audio, and in-memory states. They omit HD
packs, the NTSC filter, and the NSF visualizer. The H1 cartridge adapter accepts
only iNES/NES 2.0 content up to 4 MiB plus header/trainer. It aggregates all four mapper save segments
for the frontend's battery-save storage. No game ROM or FDS BIOS is bundled.
