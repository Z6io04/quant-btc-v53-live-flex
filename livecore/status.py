"""Read-only status plus an actual public request THROUGH the configured proxy."""
from datetime import datetime
import socket
import subprocess
import time

from .config import data_dir, safe_error
from .okx import OKXClient
from .store import read_status


def stamp(value):
    if not value:
        return '尚无记录'
    # Explicit UTC+8 even when called from a Mac with another local timezone.
    from datetime import timezone, timedelta
    return datetime.fromtimestamp(value/1000,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S +08:00')


def service_state(name):
    try:
        r = subprocess.run(['systemctl','--user','is-active',name],capture_output=True,
                            text=True,timeout=3)
        result = r.stdout.strip()
        return result if result in ('active','inactive','failed','activating','deactivating','unknown') else '无法读取'
    except (OSError,subprocess.TimeoutExpired):
        return '当前环境不能读取systemd'


def network_probe(cfg):
    checked = int(time.time()*1000)
    listening = False
    try:
        with socket.create_connection(('127.0.0.1',17890),timeout=1):
            listening = True
    except OSError:
        pass
    result = {'checked_at':checked,'proxy_service':service_state('mihomo-proxy.service'),
              'proxy_listening':listening,'okx_reachable':False,'latency_ms':None,'error':None}
    start = time.monotonic()
    if not listening:
        result['error'] = '本机17890没有接受连接'
        return result
    try:
        api = OKXClient(mode=cfg['mode'],origin=cfg['origin'],proxy=cfg['proxy'],timeout=4)
        server = api.sync_time()
        result.update(okx_reachable=True,server_time=server,latency_ms=round((time.monotonic()-start)*1000))
    except Exception as exc:
        result['error'] = safe_error(exc)
        result['latency_ms'] = round((time.monotonic()-start)*1000)
    return result


def status(cfg,probe=True):
    local = read_status(data_dir(cfg))
    result = {'mode':cfg['mode'],'initial_capital_usdt':cfg['capital_usdt'],
              'max_loss_usdt':cfg['max_loss_usdt'],'service':service_state('btc-v53-live.service'),
              'local':local,'network':network_probe(cfg) if probe else None}
    return result


def display(result):
    print('v53 交易执行版 — '+('欧易模拟盘' if result['mode']=='demo' else '欧易真钱交易；实际成交会改变账户资金'))
    print('交易服务：'+result['service'])
    local,net = result['local'],result['network']
    state,health = local.get('state'),local.get('health',{})
    if not state:
        print('交易尚未启用；没有初始化账户账本。')
    else:
        age = int(time.time()*1000)-health.get('checked_at',0)
        fresh = 0 <= age <= 90000 and health.get('phase') in ('running','paused','closing') and result['service']=='active'
        print('运行记录：'+('近期有更新' if fresh else '尚未就绪、已停止或记录过期；以下可能是旧记录'))
        print('阶段：'+str(health.get('phase','未知'))+'；'+str(health.get('message','')))
        print('最近运行检查：'+stamp(health.get('checked_at')))
        print('行情最后一分钟开始：'+stamp(health.get('last_candle')))
        print('新开仓/补仓：'+('暂停' if state['paused'] else '允许条件检查')+'；风控停止：'+str(state['halted'] or '无'))
        initial = result.get('initial_capital_usdt',local.get('identity',{}).get('config',{}).get('capital_usdt',30))
        added = state.get('capital_added_usdt',0)
        principal = state.get('capital_usdt',initial)
        loss_limit = result.get('max_loss_usdt',local.get('identity',{}).get('config',{}).get('max_loss_usdt',30))
        print('本金可追加，无固定金额上限；逐仓15倍；累计亏损停止阈值：%g U。' % loss_limit)
        print('起始本金：%.4f U；已同步追加：%.4f U；累计投入本金：%.4f U' % (initial,added,principal))
        print('账本现金：%.4f U；最近估算净值：%.4f U' % (state['cash'],state['last_equity']))
        print('最近同步本金：'+stamp(state.get('last_capital_sync_at')))
        print('已观察、尚未同步的转入：%s U；账单最近核对：%s' %
              (state.get('capital_pending_usdt','尚无记录'),stamp(state.get('capital_bill_cursor'))))
        print('充值不会自动增加策略预算；预算和停止阈值不是最大损失保证。')
        print('已实现交易盈亏：%.4f U；成交费用收支：%.4f U；资金费收支：%.4f U' %
              (state['realized_pnl'],state['fees_signed'],state['funding']))
        audit = state.get('fill_audit',{})
        print('逐笔成交核对：%s；最近检查：%s' %
              ('通过（以该时点为准）' if audit.get('ok') else '尚未完成或需要核对',stamp(audit.get('checked_at'))))
        print('最大回撤：%.2f%%；已完成平仓：%d次；尚未核对的请求：%d' %
              (state['max_drawdown']*100,state['closed_trades'],len(local['unresolved'])))
        p = state['position']
        if p:
            print('已登记持仓：%s %.8f BTC；实际成交均价：%.2f' % ('多头仓位' if p['direction']==1 else '空头仓位',p['quantity'],p['average']))
            protect = state.get('protection')
            if protect:
                print('止损登记：%.2f；整仓减仓止损；最近确认：%s' % (protect['stop'],stamp(protect.get('verified_at'))))
                print('止损记录不是实时保证；断网期间需在欧易确认。')
            else:
                print('警示：本地尚未确认整仓止损，请立即核对欧易持仓。')
        else:
            print('已登记持仓：空仓（以最近成功对账时点为准）')
    if net:
        print('代理服务：%s；127.0.0.1:17890接受连接：%s' % (net['proxy_service'],'是' if net['proxy_listening'] else '否'))
        print('经代理访问欧易公开接口：%s；耗时：%s毫秒' % ('成功' if net['okx_reachable'] else '失败',net['latency_ms']))
        print('本次网络检查：'+stamp(net['checked_at']))
        if net.get('error'):
            print('网络提示：'+net['error'])
    print('公开接口能连通，不代表私有下单权限、持仓对账或止损成交已经验证。')
