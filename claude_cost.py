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
# 允许指定自己的价目表：别人机器上没有 CC Switch，也能用自家价格。
PRICING_ENV = 'PET_CLAUDE_PRICING'

# 兜底价目（美元 / 每百万 token）：输入、输出、缓存读、缓存写。
# ⚠️ 最后两个的顺序是「读、写」，和 Anthropic 官网表格的列序不同，照抄会算错一半。
# 5.x 一档对齐 CC Switch 面板（用户看的就是那个数）；
# 4.x 及更早用 Anthropic 公开价，让没装 CC Switch 的机器也能算出金额。
FALLBACK_PRICES = {
    'claude-fable-5-1':  (10.0, 50.0, 0.25, 12.5),
    'claude-fable-5':    (10.0, 50.0, 1.0, 12.5),
    'claude-opus-5-5':   (4.0, 20.0, 0.2, 5.0),
    'claude-opus-5':     (5.0, 25.0, 0.5, 6.25),
    'claude-sonnet-5-5': (2.0, 10.0, 0.1, 2.5),
    'claude-sonnet-5':   (2.0, 10.0, 0.2, 2.5),
    'claude-haiku-5-5':  (0.1, 0.5, 0.01, 0.125),
    'claude-opus-4-8':   (5.0, 25.0, 0.5, 6.25),
    'claude-opus-4-7':   (5.0, 25.0, 0.5, 6.25),
    'claude-opus-4-6':   (5.0, 25.0, 0.5, 6.25),
    'claude-opus-4-5':   (5.0, 25.0, 0.5, 6.25),
    'claude-opus-4-1':   (15.0, 75.0, 1.5, 18.75),
    'claude-opus-4':     (15.0, 75.0, 1.5, 18.75),
    'claude-opus-3':     (15.0, 75.0, 1.5, 18.75),
    'claude-sonnet-4-6': (3.0, 15.0, 0.3, 3.75),
    'claude-sonnet-4-5': (3.0, 15.0, 0.3, 3.75),
    'claude-sonnet-4':   (3.0, 15.0, 0.3, 3.75),
    'claude-sonnet-3-7': (3.0, 15.0, 0.3, 3.75),
    'claude-sonnet-3-5': (3.0, 15.0, 0.3, 3.75),
    'claude-haiku-4-5':  (1.0, 5.0, 0.1, 1.25),
    'claude-haiku-3-5':  (0.8, 4.0, 0.08, 1.0),
    'claude-haiku-3':    (0.25, 1.25, 0.03, 0.3),
}

FAMILIES = ('opus', 'sonnet', 'haiku', 'fable')


def _parse_model(name):
    """从型号名里取出 (家族, 主版本, 次版本)；认不出返回 None。

    真实日志至少有两种写法，都要认：
        claude-sonnet-4-5-20250929
        claude-3-5-sonnet-20241022
    前面还可能带供应商前缀（openrouter/anthropic/claude-…）。
    """
    low = str(name or '').strip().lower()
    m = re.search(r'claude-(%s)-(\d+)(?:[-.](\d+))?' % '|'.join(FAMILIES), low)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3) or 0)
    m = re.search(r'claude-(\d+)(?:[-.](\d+))?-(%s)' % '|'.join(FAMILIES), low)
    if m:
        return m.group(3), int(m.group(1)), int(m.group(2) or 0)
    return None


def _pricing_path():
    override = os.environ.get(PRICING_ENV)
    if override:
        return os.path.expanduser(override)
    return CC_PRICING


def price_table():
    table = {}
    try:
        with open(_pricing_path(), encoding='utf-8') as fh:
            raw = json.load(fh)
        for row in raw.get('models') or []:
            mid = str(row.get('modelId') or '').strip().lower()
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
    # 外部表优先：它代表用户实际在用的价格口径，内置表只补缺口。
    for mid, price in FALLBACK_PRICES.items():
        table.setdefault(mid, price)
    return table


