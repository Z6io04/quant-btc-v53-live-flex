"""Use a holding-specific continuation gate without changing any entry signal."""
from capture import capture_frames


def holding_strong(direction, context):
    aligned = context['plus_di'] > context['minus_di'] if direction == 1 else context['minus_di'] > context['plus_di']
    return bool(direction and context['adx'] is not None and context['adx'] >= 25
                and aligned and context['spread_atr'] >= .5 and context['extension_atr'] >= 0)


def holding_confirmation_frames(bars, base, context):
    original, stats = capture_frames(bars, base, context, 'breakout')
    frames = {}
    for t, frame in original.items():
        # Entry was already gated by the original strong condition, including extension<=2.
        # Only the engine's existing-position confirmation reads this revised boolean.
        frames[t] = dict(frame, strong=holding_strong(frame['trend'], context[t]))
    return frames, stats
