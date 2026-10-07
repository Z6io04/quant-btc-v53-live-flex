#!/usr/bin/env python3
"""Operator CLI. Credentials are entered only on the user's own machine."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import uuid

from livecore.config import ROOT, Attention, credentials, data_dir, load_config, safe_error
from livecore.okx import OKXClient
from livecore import runtime
from livecore.market import specification, ticker
from livecore.status import display, status
from livecore.store import Store, read_status


def setup_credentials():
    if not sys.stdin.isatty():
        raise Attention('密钥配置需要在你自己的交互式服务器终端运行，不接受聊天或管道输入。')
    path = ROOT/'credentials.env'
    if path.exists() or path.is_symlink():
        raise Attention('credentials.env已存在，不覆盖。更换账户或密钥须先审查旧账本和挂单。')
    print('本包用于真钱交易。输入欧易实盘API，仅读取和交易权限，不要提现权限。')
    print('输入不会显示；请勿发送到聊天、日志或GitHub。保存密钥不会自动下单。')
    vals = []
    for key in ('OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE'):
        value = getpass.getpass(key+'：')
        if not value or len(value)>4096 or any(ord(ch)<33 or ord(ch)>126 for ch in value):
            raise Attention('密钥为空或含不支持的字符；未写入文件。')
        vals.append(key+'='+value)
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write('\n'.join(vals)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    print('已在本机保存 credentials.env（600权限）；尚未联网或启动交易。')


def confirm_live(cfg):
    if not sys.stdin.isatty():
        raise Attention('arm-live 必须在你自己的交互式终端确认，不接受管道确认。')
    print('将启用欧易真钱 v53：BTC-USDT永续，%g USDT起始本金，15倍逐仓。' % cfg['capital_usdt'])
    print('以后可核对到账转入并手动同步本金；累计亏损停止阈值为%g USDT。' % cfg['max_loss_usdt'])
    print('之后启动后台服务会按策略自动交易；预算不是任何情况下的亏损保证。')
    phrase = input('确认后输入 '+runtime.consent_phrase(cfg)+'，再按回车；其他输入取消：').strip()
    if phrase != runtime.consent_phrase(cfg):
        raise Attention('已取消实盘启用，没有提交订单。')
    return phrase


def confirm_small_buy(plan):
    if not sys.stdin.isatty():
        raise Attention('小单测试必须在你自己的交互式终端确认，不接受管道确认。')
    print('真钱小单测试：BTC-USDT永续做多，15倍逐仓，估算保证金上限1U。')
    print(json.dumps(plan,ensure_ascii=False,indent=2))
    print('这是一次性买入测试，不运行v53、不自动止损或平仓；成交后由你在欧易持仓页平仓。')
    return input('确认上述数量和限价后，输入 BUY 1U，再按回车；其他输入取消：').strip() == 'BUY 1U'


def main(argv=None):
    parser = argparse.ArgumentParser(description='v53欧易真钱执行版；填写密钥后仍需手动启用')
    parser.add_argument('command',choices=['setup-credentials','check','account-check','arm-live','run',
                                         'status','pause','resume','flatten','test-buy','test-status','sync-capital'])
    parser.add_argument('--local',action='store_true',help='status只读取本地记录，跳过本次网络检查')
    parser.add_argument('--json',action='store_true',help='status输出结构化内容（不含密钥）')
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == 'sync-capital':
        if not sys.stdin.isatty():
            raise Attention('同步本金需要在你自己的交互式终端确认。')
        from livecore.capital import sync_capital
        print(json.dumps(sync_capital(cfg),ensure_ascii=False,indent=2))
        return 0
    if args.command == 'setup-credentials':
        runtime.assert_live(cfg)
        setup_credentials()
        return 0
    if args.command == 'status':
        report = status(cfg,probe=not args.local)
        if args.json:
            # Local identity includes only a key HASH, not actual credentials.
            print(json.dumps(report,ensure_ascii=False,indent=2))
        else:
            display(report)
        return 0
    if args.command in ('pause','resume','flatten'):
        if not read_status(data_dir(cfg)).get('state'):
            raise Attention('尚无运行账本，不能提交控制指令。')
        with Store(data_dir(cfg)) as store:
            store.append_record('control',uuid.uuid4().hex,{'command':args.command},int(time.time()*1000))
        print('已记录 '+args.command+' 请求，运行中的服务将在下一轮处理。')
        print('请再次查看状态；命令返回不等于已经平仓。服务未运行时请求会等待。')
        return 0
    runtime.assert_live(cfg)
    if args.command in ('arm-live','test-buy') and not sys.stdin.isatty():
        raise Attention('启用真钱或小单测试需要交互式终端。')
    creds = None if args.command == 'check' else credentials()
    api = OKXClient(mode=cfg['mode'],origin=cfg['origin'],proxy=cfg['proxy'],
                    credentials=creds,allow_orders=args.command in ('run','test-buy'))
    if args.command == 'check':
        api.sync_time()
        spec = specification(api)
        quote = ticker(api,cfg)
        print(json.dumps({'passed':True,'mode':cfg['mode'],'proxy':cfg['proxy'],
                          'public_only':True,'orders_sent':0,'spec':spec,'quote_time':quote['ts']},
                         ensure_ascii=False,indent=2))
    elif args.command == 'account-check':
        spec,q,snap = runtime.preflight(api,cfg,require_flat=not read_status(data_dir(cfg)).get('state'))
        print(json.dumps({'passed':True,'mode':'live','account_read_verified':True,'orders_sent':0,
                          'position_present':snap['position'] is not None,
                          'available_covers_initial_capital':snap['available']>=cfg['capital_usdt'],'leverage':15,
                          'account_mode':'Futures / 单币种保证金，net，isolated'},ensure_ascii=False,indent=2))
    elif args.command == 'arm-live':
        phrase = confirm_live(cfg)
        print(json.dumps(runtime.arm_live(api,cfg,creds,confirmation=phrase),ensure_ascii=False,indent=2))
    elif args.command in ('test-buy','test-status'):
        from livecore import smoke
        report = smoke.buy(api,cfg,confirm_small_buy) if args.command == 'test-buy' else smoke.status(api,cfg)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        print('测试请求已受理不等于已成交；以订单状态和欧易持仓为准。未确认结果时只运行test-status核对，不重复买入。')
    elif args.command == 'run':
        runtime.run(api,cfg,creds)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已取消。')
        sys.exit(130)
    except Exception as exc:
        print(safe_error(exc),file=sys.stderr)
        sys.exit(1)
