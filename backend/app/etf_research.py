"""Same-day external observations, conservatively usable from the next session."""
import bisect
from datetime import date, timedelta
import hashlib
import json

import numpy as np
import pandas as pd

from .factors import REGISTRY
from .market_breadth import capture_breadth, import_breadth


def clean(value):
    return float(value) if value is not None and np.isfinite(value) else None


def align_research(frame, spec, context, offline=False):
    frame = frame.copy()
    dates = frame.date.tolist()
    instrument = context['instruments'][spec.code]
    fund = context['funds'][spec.code]
    daily = pd.DataFrame(index=pd.Index(dates, name='date'))
    for name, columns in [('prices', ['close', 'volume', 'amount']), ('nav', ['nav', 'nav_return'])]:
        raw = fund[name]
        for column in columns:
            daily[column] = raw.set_index('date')[column].reindex(dates) if not raw.empty else np.nan
    share_rows = [context['shares'].get((instrument['code'], day), {}) for day in dates]
    daily['shares'] = [row.get('shares', np.nan) for row in share_rows]
    # Large unit-NAV discontinuities not explained by disclosed daily returns can
    # indicate conversion/distribution. Never interpret those windows as flows.
    discontinuity = ((daily.nav / daily.nav.shift() / (1 + daily.nav_return)) - 1).abs() > .10
    for window in (5, 20):
        valid = daily.shares.notna().rolling(window + 1).sum().eq(window + 1)
        valid &= discontinuity.rolling(window, min_periods=1).max().eq(0)
        daily[f'etf_shares_change_{window}'] = ((daily.shares / daily.shares.shift(window) - 1) * 100).where(valid & daily.close.notna())
    daily['etf_turnover'] = daily.volume / daily.shares * 100
    daily['etf_amount_ratio_20'] = daily.amount / daily.amount.shift().rolling(20, min_periods=20).mean().replace(0, np.nan)
    daily['etf_premium_discount'] = (daily.close / daily.nav - 1) * 100
    nav_growth = (1 + daily.nav_return).rolling(20, min_periods=20).apply(np.prod, raw=True)
    daily['etf_nav_index_gap_20'] = (nav_growth - frame.close.to_numpy() / frame.close.shift(20).to_numpy()) * 100
    daily.loc[daily.close.isna(), 'etf_nav_index_gap_20'] = np.nan
    breadth, warnings = capture_breadth(spec, offline)
    breadth_by_day = {row['date']: row for row in breadth}
    breadth_by_day.update({row['date']: row for row in import_breadth(spec)})
    for fid, definition in REGISTRY.items():
        if definition.source_group:
            frame[fid + '_input'] = np.nan
    observations, links, logical = {}, [], []
    latest = {}

    def store(group, day, available, values, records):
        if day not in dates:
            return
        # Reports arriving late never masquerade as the next session's signal.
        target_index = bisect.bisect_right(dates, day)
        eligible = target_index < len(dates) and dates[target_index] >= available
        payload = {'index_code': spec.code, 'instrument_code': instrument['code'] if group == 'etf' else spec.code,
            'group': group, 'observation_date': day, 'available_date': available, 'values': values}
        provenance = {'records': records, 'availability_assumed': group == 'etf'}
        identity = hashlib.sha256(json.dumps({**payload, 'provenance': provenance}, sort_keys=True, allow_nan=False).encode()).hexdigest()
        observations[identity] = {'id': identity, **payload, 'provenance': provenance}
        for fid, definition in REGISTRY.items():
            if definition.source_group != group:
                continue
            latest[fid] = {'latest_observation_date': day, 'earliest_available_date': available}
            if eligible:
                target = dates[target_index]
                frame.loc[target_index, fid + '_input'] = values.get(fid, np.nan)
                links.append({'factor_id': fid, 'trade_date': target, 'observation_id': identity})
                logical.append({'factor_id': fid, 'trade_date': target, **payload})

    for i, (day, row) in enumerate(daily.iterrows()):
        if not any(pd.notna(row[k]) for k in ('close', 'nav', 'shares')):
            continue
        record = share_rows[i].get('record')
        values = {column: clean(value) for column, value in row.items()}
        store('etf', day, (date.fromisoformat(day) + timedelta(days=1)).isoformat(), values,
            fund['records'] + ([record] if record else []))
    for day, row in sorted(breadth_by_day.items()):
        expected = row['expected_members']
        for key in ('expected_members', 'valid_members', 'advancing', 'above_ma20', 'ma20_valid_members'):
            value = row.get(key)
            if value is not None and (not np.isfinite(value) or value < 0 or int(value) != value or value > expected):
                raise ValueError('市场宽度家数必须是有效整数且不超过成分总数')
        if expected < 1:
            raise ValueError('市场宽度成分总数必须为正')
        pos = bisect.bisect_left(dates, day)
        calendar_ok = 'ma20_observation_dates' not in row or row['ma20_observation_dates'] == dates[max(0, pos-19):pos+1] and pos >= 19
        values = {key: row.get(key) for key in ('expected_members', 'valid_members', 'advancing', 'above_ma20', 'ma20_valid_members')}
        values['universe_date'] = row['universe_date']
        values['breadth_advancing_ratio'] = row['advancing'] / expected * 100 if row['valid_members'] == expected and row['advancing'] is not None else None
        values['breadth_above_ma20_ratio'] = row['above_ma20'] / expected * 100 if calendar_ok and row['ma20_valid_members'] == expected and row['above_ma20'] is not None else None
        available = max(row['available_date'], (date.fromisoformat(day) + timedelta(days=1)).isoformat())
        store('breadth', day, available, values, row['records'])
    sources = {}
    for fid, definition in REGISTRY.items():
        if not definition.source_group:
            continue
        is_etf = definition.source_group == 'etf'
        source = {'instrument_code': instrument['code'] if is_etf else spec.code,
            'instrument_name': instrument['name'] if is_etf else spec.short_name + '当期成分股',
            'note': '仅代表所列ETF；IC及相近位置表现检验对应价格指数收益。观测日数据保守按下一交易日可用，不向前填充。' if is_etf else '仅使用可验证的当期成分和完整行情。当前截面开始累积；历史需导入带当时成分与可用日期的数据，缺失区间不回填。',
            **latest.get(fid, {})}
        current_link = next((r for r in reversed(links) if r['factor_id'] == fid and r['trade_date'] == dates[-1]), None)
        if current_link:
            source['input_observation_date'] = observations[current_link['observation_id']]['observation_date']
        sources[fid] = source
    return frame, {'observations': list(observations.values()), 'inputs': links, 'logical': logical, 'sources': sources,
        'warnings': warnings + fund.get('errors', []) + context.get('errors', [])}
