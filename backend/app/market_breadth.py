"""Observe current constituent breadth; never backcast today's membership.

Historical verified aggregates can be imported with explicit membership and
availability dates. Current official lists are used only on their stated date.
"""
from datetime import date, datetime, timedelta
from io import BytesIO
import hashlib
import json
import re

import numpy as np
import pandas as pd
import httpx

from .config import DATA_DIR, ROOT, SHANGHAI
from .etf_sources import read_cache, save_cache
from .job_control import check_cancelled, report_progress
from .providers import ProviderError, _http_client, archive, fetch


def constituents(spec):
    if spec.code == "399673.SZ":
        response = _http_client.get("https://www.cnindex.com.cn/sample-detail/download-history", params={"indexcode": spec.code[:6]})
    else:
        response = _http_client.get(f"https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{spec.code[:6]}cons.xls")
    response.raise_for_status()
    data = response.content
    frame = pd.read_excel(BytesIO(data), dtype=str)
    if spec.code == "399673.SZ":
        dates = pd.to_datetime(frame.iloc[:, 0]).dt.strftime("%Y-%m-%d")
        codes = frame["样本代码"].str.zfill(6)
        symbols = ["sz" + code for code in codes]
    else:
        dates = pd.to_datetime(frame["日期Date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        codes = frame["成份券代码Constituent Code"].str.zfill(6)
        symbols = [("sh" if "上海" in exchange else "sz" if "深圳" in exchange else "bj") + code
            for code, exchange in zip(codes, frame["交易所Exchange"])]
    if dates.nunique() != 1 or len(set(symbols)) != len(symbols) or len(symbols) != {"000300.SH": 300, "000905.SH": 500, "000016.SH": 50, "399673.SZ": 50, "000688.SH": 50}[spec.code]:
        raise ProviderError("成分股文件日期、去重或数量核查失败")
    day = dates.iloc[0]
    digest = hashlib.sha256(data).hexdigest()
    path = DATA_DIR / "raw" / spec.code / f"constituents-{day}-{digest[:16]}.xls"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return day, symbols, {"source": "cnindex_constituents" if spec.code == "399673.SZ" else "csindex_constituents",
        "sha256": digest, "path": str(path.relative_to(ROOT)), "membership_date": day}


def parse_quotes(text, day):
    result = {}
    for symbol, literal in re.findall(r'v_([a-z]{2}\d{6})="([^"\n]*)"', text):
        fields = literal.split("~")
        if len(fields) <= 30 or len(fields[30]) < 14:
            continue
        stamp = fields[30]
        quote_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        # A quote from another day or before the close is not an EOD observation.
        if quote_date != day or stamp[8:12] < "1500":
            continue
        close, previous = float(fields[3]), float(fields[4])
        if close > 0 and previous > 0:
            result[symbol] = {"close": close, "previous": previous}
    return result


def capture_breadth(spec, offline=False):
    directory = DATA_DIR / "breadth-cache" / spec.code
    warnings = []
    if not offline:
        try:
            report_progress("collect_breadth", f"{spec.short_name}：核对当期成分与收盘行情", index=spec.code)
            day, symbols, membership = constituents(spec)
            now = datetime.now(SHANGHAI)
            completed = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
            if day != completed.isoformat():
                raise ProviderError("成分文件并非当前已收盘交易日，不向过去回填当前截面")
            quotes, refs = {}, [membership]
            for start in range(0, len(symbols), 50):
                check_cancelled()
                text = fetch("https://qt.gtimg.cn/q=" + ",".join(symbols[start:start + 50]))
                quotes.update(parse_quotes(text, day))
                refs.append(archive(spec.code, f"breadth_quotes_{day}_{start}", text))
            # Retain observations, including incomplete snapshots, without filling gaps.
            snapshot = {"date": day, "universe_date": day, "symbols": symbols, "quotes": quotes, "records": refs}
            save_cache(directory / f"{day}.json", snapshot)
        except (ProviderError, ValueError, OSError, ImportError, KeyError, httpx.HTTPError) as exc:
            # Cancellation inherits BaseException and must still escape this handler.
            warnings.append(f"当期宽度采集未完成：{type(exc).__name__}")
    snapshots = [read_cache(path) for path in sorted(directory.glob("????-??-??.json"))]
    snapshots = [row for row in snapshots if row]
    history, rows = {}, []
    for snap in snapshots:
        symbols, quotes = snap["symbols"], snap["quotes"]
        complete = len(quotes) == len(symbols) and all(symbol in quotes for symbol in symbols)
        for symbol, quote in quotes.items():
            history.setdefault(symbol, []).append((snap["date"], quote["close"], quote["previous"]))
        above, ma_valid = 0, 0
        # A rolling mean is supported only by 20 consecutive recorded sessions for
        # every current constituent. No old membership is inferred from this list.
        recent_dates = [r["date"] for r in snapshots if r["date"] <= snap["date"]][-20:]
        for symbol in symbols:
            prices = history.get(symbol, [])[-20:]
            if len(recent_dates) == 20 and [r[0] for r in prices] == recent_dates:
                ma_valid += 1
                # Chain each day's close/previous-close reference to place all
                # 20 observations on a comparable basis across ex-rights dates.
                comparable = np.cumprod([1.] + [r[1] / r[2] for r in prices[1:]])
                above += comparable[-1] > float(np.mean(comparable))
        rows.append({"date": snap["date"], "available_date": (date.fromisoformat(snap["date"]) + timedelta(days=1)).isoformat(),
            "universe_date": snap["universe_date"], "expected_members": len(symbols), "valid_members": len(quotes),
            "advancing": sum(q["close"] > q["previous"] for q in quotes.values()) if complete else None,
            "above_ma20": int(above) if ma_valid == len(symbols) else None, "ma20_valid_members": ma_valid,
            "ma20_observation_dates": recent_dates,
            "source": "official_membership+tencent_eod", "records": snap["records"]})
    return rows, warnings


def import_breadth(spec):
    path = DATA_DIR / "imports" / f"{spec.code}-breadth.csv"
    if not path.exists():
        return []
    frame = pd.read_csv(path, dtype={"date": str, "available_date": str, "universe_date": str})
    required = {"date", "available_date", "universe_date", "expected_members", "valid_members", "advancing", "above_ma20", "ma20_valid_members", "source"}
    if not required.issubset(frame):
        raise ValueError(f"{path.name} 必须包含 {sorted(required)}")
    if frame.date.duplicated().any():
        raise ValueError("市场宽度导入日期重复")
    rows = []
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for row in frame.to_dict("records"):
        for field in ("date", "available_date", "universe_date"):
            if pd.isna(row[field]):
                raise ValueError("市场宽度的观测、可用及成分日期不可缺失")
            row[field] = pd.Timestamp(row[field]).strftime("%Y-%m-%d")
        if row["available_date"] < row["date"] or row["universe_date"] > row["date"] or pd.isna(row["source"]):
            raise ValueError("市场宽度存在未来成分、提前可用或缺少来源")
        row["records"] = [{"source": str(row["source"]), "sha256": digest, "path": str(path.relative_to(ROOT))}]
        for field in ("expected_members", "valid_members", "advancing", "above_ma20", "ma20_valid_members"):
            row[field] = float(row[field]) if pd.notna(row[field]) else None
        rows.append(row)
    return rows
