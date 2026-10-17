"""Verify native/Python counter parity on the same unmodified release BDA."""
import argparse
import json
from pathlib import Path
import jit_app_long_benchmark as benchmark
from native_block_counter import application_class
from make_test_rom import make_rom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bda', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--nm', type=Path, required=True)
    parser.add_argument('--rom', type=Path)
    parser.add_argument('--frames', type=int, default=32)
    parser.add_argument('--output', type=Path, default=benchmark.ROOT/'build/verification/native-counter.json')
    args = parser.parse_args()
    rom = args.rom.read_bytes() if args.rom else make_rom(sprites=True, dmc=True)
    output = args.output.with_suffix('')
    old = benchmark.execute(args.bda, args.elf, args.nm, rom, args.frames, [],
                            output/'python', False, True, True, True, 0)
    benchmark.LongApplication = application_class(benchmark.LongApplication)
    new = benchmark.execute(args.bda, args.elf, args.nm, rom, args.frames, [],
                            output/'native', False, True, True, True, 0)
    for key in old:
        if key != 'seconds_host':
            assert old[key] == new[key], key
    report = {'ok': True, 'frames': args.frames, 'bda_sha256': old['bda_sha256'],
              'rom_sha256': benchmark.digest(rom), 'counter_backend': new['counter_backend'],
              'exact_instruction_count': old['block_instructions'],
              'python_seconds': old['seconds_host'], 'native_seconds': new['seconds_host'],
              'checks': 'Every output field except host duration exactly equal, including each frame cost/state/PCM/LCD and total/startup/teardown costs'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
