"""Separate causal entry timing / volume mechanisms with no threshold fitting."""
from collections import Counter
from strategy import refinement_frames


def quality_frames(bars,base,context,variant):
    assert variant in ['v18','v23','v24','v25']
    original,stats=refinement_frames(bars,base,context,'v5')
    if variant in ['v18','v25']:return original,stats
    return transform(original,base,context,variant)


def transform(original,base,context,variant):
    out={};stats=Counter()
    for t,frame in original.items():
        f=dict(frame)
        if variant=='v23':
            seed=base[t].get('breakout');f['entry']=None
            if seed:
                stats['breakout_seeds']+=1
                if context[t]['strong']:f['entry']=dict(seed)
                else:stats['strength_rejected']+=1
        elif f['entry']:
            stats['baseline_retests']+=1
            s=f['entry']
            if s['retest_volume']>s['breakout_volume']:
                f['entry']=None;stats['volume_rejected']+=1
        stats['accepted_signals']+=f['entry'] is not None
        out[t]=f
    return out,dict(stats)
