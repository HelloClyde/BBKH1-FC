"""Exact LQ APU waveform batching; clock/DMA/IRQ boundaries stay unchanged."""
from pathlib import Path


LIMIT = r'''
/* A Wave cell receives 16 LQ oversamples. Aggregate only through the first
 * oscillator transition (the old amplitude includes that final sample), and
 * never across the cell boundary. Most cells need no division. Non-positive
 * restored phases retain the original one-sample repair/wrap behavior. */
static INLINE int32_t h1_lq_limit(int32_t count, int32_t acc, int32_t step) {
    if (acc <= 0) return 1;
    if (step && acc <= step * count)
        return ((uint32_t)acc - 1u) / (uint32_t)step + 1u;
    return count;
}
'''


def once(text, old, new):
    assert text.count(old) == 1, ('Pinned APU patch mismatch', old[:100])
    return text.replace(old, new, 1)


def prepare_apu(core):
    path = Path(core) / 'src/sound.c'
    text = path.read_text(encoding='utf-8')
    text = once(text, 'static void RDoSQLQ(void) {', LIMIT + '\nstatic void RDoSQLQ(void) {')
    first = text.index('static void RDoSQLQ(void) {')
    last = text.index('\nstatic void RDoTriangle(void)', first)
    square = text[first:last]
    square = once(square, '\tint32_t V;', '\tint32_t V, span;')
    square = once(square, 'for (V = start; V < end; V++) {',
                  'for (V = start; V < end; V += span) {')
    square = once(square, '\t\t\tWave[V >> 4] += totalout;\t/* tmpamp; */', '''
            span = 16 - (V & 15);
            if (span > end - V) span = end - V;
            span = h1_lq_limit(span, sqacc[0], inie[0]);
            span = h1_lq_limit(span, sqacc[1], inie[1]);
            Wave[V >> 4] += totalout * span;''')
    square = once(square, 'sqacc[0] -= inie[0];', 'sqacc[0] -= inie[0] * span;')
    square = once(square, 'sqacc[1] -= inie[1];', 'sqacc[1] -= inie[1] * span;')
    text = text[:first] + square + text[last:]
    first = text.index('static void RDoTriangleNoisePCMLQ(void) {')
    last = text.index('\nstatic void RDoNoise(void)', first)
    mixed = text[first:last]
    mixed = once(mixed, '\tint32_t V;', '\tint32_t V, span;')
    assert mixed.count('for (V = start; V < end; V++) {') == 3
    mixed = mixed.replace('for (V = start; V < end; V++) {',
                          'for (V = start; V < end; V += span) {')
    assert mixed.count('\t\t\tWave[V >> 4] += totalout;') == 4
    mixed = once(mixed, '''\t} else {
\t\tfor (V = start; V < end; V++)
\t\t\tWave[V >> 4] += totalout;
\t}''', '''\t} else {
        for (V = start; V < end; V += span) {
            span = 16 - (V & 15);
            if (span > end - V) span = end - V;
            Wave[V >> 4] += totalout * span;
        }
\t}''')
    mixed = mixed.replace('\t\t\tWave[V >> 4] += totalout;', '''
            span = 16 - (V & 15);
            if (span > end - V) span = end - V;
            if (inie[0]) span = h1_lq_limit(span, lq_triacc, inie[0]);
            if (inie[1]) span = h1_lq_limit(span, lq_noiseacc, inie[1]);
            Wave[V >> 4] += totalout * span;''')
    assert mixed.count('lq_triacc -= inie[0];') == 2
    assert mixed.count('lq_noiseacc -= inie[1];') == 2
    mixed = mixed.replace('lq_triacc -= inie[0];', 'lq_triacc -= inie[0] * span;')
    mixed = mixed.replace('lq_noiseacc -= inie[1];', 'lq_noiseacc -= inie[1] * span;')
    text = text[:first] + mixed + text[last:]
    path.write_text(text, encoding='utf-8', newline='\n')
