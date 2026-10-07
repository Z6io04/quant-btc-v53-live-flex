"""Frozen, causal BTC trend/retest signals. Standard library only."""
import csv
import hashlib
import json
from bisect import bisect_right
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1]
BASE = WORK / 'outputs/okx-funding-arb-v1/data'
MINUTE, HOUR, DAY = 60000, 3600000, 86400000


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(sample):
    folder = BASE / 'perpetual' / sample
    meta = json.loads((folder/'metadata.json').read_text())
    assert sha(folder/'candles.csv') == meta['candle_sha256']
    assert sha(folder/'funding.json') == meta['funding_sha256']
    with (folder/'candles.csv').open() as f:
        bars = [(int(x['ts']), *[float(x[k]) for k in ['open','high','low','close','volume']]) for x in csv.DictReader(f)]
    assert len(bars) == (meta['end']-meta['warm_start'])//MINUTE
    for i, b in enumerate(bars):
        assert b[0] == meta['warm_start']+i*MINUTE
        assert 0 < b[3] <= min(b[1], b[4]) <= max(b[1],b[4]) <= b[2] and b[5] >= 0
    funding = {x['ts']:x['rate'] for x in json.loads((folder/'funding.json').read_text())}
    marks = {x[0]:x for x in json.loads((BASE/(sample+'-mark.json')).read_text())}
    assert all(t in marks for t in funding if meta['warm_start']<=t<meta['end'])
    spec = json.loads((BASE/'swap-spec.json').read_text())['response']['data'][0]
    tier = json.loads((BASE/'tiers.json').read_text())['response']['data'][0]
    params = dict(step=float(spec['ctVal'])*float(spec['ctMult'])*float(spec['lotSz']),
        minimum=float(spec['ctVal'])*float(spec['ctMult'])*float(spec['minSz']), tick=float(spec['tickSz']),
        mmr=float(tier['mmr']), max_quantity=float(tier['maxSz'])*float(spec['ctVal']))
    return bars, funding, marks, params, meta


def aggregate(bars, interval):
    out, current = [], []
    key = None
    for b in bars:
        bucket = b[0]//interval*interval
        if bucket != key:
            if current:
                assert len(current) == interval//MINUTE
                out.append((key+interval,current[0][1],max(x[2] for x in current),
                    min(x[3] for x in current),current[-1][4],sum(x[5] for x in current)))
            key, current = bucket, []
        current.append(b)
    if current and len(current)==interval//MINUTE:
        out.append((key+interval,current[0][1],max(x[2] for x in current),
            min(x[3] for x in current),current[-1][4],sum(x[5] for x in current)))
    return out


def ema(values, period):
    out=[]; last=values[0]; alpha=2/(period+1)
    for x in values:
        last=alpha*x+(1-alpha)*last;out.append(last)
    return out


def atr(bars, period=14):
    result=[]; value=0
    for i,b in enumerate(bars):
        prev=bars[i-1][4] if i else b[1]
        tr=max(b[2]-b[3],abs(b[2]-prev),abs(b[3]-prev))
        value=(value*i+tr)/(i+1) if i<period else (value*(period-1)+tr)/period
        result.append(value)
    return result


