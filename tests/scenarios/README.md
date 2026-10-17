# Deterministic local-ROM scenarios

`sgz2-newgame-city.json` drives the H1 firmware key queue for the local
Chinese 三国志 II / 霸王的大陆 ROM (SHA-256
`7b4fcd1c0be33e69943dcf9c81cca67bac634a9de2881aa2f3d8f9b6a3864117`).
The ROM, screenshots, music and saves are not included in the repository.

Start without a save and run 1,800 frames. For comparisons involving 0.1.10 or
later, pass `--skip 0` or `--skip 1` explicitly: the harness loads the same
CRC-checked configuration through the ordinary file interface in both builds.
This separates optimization gains from the new default skip setting.
Enter selects single-player and difficulty 1; Z selects Liu Bei and
confirms. Later Z presses advance the turn prompt, choose Bingzhou and open its
city command list. Every press has a separate release. At frame 1,800,
`--menu-exit` sends P to open the pause menu and then confirms Exit. The same
menu sequence works on the older baseline. This covers a new campaign and
city interaction; it does not cover a battle or claim physical H1 performance.

Example baseline capture (from the repository root):

```powershell
python tests/jit_app_long_benchmark.py `
  --baseline-bda build/jit-v4-baseline/release-jit/H1FC.bda `
  --baseline-elf build/jit-v4-baseline/release-jit/H1FC.elf `
  --rom build/audio-baseline/sgz2-cn.nes `
  --frames 1800 --input-script tests/scenarios/sgz2-newgame-city.json `
  --declared-state --allow-audio-catchup --menu-exit --skip 0 --baseline-only `
  --output build/verification/jit-menu-exit-city-baseline1800.json
```

For a candidate comparison, use the same scenario arguments, remove
`--baseline-only`, add `--baseline-cache` pointing to that report, and supply the
candidate's `--bda`, `--elf` and a distinct `--output` path. Cache matching checks
both exact executable hashes and the input/settings fingerprint.

`--menu-exit` identifies the P-menu-confirm protocol explicitly: historical
v0.1.5/v0.1.6 caches captured without it cannot be reused. The original default identity
and direct-Escape behavior are retained for reproducing those old results.
In the new mode, final gameplay LCD, state and PCM observations stop at the
first audio pause/close, before the menu changes the LCD. Total application
instruction counts still include each build's actual menu exit path, including
menu drawing and confirmation; the frame totals exclude that later work and
`teardown_instructions` records it separately. Do not interpret an old/new
total-cost difference caused by the exit UI as a gameplay optimization.

The optional `--declared-state` observes the pinned core's CPU, PPU, APU, input
and mapper SFORMAT tables directly, without executing guest instructions or
calling save callbacks. It adds coverage beyond the original CPU/PPU memory
hash, but is not a serializer-generated savestate. The original default mode
and its cached 600-frame title benchmark remain compatible.

The longer scene activates the existing audio catch-up policy on the reference
build. --allow-audio-catchup records that policy in the scenario identity and
requires identical rendered-frame counts, catch-up counts, every LCD frame and
audio sample sequence. It does not disable rendering or change guest code.
Without catch-up, the title test requires the rendered-frame count specified
by `--skip`. Omitting `--skip` preserves the historical empty-filesystem cache
identity and skip-0 assertions, for reproducing older results only. The optional
`--native-counter` changes the host instruction collector, not the scenario or
guest execution; `tests/native_counter_smoke.py` checks parity with Python.
