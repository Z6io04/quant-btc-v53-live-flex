"""Offline minute-bar simulation, isolated linear futures and controlled pyramiding.
No network, credentials, live orders or assumed intrabar take-profit sequence.
"""
from dataclasses import dataclass, asdict
from collections import Counter
from decimal import Decimal, ROUND_FLOOR
import math
from strategy import MINUTE,HOUR,DAY


@dataclass(frozen=True)
class Config:
    capital: float = 20
    risk: float = .02
    add: bool = False
    leverage: float = 15
    max_notional: float = 5
    reserve: float = .5
    fee: float = .0005
    slip: float = .0003
    latency: int = 1
    halt_dd: float = .20
    cooldown: int = 6
    failed_breakout_exit: bool = False
    profit_protection: bool = False
    add_size_multiple: float = .5
    add_stop_floor_zero: bool = False
    add_giveback_r: float = 2.0
    no_progress_hours: int = 0
    staged_entry: bool = False
    continuation_core: bool = False
    confirm_replenish: bool = False
    confirm_stop_floor_zero: bool = False
    confirm_capacity_multiple: int = 1
    confirm_min_interval_hours: int = 1
    confirmed_profit_protection: bool = False


def floor_quantity(x,step):
    return float((Decimal(str(max(x,0)))/Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(step)))


def fill(raw, side, cfg, tick):
    value=raw*(1+side*cfg.slip)/tick
    return (math.ceil(value-1e-9) if side==1 else math.floor(value+1e-9))*tick


def liquidation_price(p,mmr,fee):
    # margin + direction*q*(mark-average) = q*mark*(mmr+close_fee)
    d,q=p['direction'],p['quantity']
    return (d*q*p['average']-p['margin'])/(q*(d-mmr-fee))


def stop_total_pnl(p,stop,cfg,spec):
    price=fill(stop,-p['direction'],cfg,spec['tick'])
    return p['direction']*p['quantity']*(price-p['average'])-p['fees']+p['funding']-p['quantity']*price*cfg.fee


def stop_for_net(p,target,cfg,spec):
    """Invert modeled exit PNL; one tick allowance offsets adverse fill rounding."""
    d,q=p['direction'],p['quantity']
    target+=q*spec['tick']*(1+cfg.fee)
    exit_price=(target+d*q*p['average']+p['fees']-p['funding'])/(q*(d-cfg.fee))
    return exit_price/(1-d*cfg.slip)


def size_entry(cash,signal,raw,cfg,spec):
    d=signal['direction'];price=fill(raw,d,cfg,spec['tick']);stop=signal['stop']
    if d*(price-stop)<=0:return None,'invalid_stop'
    if abs(price-signal['close'])>.5*signal['atr']:return None,'entry_gap'
    stopped=fill(stop,-d,cfg,spec['tick'])
    unit_risk=d*(price-stopped)+(price+stopped)*cfg.fee
    full_budget=cash*cfg.risk
    budget=full_budget*.5 if cfg.staged_entry else full_budget
    affordable=cash*(1-cfg.reserve)/(price*(1/cfg.leverage+cfg.fee))
    quantity=floor_quantity(min(budget/unit_risk,affordable,cfg.max_notional*cash/price,spec['max_quantity']),spec['step'])
    if quantity<spec['minimum']-1e-12:return None,'minimum_lot'
    p=dict(direction=d,quantity=quantity,average=price,margin=quantity*price/cfg.leverage,
           fees=quantity*price*cfg.fee,funding=0.,stop=stop)
    liq=liquidation_price(p,spec['mmr'],cfg.fee)
    if d*(price-stop)>.6*d*(price-liq):return None,'stop_too_near_liquidation'
    assert -stop_total_pnl(p,stop,cfg,spec)<=budget+1e-8
    candidate=dict(**p,risk_budget=full_budget,initial_risk=-stop_total_pnl(p,stop,cfg,spec),
        initial_quantity=quantity,initial_price=price,initial_gap=d*(price-stop))
    if cfg.staged_entry:candidate['scout_risk_budget']=budget
    return candidate,None