def features(bars):
    hours=aggregate(bars,HOUR); fours=aggregate(bars,4*HOUR)
    ft=[b[0] for b in fours]; close=[b[4] for b in fours]
    fast=ema(close,20);slow=ema(close,50);av=atr(hours)
    trend=[]
    for i,b in enumerate(fours):
        d=0
        if i>=49:
            if b[4]>fast[i]>slow[i] and fast[i]>fast[i-3]:d=1
            if b[4]<fast[i]<slow[i] and fast[i]<fast[i-3]:d=-1
        trend.append(d)
    frames={};setup=None;stats=Counter()
    for i,b in enumerate(hours):
        if i<24:continue
        t,o,hi,lo,c,v=b;j=bisect_right(ft,t)-1
        if j<49:continue
        d=trend[j];a=av[i]
        last3=hours[i-2:i+1];last6=hours[i-5:i+1]
        frame=dict(ts=t,close=c,atr=a,trend=d,
            trail_long=min(x[3] for x in last6)-.75*a,
            trail_short=max(x[2] for x in last6)+.75*a,
            fresh_high=c>max(x[2] for x in hours[i-3:i]),
            fresh_low=c<min(x[3] for x in hours[i-3:i]),
            exit_long=close[j]<fast[j] and close[j-1]<fast[j-1],
            exit_short=close[j]>fast[j] and close[j-1]>fast[j-1], entry=None)
        if setup:
            sd,level,sa,formed=setup['direction'],setup['level'],setup['atr'],setup['index']
            fail=d!=sd or i-formed>12 or (c<level-sa if sd==1 else c>level+sa)
            if fail:
                stats['setups_invalidated']+=1;setup=None
            elif i>formed:
                retest=(lo<=level+.35*sa and c>level+.1*sa and c>o) if sd==1 else (
                    hi>=level-.35*sa and c<level-.1*sa and c<o)
                if retest:
                    stop=min(x[3] for x in last3)-.75*a if sd==1 else max(x[2] for x in last3)+.75*a
                    frame['entry']=dict(direction=sd,stop=stop,close=c,atr=a,known_at=t,
                        breakout_known_at=hours[formed][0],level=level,volume_ratio=setup['volume_ratio'],breakout_volume=setup['volume'],retest_volume=v)
                    stats['confirmed_retests']+=1;setup=None
        if setup is None and frame['entry'] is None and d:
            prior=hours[i-24:i];level=max(x[2] for x in prior) if d==1 else min(x[3] for x in prior)
            vm=sum(x[5] for x in hours[i-20:i])/20
            beyond=c>level+.1*a if d==1 else c<level-.1*a
            if beyond and vm>0 and v>=1.2*vm:
                setup=dict(direction=d,level=level,atr=a,index=i,volume_ratio=v/vm,volume=v)
                stop=min(x[3] for x in last3)-.75*a if d==1 else max(x[2] for x in last3)+.75*a
                frame['breakout']=dict(direction=d,stop=stop,close=c,atr=a,known_at=t,breakout_known_at=t,
                    level=level,volume_ratio=v/vm,breakout_volume=v)
                stats['breakouts']+=1
        frames[t]=frame
    return frames,dict(stats)


def directional_index(bars,period=14):
    """Wilder TR/DM smoothing; ADX first exists after period DX observations."""
    n=len(bars);plus=[0.]*n;minus=[0.]*n;adx=[None]*n
    tr_sum=up_sum=down_sum=0.;dx=[]
    for i in range(1,n):
        b,p=bars[i],bars[i-1]
        up=b[2]-p[2];down=p[3]-b[3]
        positive=up if up>down and up>0 else 0.
        negative=down if down>up and down>0 else 0.
        tr=max(b[2]-b[3],abs(b[2]-p[4]),abs(b[3]-p[4]))
        if i<=period:
            tr_sum+=tr;up_sum+=positive;down_sum+=negative
        else:
            tr_sum=tr_sum-tr_sum/period+tr
            up_sum=up_sum-up_sum/period+positive
            down_sum=down_sum-down_sum/period+negative
        if i>=period:
            plus[i]=100*up_sum/tr_sum if tr_sum else 0.
            minus[i]=100*down_sum/tr_sum if tr_sum else 0.
            denominator=plus[i]+minus[i]
            value=100*abs(plus[i]-minus[i])/denominator if denominator else 0.
            dx.append(value)
            if len(dx)==period:adx[i]=sum(dx)/period
            elif len(dx)>period:adx[i]=(adx[i-1]*(period-1)+value)/period
    return adx,plus,minus


