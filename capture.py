"""Causal frame adapter; strategy mechanics and thresholds remain frozen."""
from quality import quality_frames


def capture_frames(bars, base, context, style):
    assert style in ('retest', 'breakout')
    original, stats = quality_frames(bars, base, context, 'v25' if style == 'retest' else 'v23')
    out = {}
    for t, frame in original.items():
        cx = context[t]
        assert cx['last_4h_close'] <= t
        out[t] = dict(frame, strong=cx['strong'],
                      trail_long_4h=cx['trail_long_4h'],
                      trail_short_4h=cx['trail_short_4h'])
    return out, stats
