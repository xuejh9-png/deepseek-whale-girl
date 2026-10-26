#!/usr/bin/env python3
"""Claude 费用：两种口径，分开算，绝不混在一起。

1. `snapshot()` —— 中转站导出账单（真金白银）。需要用户导入并核对总额；
   没导入就如实返回 amount=None。
2. `spend_snapshot()` —— Claude Code 本机日志按模型单价折算（本机日志里没有
   cost 字段）。单价表优先读 CC Switch 的 `model-pricing.json`，与它「用量统计」
   面板同口径，可以互相对账；读不到时用内置的 Claude 价目兜底。

账单存在时以账单为准；否则用折算值，并在 note 里写明这是折算、不是账单。
"""
import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import tempfile

from usage_stats import is_claude, iter_claude_calls

LEDGER_PATH = os.path.expanduser('~/.local/share/workbuddy-pet/claude-ledger.json')
SOURCE = '本地导入：中转站导出账单'

# CC Switch 的单价表就在本机；用户看的就是同一个面板，口径必须一致。
CC_PRICING = os.path.expanduser('~/.cc-switch/model-pricing.json')

# 兜底（美元 / 每百万 token）：输入、输出、缓存读、缓存写。
FALLBACK_PRICES = {
    'claude-opus-5-5': (4.0, 20.0, 0.2, 5.0),
    'claude-opus-5': (5.0, 25.0, 0.5, 6.25),
    'claude-sonnet-5-5': (2.0, 10.0, 0.1, 2.5),
    'claude-sonnet-5': (2.0, 10.0, 0.2, 2.5),
    'claude-haiku-5-5': (0.1, 0.5, 0.01, 0.125),
}


def price_table():
    table = {}
    try:
        with open(CC_PRICING, encoding='utf-8') as fh:
            raw = json.load(fh)
        for row in raw.get('models') or []:
            mid = str(row.get('modelId') or '').strip()
            if not mid.startswith('claude-'):
                continue
            try:
                table[mid] = tuple(float(row.get(k) or 0) for k in
                                   ('inputCostPerMillion', 'outputCostPerMillion',
                                    'cacheReadCostPerMillion', 'cacheCreationCostPerMillion'))
            except (TypeError, ValueError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    for mid, price in FALLBACK_PRICES.items():
        table.setdefault(mid, price)
    return table


def price_of(model, table):
    key = str(model or '').strip()
    if key in table:
        return table[key]
    # 日志换了小版本号时（claude-opus-5-7），按家族退回最近的已知价目，
    # 比直接不给金额更有用；tooltip 会写明是折算。
    low = key.lower()
    for family in ('opus', 'sonnet', 'haiku'):
        if family in low:
            for mid in ('claude-%s-5-5' % family, 'claude-%s-5' % family):
                if mid in table:
                    return table[mid]
    return None


def timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp
    except (AttributeError, TypeError, ValueError):
        raise ValueError('账单日期必须包含时区') from None


def money(value):
    if isinstance(value, bool):
        raise ValueError('金额无效')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError('金额必须有限')
        return amount
    except InvalidOperation:
        raise ValueError('金额无效') from None


def normalize(raw):
    if not isinstance(raw, dict) or raw.get('schemaVersion') != 1:
        raise ValueError('需要 schemaVersion=1 的标准化中转站账单')
    start, end, exported = (timestamp(raw.get(k)) for k in ('coverageStart', 'coverageEnd', 'exportedAt'))
    if start > end or end > exported or exported > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise ValueError('账单覆盖时间无效')
    totals = raw.get('billedTotals')
    if not isinstance(totals, dict) or not totals:
        raise ValueError('缺少可核对的账单总额 billedTotals')
    if not all(re.fullmatch(r'[A-Z]{3}', c) for c in totals):
        raise ValueError('币种必须为三个大写字母')
    expected = {c: money(v) for c, v in totals.items()}
    entries, sums = {}, {c: Decimal(0) for c in totals}
    if not isinstance(raw.get('entries'), list):
        raise ValueError('缺少 entries')
    for row in raw['entries']:
        if not isinstance(row, dict):
            raise ValueError('账单行格式错误')
        model = row.get('model')
        # Do not copy arbitrary account labels, URLs or request content to disk.
        if not isinstance(model, str) or not re.fullmatch(r'[\w./:-]{1,120}', model):
            raise ValueError('需要请求的模型标识')
        currency = row.get('currency')
        if currency not in expected:
            raise ValueError('账单行币种不在对账总额中')
        when = timestamp(row.get('timestamp'))
        if not start <= when <= end:
            raise ValueError('账单行超出声明覆盖范围')
        amount = money(row.get('billedAmount'))
        kind = row.get('kind', 'charge')
        if kind not in ('charge', 'adjustment'):
            raise ValueError('账单行 kind 必须为 charge 或 adjustment')
        if kind == 'charge' and amount < 0:
            raise ValueError('负数应作为 adjustment')
        # One charge per request. Separate retry IDs remain separate charges;
        # actual failed-but-charged requests count, zero-charge failures add zero.
        identity = row.get('entryId') or (row.get('requestId') if kind == 'charge' else None)
        if not identity:
            if kind == 'adjustment':
                raise ValueError('调整项必须有独立 entryId')
            identity = json.dumps([when.isoformat(), model, currency, str(amount),
                                   row.get('inputTokens'), row.get('outputTokens'), row.get('status')], sort_keys=True)
        key = hashlib.sha256(str(identity).encode()).hexdigest()
        clean = {'entryId': key, 'timestamp': when.isoformat(), 'model': model,
                 'currency': currency, 'billedAmount': str(amount), 'kind': kind}
        if key in entries and entries[key] != clean:
            raise ValueError('同一账单 ID 有冲突，需导出最终账单或独立调整项')
        entries[key] = clean
    for row in entries.values():
        sums[row['currency']] += money(row['billedAmount'])
    if sums != expected:
        raise ValueError('去重后的账单明细与 billedTotals 不符')
    return {'schemaVersion': 1, 'coverageStart': start.isoformat(), 'coverageEnd': end.isoformat(),
            'exportedAt': exported.isoformat(), 'fullHistory': raw.get('fullHistory') is True,
            'billedTotals': {c: str(v) for c, v in expected.items()}, 'entries': list(entries.values())}


def snapshot(path=None, now=None):
    now = now or dt.datetime.now().astimezone()
    result = {'amount': None, 'currency': None, 'source': SOURCE, 'verified': False,
              'updatedAt': None, 'coverageStart': None, 'coverageEnd': None,
              'note': '未接入真实账单', 'byCurrency': {}, 'today': {}, 'month': {}}
    try:
        with open(path or LEDGER_PATH, encoding='utf-8') as fh:
            raw = normalize(json.load(fh))
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError, KeyError):
        result['note'] = '账单校验失败，未显示金额'
        return result
    result.update(updatedAt=raw['exportedAt'], coverageStart=raw['coverageStart'], coverageEnd=raw['coverageEnd'])
    if not raw['fullHistory']:
        result['note'] = '账单仅覆盖部分历史，不能作为累计消费'
        return result
    sums, today, month = {}, {}, {}
    for row in raw['entries']:
        if not is_claude(row['model']):
            continue
        c, amount = row['currency'], money(row['billedAmount'])
        sums[c] = sums.get(c, Decimal(0)) + amount
        local = timestamp(row['timestamp']).astimezone(now.tzinfo)
        if local.date() == now.date():
            today[c] = today.get(c, Decimal(0)) + amount
        if (local.year, local.month) == (now.year, now.month):
            month[c] = month.get(c, Decimal(0)) + amount
    if not sums and len(raw['billedTotals']) == 1:
        sums[next(iter(raw['billedTotals']))] = Decimal(0)
    result.update(byCurrency={c: str(v) for c, v in sums.items()},
                  today={c: str(v) for c, v in today.items()}, month={c: str(v) for c, v in month.items()})
    if len(sums) != 1:
        result['note'] = '存在多币种账单，未换汇相加；详情按币种列出'
        return result
    currency, amount = next(iter(sums.items()))
    result.update(amount=float(amount), currency=currency, verified=True,
                  note='中转站账面成本；导入明细已与账单总额核对，完整历史依赖导出声明')
    return result