def strength_context(bars,frames):
    fours=aggregate(bars,4*HOUR);ft=[b[0] for b in fours];prices=[b[4] for b in fours]
    fast=ema(prices,20);slow=ema(prices,50);a=atr(fours)
    adx,plus,minus=directional_index(fours);out={}
    for t,frame in frames.items():
        j=bisect_right(ft,t)-1;d=frame['trend'];vol=a[j]
        spread=d*(fast[j]-slow[j])/vol if vol else 0.
        extension=d*(frame['close']-fast[j])/vol if vol else 0.
        aligned=(plus[j]>minus[j]) if d==1 else (minus[j]>plus[j])
        accepted=bool(d and adx[j] is not None and adx[j]>=25 and aligned and spread>=.5 and 0<=extension<=2)
        out[t]=dict(last_4h_close=ft[j],adx=adx[j],plus_di=plus[j],minus_di=minus[j],
            spread_atr=spread,extension_atr=extension,strong=accepted,
            trail_long_4h=min(b[3] for b in fours[j-2:j+1])-.5*vol,
            trail_short_4h=max(b[2] for b in fours[j-2:j+1])+.5*vol)
        assert out[t]['last_4h_close']<=t
    return out


def variant_frames(base,context,variant):
    assert variant in ['v1','v2','v3','v4']
    out={};stats=Counter()
    for t,frame in base.items():
        f=dict(frame);cx=context[t]
        if variant in ['v2','v4']:
            f['trail_long']=cx['trail_long_4h'];f['trail_short']=cx['trail_short_4h']
        if f['entry']:
            stats['baseline_signals']+=1
            if variant in ['v3','v4'] and not cx['strong']:
                f['entry']=None;stats['strength_rejected']+=1
            else:stats['accepted_signals']+=1
        out[t]=f
    return out,dict(stats)


def refinement_frames(bars, base, context, variant):
    """Entry refinements use completed bars only; no outcome labels or future prices."""
    assert variant in ['v3','v5','v6','v7','v8','v9','v10']
    original,_=variant_frames(base,context,'v3')
    if variant in ['v3','v7','v8']:
        return original,dict(accepted_signals=sum(f['entry'] is not None for f in original.values()))
    hours=aggregate(bars,HOUR);indices={b[0]:i for i,b in enumerate(hours)}
    out={};pending=None;stats=Counter()
    for t,frame in original.items():
        f=dict(frame);sig=frame['entry'];f['entry']=None;i=indices[t];b=hours[i]
        if variant in ['v5','v9']:
            if sig:
                stats['baseline_signals']+=1
                touch=b[3]<=sig['level'] if sig['direction']==1 else b[2]>=sig['level']
                if touch:f['entry']=dict(sig);stats['accepted_signals']+=1
                else:stats['no_actual_touch']+=1
        else:
            if pending:
                seed=pending['signal'];d=seed['direction'];age=i-pending['index']
                structure_broken=b[3]<=seed['stop'] if d==1 else b[2]>=seed['stop']
                invalid=f['trend']!=d or d*(b[4]-seed['level'])<=0 or age>3 or structure_broken
                if invalid:pending=None;stats['confirmation_invalidated']+=1
                elif age>0 and d*(b[4]-pending['trigger'])>0 and context[t]['strong']:
                    last3=hours[max(0,i-2):i+1]
                    structure=min(x[3] for x in last3)-.75*f['atr'] if d==1 else max(x[2] for x in last3)+.75*f['atr']
                    stop=min(seed['stop'],structure) if d==1 else max(seed['stop'],structure)
                    f['entry']=dict(seed,stop=stop,close=b[4],atr=f['atr'],known_at=t,
                        retest_known_at=seed['known_at'])
                    pending=None;stats['accepted_signals']+=1
            if sig and pending is None and f['entry'] is None:
                pending=dict(signal=dict(sig),index=i,trigger=b[2] if sig['direction']==1 else b[3])
                stats['confirmation_armed']+=1
        out[t]=f
    return out,dict(stats)
