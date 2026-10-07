"""The selected v53 model with unchanged quality/protection rules.

Returned frame objects are read-only inputs to the model. The single model
retains the exact v53 frames and configuration from the five-model observer.
"""
from collections import Counter
from dataclasses import replace

from model_engine import Config
from strategy import HOUR, aggregate, features, strength_context
from confirmation import holding_confirmation_frames

VARIANTS=('v53',)


def config_for(variant,capital):
    if variant not in VARIANTS:raise ValueError(('Unknown variant',variant))
    cfg=Config(capital=capital,risk=.06,halt_dd=.6,cooldown=6,
        staged_entry=True,confirm_replenish=True,confirm_stop_floor_zero=True,
        confirm_capacity_multiple=2)
    return replace(cfg,confirmed_profit_protection=True)


def entry_variants(original,hours,context):
    """Transform original accepted breakout seeds without refreshing setups.

    Exposed separately for synthetic boundary and causality checks. ``hours``
    contains completed bars whose timestamp is their close/known time.
    """
    by_time={b[0]:b for b in hours}
    close_frames={};survival_frames={};pending=None
    close_stats=Counter();survival_stats=Counter()
    for t,frame in sorted(original.items()):
        b=by_time[t];seed=frame['entry'];a=dict(frame);s=dict(frame)
        if seed:
            close_stats['baseline_signals']+=1
            width=b[2]-b[3]
            score=((b[4]-b[3]) if seed['direction']==1 else (b[2]-b[4]))/width if width>0 else None
            if score is None or score<.75:
                a['entry']=None;close_stats['close_location_rejected']+=1
            else:
                a['entry']=dict(seed,breakout_close_location=score)
                close_stats['accepted_signals']+=1
        close_frames[t]=a
        s['entry']=None;confirmed=False
        if pending is not None:
            original_seed=pending;d=original_seed['direction']
            due=original_seed['known_at']+HOUR
            if t>=due:
                survived=(t==due and frame['trend']==d and context[t]['strong']
                    and d*(b[4]-original_seed['level'])>0
                    and (b[3]>original_seed['stop'] if d==1 else b[2]<original_seed['stop']))
                if survived:
                    s['entry']=dict(original_seed,close=b[4],atr=frame['atr'],known_at=t,
                        breakout_seed=dict(original_seed),entry_confirmation_hours=1)
                    confirmed=True;survival_stats['accepted_signals']+=1
                else:survival_stats['confirmation_invalidated']+=1
                pending=None
        if seed:
            survival_stats['baseline_signals']+=1
            if confirmed:
                survival_stats['same_hour_seed_consumed']+=1
            elif pending is None:
                pending=dict(seed);survival_stats['confirmation_armed']+=1
        survival_frames[t]=s
    survival_stats['pending_at_end']=int(pending is not None)
    return {'v44':dict(frames=close_frames,stats=dict(close_stats)),
            'v45':dict(frames=survival_frames,stats=dict(survival_stats))}


def prepare_variants(bars):
    base,_=features(bars)
    context=strength_context(bars,base)
    original,stats=holding_confirmation_frames(bars,base,context)
    hours=aggregate(bars,HOUR)
    entries=entry_variants(original,hours,context)
    # v53 uses exact v48 entry frames plus the existing v47 protection flag.
    entries['v53']=v48_body_filter(entries['v44']['frames'],hours)
    return {variant:(entries[variant] if variant in entries else
        dict(frames=original,stats=dict(stats))) for variant in VARIANTS}


def v48_body_filter(v44,hours):
    """Exact v48 filter on existing v44 seeds, never regenerate setups.

    Input hours are complete bars at their known time; all holding fields and
    existing seed metadata remain unchanged. Equality at 0.5 passes.
    """
    by_time={b[0]:b for b in hours};frames={};stats=Counter()
    for t,frame in sorted(v44.items()):
        b=by_time[t];seed=frame['entry'];out=dict(frame)
        if seed:
            stats['baseline_v44_signals']+=1
            width=b[2]-b[3]
            ratio=seed['direction']*(b[4]-b[1])/width if width>0 else None
            if ratio is None or ratio<.5:
                out['entry']=None;stats['directional_body_rejected']+=1
            else:
                out['entry']=dict(seed,directional_body_ratio=ratio)
                stats['accepted_signals']+=1
        frames[t]=out
    return dict(frames=frames,stats=dict(stats))