def spend_snapshot(path=None, now=None):
    """Claude Code 的消费：有核对过的账单就用账单，否则按单价折算。

    返回结构与 snapshot() 兼容（amount / currency / verified / today / month），
    前端不用为两种口径写两套渲染。
    """
    bill = snapshot(path, now)
    if bill.get('verified'):
        bill['estimated'] = False
        return bill

    now = now or dt.datetime.now().astimezone()
    table = price_table()
    total = today = month = 0.0
    calls = today_calls = 0
    tokens = today_tokens = 0
    first = last = None
    try:
        rows = list(iter_claude_calls())
    except (OSError, ValueError, TypeError):
        rows = []
    for r in rows:
        price = price_of(r.get('model'), table)
        if price is None:
            continue
        cost = (r['input'] * price[0] + r['completion'] * price[1]
                + r['cacheRead'] * price[2] + r['cacheCreation'] * price[3]) / 1000000.0
        when = r['when']
        total += cost
        calls += 1
        tokens += r['total']
        if when.date() == now.date():
            today += cost
            today_calls += 1
            today_tokens += r['total']
        if (when.year, when.month) == (now.year, now.month):
            month += cost
        if first is None or when < first:
            first = when
        if last is None or when > last:
            last = when

    amount = round(total, 4)
    return {
        'amount': amount, 'currency': 'USD', 'verified': True, 'estimated': True,
        'source': 'Claude Code 本地日志 × 模型单价',
        'updatedAt': now.isoformat(),
        'coverageStart': first.isoformat() if first else None,
        'coverageEnd': last.isoformat() if last else None,
        'note': '按模型单价折算（与 CC Switch 用量面板同口径），不是中转站账单',
        'byCurrency': {'USD': str(amount)},
        'today': {'USD': str(round(today, 4))},
        'month': {'USD': str(round(month, 4))},
        'requests': calls, 'todayRequests': today_calls,
        'tokens': {'total': tokens, 'today': today_tokens},
    }


def import_ledger(source, destination=None):
    with open(source, encoding='utf-8') as fh:
        data = normalize(json.load(fh))
    destination = destination or LEDGER_PATH
    parent = os.path.dirname(os.path.abspath(destination))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=parent, prefix='.ledger-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description='中转站账单本地导入/导出（不联网）')
    parser.add_argument('action', choices=('import', 'export', 'status'))
    parser.add_argument('file', nargs='?')
    args = parser.parse_args()
    try:
        if args.action == 'import':
            if not args.file:
                parser.error('import 需要账单文件路径')
            import_ledger(args.file)
            print('账单已校验并导入本机私有目录。')
        elif args.action == 'export':
            if not args.file:
                parser.error('export 需要输出文件路径')
            # Export is deliberately explicit and uses private file permissions.
            import_ledger(LEDGER_PATH, args.file)
            print('已导出标准化账单，请勿提交到公共仓库。')
        else:
            print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, '账单操作失败：' + (str(exc) if isinstance(exc, ValueError) else '文件不可读或格式无效') + '\n')


if __name__ == '__main__':
    main()