def price_of(model, table):
    key = str(model or '').strip().lower()
    if not key:
        return None
    if key in table:
        return table[key]
    # 带日期后缀的完整名：表里可能存的是同一个键，只比前缀
    for mid, price in table.items():
        if key.startswith(mid):
            return price
    parsed = _parse_model(key)
    if not parsed:
        return None
    family, major, minor = parsed
    for candidate in ('claude-%s-%d-%d' % (family, major, minor),
                      'claude-%s-%d' % (family, major)):
        if candidate in table:
            return table[candidate]
    # 同家族同主版本的任意型号 —— 同代价格通常一致
    same_major = [(mid, price) for mid, price in table.items()
                  if (lambda p: p and p[0] == family and p[1] == major)(_parse_model(mid))]
    if same_major:
        return sorted(same_major)[0][1]
    # 同家族最接近的主版本：小版本号对不上时，比直接不给金额更有用
    family_rows = [(p[1], mid, price) for mid, price in table.items()
                   for p in [_parse_model(mid)] if p and p[0] == family]
    if family_rows:
        return min(family_rows, key=lambda row: (abs(row[0] - major), -row[0]))[2]
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
    unpriced = 0
    first = last = None
    try:
        rows = list(iter_claude_calls())
    except (OSError, ValueError, TypeError):
        rows = []
    for r in rows:
        when = r['when']
        is_today = when.date() == now.date()
        # ⚠️ token 与调用次数**不依赖价格表**。
        # 以前 price is None 就整条 continue，模型一旦没进价目表，
        # 它的 token 和调用次数也一起消失 —— 卡片上的「今日 Token」会莫名少一块，
        # 而用户完全看不出是价目表缺条目。金额可以缺，计数不能缺。
        tokens += r['total']
        calls += 1
        if is_today:
            today_tokens += r['total']
            today_calls += 1
        if first is None or when < first:
            first = when
        if last is None or when > last:
            last = when

        price = price_of(r.get('model'), table)
        if price is None:
            unpriced += 1
            continue
        cost = (r['input'] * price[0] + r['completion'] * price[1]
                + r['cacheRead'] * price[2] + r['cacheCreation'] * price[3]) / 1000000.0
        total += cost
        if is_today:
            today += cost
        if (when.year, when.month) == (now.year, now.month):
            month += cost

    amount = round(total, 4)
    # 价目来源要写清楚：别人机器上通常没有 CC Switch，
    # 用的是内置公开价，数字会和他自己的面板对不上 —— 先说清楚，别让人怀疑算错。
    if os.path.exists(_pricing_path()):
        note = '按模型单价折算（与 CC Switch 用量面板同口径），不是中转站账单'
    else:
        note = '按模型单价折算（价目来源：内置公开价目，可设 %s 指定自己的表），不是中转站账单' % PRICING_ENV
    return {
        'amount': amount, 'currency': 'USD', 'verified': True, 'estimated': True,
        'source': 'Claude Code 本地日志 × 模型单价',
        'updatedAt': now.isoformat(),
        'coverageStart': first.isoformat() if first else None,
        'coverageEnd': last.isoformat() if last else None,
        'note': note,
        'byCurrency': {'USD': str(amount)},
        'today': {'USD': str(round(today, 4))},
        'month': {'USD': str(round(month, 4))},
        'requests': calls, 'todayRequests': today_calls,
        # 有多少次调用因为模型不在价目表里而没算进金额（token 仍然算进去了）
        'unpricedRequests': unpriced,
        'tokens': {'total': tokens, 'today': today_tokens},
    }


def import_ledger(source, destination=None):
    with open(source, encoding='utf-8') as fh:
        data = normalize(json.load(fh))
    destination = destination or LEDGER_PATH
    parent = os.path.dirname(os.path.abspath(destination))
    # Windows 会忽略 mode（没有 POSIX 权限位），只有 POSIX 上才真的收紧到 0700
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=parent, prefix='.ledger-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def detail(now=None):
    """逐笔列出每次调用的四个分量与折算金额 —— 质疑"数字不准"时先看这个。

    只读本机日志，不联网、不读账单；每行对应 Claude Code 日志里一条 message.id
    （同一条 message 的多次流式写入已按最大值去重）。
    """
    now = now or dt.datetime.now().astimezone()
    table = price_table()
    rows = sorted(iter_claude_calls(), key=lambda r: r['when'])
    print(f"{'时间':16s}  {'模型':26s} {'输入':>8s} {'输出':>7s} {'缓存写':>8s} {'缓存读':>9s} {'合计':>9s}  金额")
    total_cost = 0.0
    unpriced = 0
    for r in rows:
        price = price_of(r['model'], table)
        if price is None:
            cost_text = '—（价目表没有这个型号）'
            unpriced += 1
        else:
            cost = (r['input'] * price[0] + r['completion'] * price[1]
                    + r['cacheRead'] * price[2] + r['cacheCreation'] * price[3]) / 1000000.0
            total_cost += cost
            cost_text = '$%.4f' % cost
        if r['when'].date() == now.date():
            cost_text += '  ← 今天'
        print(f"{r['when']:%Y-%m-%d %H:%M}  {r['model'][:26]:26s} "
              f"{r['input']:>8,} {r['completion']:>7,} {r['cacheCreation']:>8,} {r['cacheRead']:>9,} "
              f"{r['total']:>9,}  {cost_text}")
    print()
    print(f"合计 {len(rows)} 次调用 · {sum(r['total'] for r in rows):,} token · ${total_cost:.4f}")
    if unpriced:
        print(f"其中 {unpriced} 次因为型号不在价目表里没有金额（token 仍然计入合计）")
    print(f"价目来源：{'CC Switch（%s）' % _pricing_path() if os.path.exists(_pricing_path()) else '内置公开价目'}")
    print("口径：输入 / 输出 / 缓存写 / 缓存读 四条分量分别计价；"
          "缓存读单价约为输入的 1/10，所以上亿 token 也可能只有几毛钱。")


def main():
    parser = argparse.ArgumentParser(description='中转站账单本地导入/导出（不联网）')
    parser.add_argument('action', choices=('import', 'export', 'status', 'detail'))
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
        elif args.action == 'detail':
            detail()
        else:
            # status 给"实际会显示的那个数"：有核对过的账单就是账单，
            # 没有就按本机日志 × 单价折算。只打 snapshot() 的话，
            # 没导入账单的人会看到一片 null，以为查不到花费。
            print(json.dumps(spend_snapshot(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, '账单操作失败：' + (str(exc) if isinstance(exc, ValueError) else '文件不可读或格式无效') + '\n')


if __name__ == '__main__':
    main()
