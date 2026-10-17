"""Add sampled CPU/APU scopes to a fully prepared diagnostic core tree.

Run this pass only for a profile build, after prepare_core and prepare_jit.
Function bodies are instrumented in place: names, signatures, callback identity,
and generated native instructions are unchanged. Native inline clock work stays
in the native category; the remaining C time includes dispatch and fallback.
"""
from pathlib import Path
import re


_MARKER = "/* H1_PROFILE_CPU_DETAIL_PASS */\n"
_LITERALS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                       re.DOTALL)


def _code_mask(text):
    """Keep offsets/newlines, but hide braces and returns inside C literals."""
    return _LITERALS.sub(lambda match: ''.join('\n' if c == '\n' else ' '
                                               for c in match.group()), text)


def _function_bounds(text, name):
    masked = _code_mask(text)
    definitions = list(re.finditer(r'\b' + re.escape(name) + r'\s*\([^;{}]*?\)\s*\{', masked))
    assert len(definitions) == 1, (name, 'expected one function definition', len(definitions))
    opening = definitions[0].end() - 1
    depth = 1
    for end in range(opening + 1, len(masked)):
        depth += (masked[end] == '{') - (masked[end] == '}')
        if depth == 0:
            return opening + 1, end
    raise AssertionError((name, 'unbalanced function body'))


def _scope_function(text, name, mask, kind, result_type=None):
    """Leave on every return, preserving conditional control and evaluation."""
    start, end = _function_bounds(text, name)
    body = text[start:end]
    token = 'h1_profile_detail_cpu_token'
    result = 'h1_profile_detail_cpu_result'
    assert token not in body and result not in body, name
    returns = list(re.finditer(r'\breturn\b(?P<value>[^;]*);', _code_mask(body)))
    if result_type:
        assert returns, (name, 'non-void body has no return')
    for match in reversed(returns):
        value = body[match.start('value'):match.end('value')].strip()
        if result_type:
            assert match.group('value').strip(), (name, 'empty non-void return')
            # The original expression must run before leave, including reads
            # of state or side effects; an added block preserves if/else scope.
            replacement = (f'{{ {result_type} {result} = ({value}); '
                           f'h1_profile_detail_leave({token}); return {result}; }}')
        else:
            assert not match.group('value').strip(), (name, 'unexpected void return value')
            replacement = f'{{ h1_profile_detail_leave({token}); return; }}'
        body = body[:match.start()] + replacement + body[match.end():]
    opening = f'\n    unsigned {token} = h1_profile_detail_enter({mask}, {kind});\n'
    closing = '' if result_type else f'\n    h1_profile_detail_leave({token});\n'
    return text[:start] + opening + body + closing + text[end:]


def _replace_once(text, original, replacement):
    assert text.count(original) == 1, (original, text.count(original))
    return text.replace(original, replacement, 1)


def _mapper_call(text, argument, known_namco):
    original = f'MapIRQHook({argument});'
    # In JIT builds the known Namco function has its own scope, including the
    # direct native call. Only opaque C callbacks need another boundary here.
    admission = ('MapIRQHook == NamcoIRQHook ? 0u : ' if known_namco else '')
    replacement = ('{\n'
        '            unsigned h1_profile_mapper_token = ' + admission +
        'h1_profile_detail_enter(HPD_EVENT, HP_MAPPER_SAMPLE);\n'
        f'            MapIRQHook({argument});\n'
        '            h1_profile_detail_leave(h1_profile_mapper_token);\n'
        '        }')
    return _replace_once(text, original, replacement)


def _include(text):
    assert _MARKER not in text, 'CPU detail pass must run exactly once'
    return _MARKER + '#include "platform/profile.h"\n' + text


def prepare_profile_cpu(core, jit=True):
    """Instrument only the caller-owned, already prepared profile source tree.

    Mapper scopes cover the admitted Namco native callback and every C mapper
    callback. An unknown mapper called directly by generic generated code is
    intentionally still part of native time: the emitter is not rewritten.
    LQ synthesis covers both actual standard workers and Namco wavetable audio;
    the application's fixed Low quality does not use the HQ worker functions.
    """
    core = Path(core)
    paths = {name: core / name for name in ('src/x6502.c', 'src/sound.c', 'src/boards/n106.c')}
    # Prepare all strings before writing, so a stale pinned-source marker fails
    # without leaving a partially instrumented generated tree.
    cpu, sound, namco = (paths[name].read_text() for name in paths)
    assert all(_MARKER not in text for text in (cpu, sound, namco)), 'CPU detail pass already applied'

    cpu = _scope_function(cpu, 'X6502_Run', 'HPD_CPU', 'HP_CPU_SAMPLE')
    if jit:
        cpu = _scope_function(cpu, 'h1_cpu_clock_slow', 'HPD_EVENT', 'HP_EVENT_SAMPLE')
        cpu = _scope_function(cpu, 'h1_cpu_jit_complete', 'HPD_EVENT',
                              'HP_COMPLETE_SAMPLE', result_type='uint32_t')
        cpu = _replace_once(cpu, 'uint32_t result = run(&X,RAM);',
            'unsigned h1_profile_native_token = h1_profile_detail_enter(HPD_NATIVE, HP_NATIVE_SAMPLE);\n'
            '                uint32_t result = run(&X,RAM);\n'
            '                h1_profile_detail_leave(h1_profile_native_token);')
        cpu = _mapper_call(cpu, 'cycles', known_namco=True)
        namco = _scope_function(namco, 'NamcoIRQHook', 'HPD_EVENT', 'HP_MAPPER_SAMPLE')
    # The original interpreter body is also retained in a JIT build for public
    # interpreter fallback. This block preserves its one evaluation of temp.
    cpu = _mapper_call(cpu, 'temp', known_namco=jit)
    sound = _scope_function(sound, 'FCEU_SoundCPUHook', 'HPD_EVENT', 'HP_APU_EVENT_SAMPLE')
    for worker in ('RDoSQLQ', 'RDoTriangleNoisePCMLQ'):
        sound = _scope_function(sound, worker, 'HPD_SOUND', 'HP_APU_SYNTH_SAMPLE')
    namco = _scope_function(namco, 'DoNamcoSound', 'HPD_SOUND', 'HP_APU_SYNTH_SAMPLE')

    for name, text in zip(paths, (cpu, sound, namco)):
        paths[name].write_text(_include(text), newline='\n')