def simulate(bars,frames,funding,marks,start,end,cfg,spec,spec_schedule=None,finalize=True):
    """Replay through end; finalize=False retains an observed, unclosed position.

    The optional state is an observation snapshot, not an incremental-resume
    checkpoint. Replaying an extended input prefix reproduces order state.
    """
    if cfg.staged_entry and cfg.add:
        raise ValueError('Staged confirmation and legacy additions are mutually exclusive.')
    if cfg.continuation_core and not cfg.staged_entry:
        raise ValueError('Continuation core requires staged entry.')
    if (cfg.confirm_replenish or cfg.confirm_stop_floor_zero or cfg.confirm_capacity_multiple!=1) and not cfg.staged_entry:
        raise ValueError('Confirmation controls require staged entry.')
    if cfg.confirm_capacity_multiple not in (1,2):
        raise ValueError('Confirmation capacity must be one or two initial quantities.')
    if cfg.confirm_capacity_multiple==2 and not (cfg.confirm_replenish and cfg.confirm_stop_floor_zero):
        raise ValueError('Second confirmation tranche requires replenishment and a nonnegative net stop floor.')
    if not isinstance(cfg.confirm_min_interval_hours,int) or cfg.confirm_min_interval_hours<1:
        raise ValueError('Confirmation interval must be a positive integer number of hours.')
    if (cfg.confirm_min_interval_hours!=1 or cfg.confirmed_profit_protection) and not cfg.staged_entry:
        raise ValueError('New confirmation controls require staged entry.')
    if cfg.confirmed_profit_protection and not cfg.confirm_stop_floor_zero:
        raise ValueError('Confirmed profit protection requires the nonnegative confirmation stop floor.')
    if (cfg.add or cfg.staged_entry) and cfg.profit_protection:
        raise ValueError('Profit-protection plus additions is unsupported: additions could weaken its protected floor.')
    assert 0<=cfg.add_size_multiple<=2
    assert 0<cfg.add_giveback_r<=3
    cash=cfg.capital;p=None;queued_entry=None;queued_manage=None;next_entry=start
    trades=[];ledger=[];curve=[];rejected=Counter();mark_flags={};peak=cfg.capital
    maxdd=0.;halted=False;halt_time=None;pending_halt=None;exposure=0;max_eff=0.;last=None

    def nav(raw,liquid=True):
        if p is None:return cash
        price=raw*(1-p['direction']*cfg.slip) if liquid else raw
        return cash+p['margin']+p['direction']*p['quantity']*(price-p['average'])-(p['quantity']*price*cfg.fee if liquid else 0)

    def book(action,t,amount,**kwargs):
        ledger.append(dict(action=action,ts=t,cash_change=amount,**kwargs))

    def close(t,raw,reason,liquidation=False):
        nonlocal cash,p,next_entry,queued_manage
        if p is None:return
        px=fill(raw,-p['direction'],cfg,spec['tick'])
        move=p['direction']*(px/p['initial_price']-1)*100
        p['max_favorable_pct']=max(p['max_favorable_pct'],move)
        p['max_adverse_pct']=min(p['max_adverse_pct'],move)
        gross=p['direction']*p['quantity']*(px-p['average']);fee=p['quantity']*px*cfg.fee
        clearance=p['quantity']*px*spec['mmr'] if liquidation else 0
        returned=p['margin']+gross-fee-clearance
        # Isolated collateral is the only modeled pool available to this position.
        deficit=max(0.,-returned);cash+=max(0.,returned)
        book('liquidate' if liquidation else 'close',t,max(0.,returned),quantity=p['quantity'],price=px,
             side=-p['direction'],gross=gross,fee=fee,clearance=clearance,deficit=deficit,reason=reason)
        net=cash-p['capital_at_entry']
        assert abs(net-(gross-p['fees']+p['funding']-fee-clearance+deficit))<1e-7
        trades.append(dict(**p,exit_ts=t,exit_price=px,reason=reason,gross_pnl=gross,
            total_fees=p['fees']+fee+clearance,net_pnl=net,end_equity=cash,
            holding_hours=(t-p['entry_ts'])/HOUR,net_r=net/p['risk_budget']))
        p=None;queued_manage=None;next_entry=t+cfg.cooldown*HOUR

    def mark_audit(t):
        if p is None:return
        hour=t//HOUR*HOUR;mb=marks.get(hour)
        if mb is None:raise AssertionError(('missing mark hour',hour))
        liq=liquidation_price(p,spec['mmr'],cfg.fee)
        extreme=mb[3] if p['direction']==1 else mb[2]
        if p['direction']*(extreme-liq)<=0:
            # Only a diagnostic; full-hour future extrema never enter trading decisions.
            key=(p['entry_ts'],hour,round(liq,3))
            mark_flags[key]=dict(entry_ts=p['entry_ts'],observed_state_ts=t,hour=hour,liq=liq,
                adverse_hour_mark=extreme,direction=p['direction'])

    def add(t,raw,frame):
        nonlocal cash
        if not cfg.add or p['add_count'] or t-p['entry_ts']<4*HOUR:return
        d=p['direction']
        if frame['trend']!=d or d*(frame['close']-p['initial_price'])<2*p['initial_gap']:return
        if not frame['fresh_high' if d==1 else 'fresh_low']:return
        if stop_total_pnl(p,p['stop'],cfg,spec)<0:return
        px=fill(raw,d,cfg,spec['tick'])
        if d*(px-p['stop'])<=0:return
        target=floor_quantity(cfg.add_size_multiple*p['initial_quantity'],spec['step'])
        equity=nav(raw);pre_stop=stop_total_pnl(p,p['stop'],cfg,spec)
        while target>=spec['minimum']-1e-12:
            q=p['quantity']+target;fee=target*px*cfg.fee;margin=target*px/cfg.leverage
            candidate=dict(p,quantity=q,average=(p['average']*p['quantity']+px*target)/q,
                fees=p['fees']+fee,margin=p['margin']+margin)
            loss_at_stop=stop_total_pnl(candidate,p['stop'],cfg,spec)
            stop_nav=p['capital_at_entry']+loss_at_stop
            liq=liquidation_price(candidate,spec['mmr'],cfg.fee)
            conditions=[cash-margin-fee>=cfg.reserve*equity,
                q*px<=cfg.max_notional*equity,q<=spec['max_quantity'],
                loss_at_stop>=(0. if cfg.add_stop_floor_zero else -p['risk_budget']),equity-stop_nav<=cfg.add_giveback_r*p['risk_budget'],
                d*(px-p['stop'])<=.6*d*(px-liq)]
            if all(conditions):
                cash-=margin+fee
                p.update(quantity=q,average=candidate['average'],fees=candidate['fees'],margin=candidate['margin'],add_count=1)
                event=dict(ts=t,quantity=target,price=px,fee=fee,margin=margin,stop=p['stop'],
                    pre_add_locked_pnl=pre_stop,post_add_stop_pnl=loss_at_stop,equity_before=equity,
                    giveback=equity-stop_nav,known_at=frame['ts'])
                p['adds'].append(event);book('add',t,-margin-fee,**{k:v for k,v in event.items() if k!='ts'})
                return
            target=floor_quantity(target-spec['step']+1e-12,spec['step'])
        rejected['add_risk_or_lot']+=1

    def confirm(t,raw,frame):
        """Confirmation using only the queued completed-hour observation.

        Default mode keeps the historical one-shot rule exactly. Optional
        replenishment consumes its cap by actual executed quantity, never by an
        attempted quantity; only a new completed hour at a better price may add.
        """
        nonlocal cash
        if not cfg.staged_entry or (p['add_count'] and not cfg.confirm_replenish):return
        # Observation age, not delayed execution age, determines eligibility.
        if frame['ts']-p['entry_ts']<HOUR:return
        d=p['direction']
        if frame['trend']!=d or not frame['strong']:return
        if d*(frame['close']-p['initial_price'])<p['initial_gap']:return
        if not frame['fresh_high' if d==1 else 'fresh_low']:return
        px=fill(raw,d,cfg,spec['tick'])
        if d*(px-p['stop'])<=0:return
        if cfg.confirm_replenish and p['add_count']:
            previous=p['adds'][-1]
            if frame['ts']//HOUR<=previous['known_at']//HOUR:return
            # Preserve the original bucket rule exactly when the new control is
            # disabled. The two-hour rule uses observations, not fill latency.
            if cfg.confirm_min_interval_hours!=1 and frame['ts']-previous['known_at']<cfg.confirm_min_interval_hours*HOUR:return
            if d*(frame['close']-previous['price'])<=0 or d*(px-previous['price'])<=0:return
        if abs(px-frame['close'])>.5*frame['atr']:
            rejected['confirmation_gap']+=1
            return
        new_mode=cfg.confirm_replenish or cfg.confirm_stop_floor_zero or cfg.confirm_capacity_multiple!=1
        if new_mode:
            tranche=2 if cfg.confirm_capacity_multiple==2 and min(d*(frame['close']-p['initial_price']),d*(px-p['initial_price']))>=2*p['initial_gap'] else 1
            capacity=Decimal(str(p['initial_quantity']))*tranche
            consumed=sum((Decimal(str(a['quantity'])) for a in p['adds']),Decimal(0))
            remaining=capacity-consumed
            target=floor_quantity(remaining,spec['step'])
            if target<spec['minimum']-1e-12:return
        else:
            target=floor_quantity(p['initial_quantity'],spec['step'])
        equity=nav(raw);pre_stop=stop_total_pnl(p,p['stop'],cfg,spec)
        required_floor=0. if cfg.confirm_stop_floor_zero else -p['risk_budget']
        if cfg.confirmed_profit_protection:
            required_floor=max(required_floor,p['protected_floor'])
        while target>=spec['minimum']-1e-12:
            q=p['quantity']+target;fee=target*px*cfg.fee;margin=target*px/cfg.leverage
            candidate=dict(p,quantity=q,average=(p['average']*p['quantity']+px*target)/q,
                fees=p['fees']+fee,margin=p['margin']+margin)
            stop_pnl=stop_total_pnl(candidate,p['stop'],cfg,spec)
            stop_nav=p['capital_at_entry']+stop_pnl
            liq=liquidation_price(candidate,spec['mmr'],cfg.fee)
            conditions=[cash-margin-fee>=cfg.reserve*equity,
                q*px<=cfg.max_notional*equity,q<=spec['max_quantity'],
                stop_pnl>=required_floor,equity-stop_nav<=2*p['risk_budget'],
                d*(px-p['stop'])<=.6*d*(px-liq)]
            if all(conditions):
                cash-=margin+fee
                p.update(quantity=q,average=candidate['average'],fees=candidate['fees'],
                    margin=candidate['margin'],add_count=p['add_count']+1 if new_mode else 1)
                event=dict(ts=t,stage='confirmation',quantity=target,price=px,fee=fee,
                    margin=margin,stop=p['stop'],pre_add_locked_pnl=pre_stop,
                    post_add_stop_pnl=stop_pnl,equity_before=equity,giveback=equity-stop_nav,
                    known_at=frame['ts'],signal_close=frame['close'],signal_atr=frame['atr'],
                    signal_trend=frame['trend'],signal_strong=frame['strong'],
                    signal_fresh_extreme=frame['fresh_high' if d==1 else 'fresh_low'])
                if new_mode:
                    cumulative=consumed+Decimal(str(target))
                    absolute_capacity=Decimal(str(p['initial_quantity']))*cfg.confirm_capacity_multiple
                    assert cumulative<=capacity<=absolute_capacity
                    event.update(confirmation_tranche=tranche,
                        confirmation_cumulative_quantity=float(cumulative),
                        confirmation_unlocked_capacity=float(capacity),
                        confirmation_remaining_quantity=float(capacity-cumulative),
                        confirmation_absolute_remaining_quantity=float(absolute_capacity-cumulative),
                        confirmation_stop_floor=required_floor)
                if cfg.confirm_min_interval_hours!=1:
                    event['confirmation_min_interval_hours']=cfg.confirm_min_interval_hours
                if cfg.confirmed_profit_protection:
                    event['confirmation_protected_floor']=p['protected_floor']
                p['adds'].append(event)
                book('add',t,-margin-fee,**{k:v for k,v in event.items() if k!='ts'})
                return
            target=floor_quantity(target-spec['step']+1e-12,spec['step'])
        rejected['confirmation_risk_or_lot']+=1

    for b in bars:
        t,o,hi,lo,c,vol=b
        if t<start:continue
        if t>=end:break
        if spec_schedule is not None:spec=spec_schedule(t)
        last=b
        # Positions existing before settlement pay/receive funding before new orders.
        if p and t in funding:
            mp=marks[t][1];amount=-p['direction']*p['quantity']*mp*funding[t]
            p['margin']+=amount;p['funding']+=amount
            book('funding',t,0.,margin_change=amount,rate=funding[t],quantity=p['quantity'],mark=mp)
        if p:
            mark_audit(t)
            liq=liquidation_price(p,spec['mmr'],cfg.fee)
            if p['direction']*(o-liq)<=0:close(t,o,'liquidation_proxy_gap',True)
            elif p['direction']*(o-p['stop'])<=0:close(t,o,'stop_gap')
        if pending_halt is not None and t>=pending_halt:
            close(t,o,'account_drawdown_halt');pending_halt=None
        if p and queued_manage and t>=queued_manage['due']:
            frame=queued_manage['frame'];no_progress=queued_manage.get('no_progress',False)
            protected_floor=queued_manage.get('protected_floor',0.);queued_manage=None
            d=p['direction']
            if cfg.failed_breakout_exit:
                p['failed_level_closes']=p['failed_level_closes']+1 if d*(frame['close']-p['breakout_level'])<0 else 0
            if no_progress:
                close(t,o,'no_progress_exit')
            elif cfg.failed_breakout_exit and p['failed_level_closes']>=2:
                close(t,o,'failed_breakout_exit')
            elif frame['exit_long' if d==1 else 'exit_short']:
                close(t,o,'four_hour_trend_exit')
            else:
                trail_key='trail_long' if d==1 else 'trail_short'
                # A confirmation frame still uses the ordinary trail. Only subsequent
                # completed observations can use the core's wider time scale.
                if cfg.continuation_core and p['add_count']:trail_key+='_4h'
                proposed=frame[trail_key]
                if cfg.confirmed_profit_protection and protected_floor>0:
                    # The monetary floor was fixed at the completed close. Its
                    # price inversion uses actual fees/funding at execution.
                    # An adverse opening gap exits at that open, never at an
                    # invented protective fill. Adds cannot move this stop.
                    protective=stop_for_net(p,protected_floor,cfg,spec)
                    proposed=max(proposed,protective) if d==1 else min(proposed,protective)
                    p['confirmed_protection_updates']+=1
                if cfg.profit_protection:
                    known_pnl=stop_total_pnl(p,frame['close'],cfg,spec)
                    p['peak_hourly_net']=max(p['peak_hourly_net'],known_pnl)
                    peak_r=p['peak_hourly_net']/p['risk_budget']
                    target=.5*p['peak_hourly_net'] if peak_r>=2 else 0. if peak_r>=1 else None
                    if target is not None:
                        protective=stop_for_net(p,target,cfg,spec)
                        proposed=max(proposed,protective) if d==1 else min(proposed,protective)
                        p['protection_updates']+=1
                newstop=max(p['stop'],proposed) if d==1 else min(p['stop'],proposed)
                assert d*(newstop-p['stop'])>=-1e-9
                p['stop']=newstop
                if d*(o-newstop)<=0:close(t,o,'trailing_stop_gap')
                else:
                    add(t,o,frame)
                    confirm(t,o,frame)
        if queued_entry and t>=queued_entry['due']:
            sig=queued_entry['signal'];queued_entry=None
            if p is None and not halted and t>=next_entry:
                candidate,reason=size_entry(cash,sig,o,cfg,spec)
                if candidate:
                    initial_cash=cash;cash-=candidate['margin']+candidate['fees']
                    p=dict(**candidate,entry_ts=t,known_at=sig['known_at'],breakout_known_at=sig['breakout_known_at'],
                        capital_at_entry=initial_cash,add_count=0,adds=[],max_favorable_pct=0.,max_adverse_pct=0.)
                    if cfg.no_progress_hours:
                        p.update(progress_peak_net=0.,progress_reached=False)
                    if cfg.failed_breakout_exit:
                        p.update(breakout_level=sig['level'],failed_level_closes=0)
                    if cfg.profit_protection:
                        p.update(peak_hourly_net=0.,protection_updates=0)
                    if cfg.confirmed_profit_protection:
                        p.update(confirmed_peak_hourly_net=0.,protected_floor=0.,
                            confirmed_protection_updates=0,protection_known_at=None)
                    assert cash>=cfg.reserve*initial_cash-1e-8
                    book('entry',t,-p['margin']-p['fees'],quantity=p['quantity'],price=p['average'],
                        side=p['direction'],fee=p['fees'],margin=p['margin'],risk=p['initial_risk'],risk_budget=p['risk_budget'],
                        stop=p['stop'],known_at=sig['known_at'],
                        **(dict(stage='scout',scout_risk_budget=p['scout_risk_budget']) if cfg.staged_entry else {}))
                else:rejected[reason]+=1
        if p:
            exposure+=1;mark_audit(t)
            equity=nav(o);max_eff=max(max_eff,p['quantity']*o/equity if equity>0 else 0)
            d=p['direction'];bad=lo if d==1 else hi;good=hi if d==1 else lo
            liq=liquidation_price(p,spec['mmr'],cfg.fee)
            # A minute spanning both stop and proxy liquidation is pessimistically liquidated.
            if d*(bad-liq)<=0:close(t,liq,'liquidation_proxy_intrabar',True)
            elif d*(bad-p['stop'])<=0:close(t,p['stop'],'structural_stop')
            if p:
                p['max_favorable_pct']=max(p['max_favorable_pct'],d*(good/p['initial_price']-1)*100)
                p['max_adverse_pct']=min(p['max_adverse_pct'],d*(bad/p['initial_price']-1)*100)
        # Drawdown includes pre-existing peak against adverse minute excursion if still held.
        adverse=nav(lo if p and p['direction']==1 else hi) if p else cash
        value=nav(c)
        maxdd=max(maxdd,(peak-min(adverse,value))/peak)
        peak=max(peak,value)
        if not halted and (peak-value)/peak>=cfg.halt_dd:
            halted=True;halt_time=t+MINUTE;pending_halt=t+MINUTE+cfg.latency*MINUTE
            queued_entry=None
        known=t+MINUTE
        if known in frames:
            frame=frames[known]
            if p and not halted:
                no_progress=False
                if cfg.no_progress_hours:
                    # Snapshot at completed-hour observation, before later funding/execution.
                    known_net=stop_total_pnl(p,frame['close'],cfg,spec)
                    p['progress_peak_net']=max(p['progress_peak_net'],known_net)
                    p['progress_reached']=p['progress_reached'] or p['progress_peak_net']>=.5*p['initial_risk']
                    no_progress=frame['ts']-p['entry_ts']>=cfg.no_progress_hours*HOUR and not p['progress_reached'] and known_net<=0
                queued_manage=dict(due=known+cfg.latency*MINUTE,frame=frame,no_progress=no_progress)
                if cfg.confirmed_profit_protection and p['add_count']:
                    # Snapshot before future funding or fills. No minute/high
                    # extrema are consulted, and unconfirmed scouts never arm.
                    known_net=stop_total_pnl(p,frame['close'],cfg,spec)
                    p['confirmed_peak_hourly_net']=max(p['confirmed_peak_hourly_net'],known_net)
                    if p['confirmed_peak_hourly_net']>=3*p['risk_budget']:
                        next_floor=.5*p['confirmed_peak_hourly_net']
                        if next_floor>p['protected_floor']:
                            p['protected_floor']=next_floor;p['protection_known_at']=known
                    queued_manage['protected_floor']=p['protected_floor']
            elif p is None and not halted and known>=next_entry and frame['entry']:
                queued_entry=dict(due=known+cfg.latency*MINUTE,signal=frame['entry'])
            curve.append(dict(ts=known,equity=value,cash=cash,position=0 if p is None else p['direction']*p['quantity']))
    if finalize and p and last:close(end,last[4],'sample_end')
    terminal_nav=nav(last[4]) if p and last else cash
    if last:
        terminal=dict(ts=end if finalize else last[0]+MINUTE,equity=terminal_nav,
            cash=cash,position=0 if p is None else p['direction']*p['quantity'])
        if not finalize and curve and curve[-1]['ts']==terminal['ts']:curve[-1]=terminal
        else:curve.append(terminal)
    maxdd=max(maxdd,(peak-terminal_nav)/peak)
    realized=sum(x['net_pnl'] for x in trades)
    if p is None:assert abs(cash-(cfg.capital+realized))<1e-7
    else:assert abs(cash+p['margin']-(cfg.capital+realized-p['fees']+p['funding']))<1e-7
    assert abs(cash-(cfg.capital+sum(x['cash_change'] for x in ledger)))<1e-7
    wins=[x['net_pnl'] for x in trades if x['net_pnl']>0];losses=[x['net_pnl'] for x in trades if x['net_pnl']<=0]
    streak=longest=0
    for trade in trades:
        streak=streak+1 if trade['net_pnl']<0 else 0;longest=max(longest,streak)
    result=dict(config=asdict(cfg),summary=dict(initial=cfg.capital,final=terminal_nav,net=terminal_nav-cfg.capital,
        return_pct=(terminal_nav/cfg.capital-1)*100,max_drawdown_pct=maxdd*100,trades=len(trades),
        win_rate_pct=len(wins)/len(trades)*100 if trades else 0,
        profit_factor=sum(wins)/-sum(losses) if sum(losses)<0 else None,
        fees=sum(x['total_fees'] for x in trades),funding=sum(x['funding'] for x in trades),
        gross=sum(x['gross_pnl'] for x in trades),adds=sum(x['add_count'] for x in trades),
        average_holding_hours=sum(x['holding_hours'] for x in trades)/len(trades) if trades else 0,
        longest_loss_streak=longest,largest_win=max(wins,default=0),largest_loss=min(losses,default=0),
        largest_wins_3=sum(sorted(wins,reverse=True)[:3]),max_effective_leverage=max_eff,
        exposure_pct=exposure/((end-start)/MINUTE)*100,halted=halted,halt_ts=halt_time,
        proxy_liquidations=sum('liquidation' in x['reason'] for x in trades),
        mark_envelope_flags=len(mark_flags),rejections=dict(rejected)),trades=trades,ledger=ledger,
        curve=curve,mark_envelope_flags=list(mark_flags.values()))
    if not finalize:
        # Preserve the engine's continuous-price liquidation NAV convention;
        # tick-rounded actual fills remain exclusively in executed ledger rows.
        estimated_exit_price=last[4]*(1-p['direction']*cfg.slip) if p and last else None
        estimated_exit_fee=p['quantity']*estimated_exit_price*cfg.fee if p else 0.
        unrealized_gross=p['direction']*p['quantity']*(estimated_exit_price-p['average']) if p else 0.
        open_net=unrealized_gross-p['fees']+p['funding']-estimated_exit_fee if p else 0.
        assert abs(terminal_nav-(cfg.capital+realized+open_net))<1e-7
        result['summary'].update(realized_net=realized,open_position_net=open_net,
            open_position_count=int(p is not None),estimated_exit_fee=estimated_exit_fee,
            unrealized_gross=unrealized_gross,fees=result['summary']['fees']+(p['fees'] if p else 0.),
            funding=result['summary']['funding']+(p['funding'] if p else 0.),
            gross=result['summary']['gross']+unrealized_gross,
            adds=result['summary']['adds']+(p['add_count'] if p else 0.))
        result['state']=dict(known_at=last[0]+MINUTE if last else start,
            last_bar_ts=last[0] if last else None,last_price=last[4] if last else None,
            position=p,queued_entry=queued_entry,queued_manage=queued_manage,cash=cash,
            next_entry=next_entry,peak=peak,halted=halted,halt_ts=halt_time,
            pending_halt=pending_halt,nav=terminal_nav,estimated_exit_price=estimated_exit_price)
    return result
