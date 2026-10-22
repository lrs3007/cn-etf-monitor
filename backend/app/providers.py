"""Full-history providers. Raw responses are retained for audit and offline replay."""
import hashlib
import atexit
import json
import os
import re
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .config import DATA_DIR, ROOT, SHANGHAI, IndexSpec, now_iso
from .provider_cache import cached_history
from .job_control import check_cancelled
from urllib.parse import urlsplit

_host_failures = {}
_http_client = httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=True,
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=60))
atexit.register(_http_client.close)


class ProviderError(RuntimeError):
    pass


def fetch(url, params=None, *, method="GET", body=None, headers=None):
    """Bounded retries; do not disable proxies or TLS verification."""
    error = None
    host = urlsplit(url).netloc
    if time.monotonic() - _host_failures.get(host, -1e9) < 600:
        raise ProviderError("同一数据源近期连接失败，短暂冷却，稍后重试")
    for attempt in range(2):
        check_cancelled()
        try:
            response = _http_client.request(method, url, params=params, json=body,
                headers={"User-Agent": "Mozilla/5.0 cn-etf-monitor/0.1", **(headers or {})})
            response.raise_for_status()
            if not response.content:
                raise ProviderError("数据源返回空响应")
            return response.text
        except (httpx.HTTPError, ProviderError) as exc:
            error = exc
            if attempt < 1:
                time.sleep(1 + attempt)
    # Some public market hosts close httpx connections; curl offers a second HTTP transport.
    # Proxy and TLS policy are preserved. Credentials, when present, go over stdin only.
    check_cancelled()
    command = ["curl", "--max-time", "12", "--fail", "--silent", "--show-error", "--location"]
    for key, value in (headers or {}).items():
        command.extend(["--header", f"{key}: {value}"])
    if params:
        command.append("--get")
        for key, value in params.items():
            command.extend(["--data-urlencode", f"{key}={value}"])
    if method == "POST":
        command.extend(["--request", "POST", "--header", "Content-Type: application/json", "--data-binary", "@-"])
    command.append(url)
    try:
        response = subprocess.run(command, input=json.dumps(body) if body is not None else None,
            text=True, capture_output=True, timeout=25, check=True)
        return response.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _host_failures[host] = time.monotonic()
        raise ProviderError(f"HTTP请求失败：{error}; curl传输亦失败") from exc


def archive(code, source, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    directory = DATA_DIR / "raw" / code
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source}-{digest[:16]}.txt"
    if not path.exists():
        path.write_text(text)
    return {"source": source, "sha256": digest, "path": str(path.relative_to(ROOT)), "fetched_at": now_iso()}


def sina_history(spec: IndexSpec, offline=False):
    if offline:
        files = sorted((DATA_DIR / "raw" / spec.code).glob("sina-*.txt"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ProviderError("没有可离线重放的新浪原始数据")
        text = files[-1].read_text()
    else:
        text = fetch(f"https://finance.sina.com.cn/realstock/company/{spec.symbol}/hisdata/klc_kl.js")
    record = archive(spec.code, "sina", text)
    match = re.search(r'=\s*"([^\"]+)"', text)
    if not match:
        raise ProviderError("新浪历史数据格式变化，未找到编码字符串")
    decoded = subprocess.run(["node", str(ROOT / "scripts/decode-sina.cjs")],
        input=json.dumps(match.group(1)), capture_output=True, text=True, timeout=30, check=True)
    frame = pd.DataFrame(json.loads(decoded.stdout))
    if frame.empty:
        raise ProviderError("新浪没有返回该指数历史")
    frame["date"] = frame["date"].str[:10]
    frame["source"] = "sina"
    frame["amount"] = np.nan
    return frame, record


@cached_history("eastmoney")
def eastmoney_history(spec: IndexSpec, offline=False, start_date=None):
    if offline:
        files = sorted((DATA_DIR / "raw" / spec.code).glob("eastmoney-*.txt"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ProviderError("没有可离线重放的东方财富原始数据")
        text = files[-1].read_text()
    else:
        text = fetch("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
            "secid": spec.secid, "fields1": "f1,f2,f3,f4,f5",
            "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": "101", "fqt": "0",
            "beg": start_date.replace("-", "") if start_date else "19900101", "end": "20500101", "lmt": "100000",
        })
    record = archive(spec.code, "tail_eastmoney" if start_date else "eastmoney", text)
    record["source"] = "eastmoney"
    data = json.loads(text).get("data")
    if not data or not data.get("klines"):
        raise ProviderError("东方财富没有返回该指数历史")
    frame = pd.DataFrame([line.split(",")[:7] for line in data["klines"]],
        columns=["date", "open", "close", "high", "low", "volume", "amount"])
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["volume"] *= 100  # hands -> shares; Sina already uses shares
    frame["source"] = "eastmoney"
    return frame, record


def load_valuations(spec, offline=False):
    """CSV available_date is mandatory: no fabricated historical availability."""
    path = DATA_DIR / "imports" / f"{spec.code}-valuations.csv"
    records, notes = [], []
    if path.exists():
        frame = pd.read_csv(path, dtype={"date": str, "available_date": str})
        required = {"date", "available_date", "pe_ttm", "pb"}
        if not required.issubset(frame.columns):
            raise ProviderError(f"{path.name} 必须包含 {sorted(required)}")
        for column in ("date", "available_date"):
            parsed = pd.to_datetime(frame[column], format="%Y-%m-%d", errors="raise")
            if parsed.isna().any():
                raise ProviderError(f"{path.name}: {column} 不可为空")
            frame[column] = parsed.dt.strftime("%Y-%m-%d")
        if (frame.available_date < frame.date).any():
            raise ProviderError(f"{path.name}: 可用日期早于观测日期，会引入前视偏差")
        for column in ("pe_ttm", "pb"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if np.isinf(frame[column].to_numpy(dtype=float)).any():
                raise ProviderError(f"{path.name}: {column} 不可为无穷值")
        records = frame.to_dict("records")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for row in records:
            row.update(source="csv", raw_sha256=digest, availability_assumed=False)
        notes.append("估值由本地CSV导入，按available_date对齐")
    token = os.getenv("TUSHARE_TOKEN")
    if token and not offline:
        # Request yearly chunks to avoid silently hitting a provider row limit.
        for year in range(int(spec.base_date[:4]), date.today().year + 1):
            try:
                cached = sorted((DATA_DIR / "raw" / spec.code).glob(f"tushare_{year}-*.txt"), key=lambda p: p.stat().st_mtime)
                from .provider_cache import _force
                checked = DATA_DIR / "provider-cache" / f"{spec.code}-tushare-{year}.checked"
                reusable = cached and year < date.today().year and not _force.get() and checked.exists() and time.time() - checked.stat().st_mtime < 7 * 86400
                text = cached[-1].read_text() if reusable else fetch("https://api.tushare.pro", method="POST", body={
                    "api_name": "index_dailybasic", "token": token,
                    "params": {"ts_code": spec.code, "start_date": f"{year}0101", "end_date": f"{year}1231"},
                    "fields": "ts_code,trade_date,pe_ttm,pb",
                })
                payload = json.loads(text)
                if payload.get("code") != 0:
                    raise ProviderError(payload.get("msg", "Tushare请求失败"))
                if not reusable:
                    archive(spec.code, f"tushare_{year}", text)
                    checked.parent.mkdir(parents=True, exist_ok=True)
                    checked.write_text(now_iso())
                data = payload.get("data") or {}
                for row in data.get("items", []):
                    item = dict(zip(data["fields"], row))
                    dt = pd.Timestamp(item["trade_date"])
                    records.append({"date": dt.strftime("%Y-%m-%d"),
                        # dailybasic does not publish PIT timestamps; conservative next-calendar-day eligibility
                        "available_date": (dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                        "pe_ttm": item.get("pe_ttm"), "pb": item.get("pb"),
                        "source": "tushare", "raw_sha256": hashlib.sha256(text.encode()).hexdigest()})
            except ProviderError as exc:
                notes.append(f"估值接口未完成：{exc}")
                break
        if records:
            notes.append("Tushare估值披露时间未提供：采用次日可用假设，非严格点时数据库")
    return records, notes


@cached_history("cnindex")
def cnindex_history(spec, offline=False, start_date=None):
    """Official ChiNext 50 backcast, including the real close-only base observation."""
    if offline:
        files = sorted((DATA_DIR / "raw" / spec.code).glob("cnindex-*.txt"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ProviderError("没有可离线重放的国证指数原始数据")
        text = files[-1].read_text()
    else:
        text = fetch("https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat", {
            "indexCode": spec.code[:6], "startDate": start_date or spec.base_date,
            "endDate": date.today().isoformat(), "frequency": "day"})
    record = archive(spec.code, "tail_cnindex" if start_date else "cnindex", text)
    record["source"] = "cnindex"
    data = json.loads(text).get("data") or {}
    rows = data.get("data") or []
    if not rows:
        raise ProviderError("国证指数没有返回历史数据")
    frame = pd.DataFrame(rows, columns=["date", "current", "high", "open", "low", "close", "change", "pct", "amount", "volume", "avg"])
    for column in ("open", "high", "low", "close", "amount", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # Official quotation uses hundred-million CNY and million shares.
    frame["amount"] *= 100_000_000
    frame["volume"] *= 1_000_000
    frame["source"] = "cnindex"
    return frame[["date", "open", "high", "low", "close", "amount", "volume", "source"]], record


@cached_history("csindex")
def csindex_history(spec, offline=False, start_date=None):
    """CSI official observations. Caller must align to independently obtained sessions."""
    if offline:
        files = sorted((DATA_DIR / "raw" / spec.code).glob("csindex-*.txt"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ProviderError("没有可离线重放的中证指数原始数据")
        text = files[-1].read_text()
    else:
        text = fetch("https://www.csindex.com.cn/csindex-home/perf/index-perf", {
            "indexCode": spec.code[:6], "startDate": start_date.replace("-", "") if start_date else "19900101", "endDate": date.today().strftime("%Y%m%d")})
    record = archive(spec.code, "tail_csindex" if start_date else "csindex", text)
    record["source"] = "csindex"
    payload = json.loads(text)
    rows = payload.get("data") or []
    if not rows:
        raise ProviderError("中证指数没有返回历史数据")
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame.tradeDate, format="%Y%m%d").dt.strftime("%Y-%m-%d")
    frame = frame.rename(columns={"tradingVol": "volume", "tradingValue": "amount", "peg": "pe_observed"})
    for column in ("open", "high", "low", "close", "volume", "amount", "pe_observed"):
        frame[column] = pd.to_numeric(frame.get(column, pd.Series(np.nan, index=frame.index)), errors="coerce")
    frame["amount"] *= 100_000_000  # CSI official API: hundred-million CNY.
    frame["source"] = "csindex"
    return frame[["date", "open", "high", "low", "close", "volume", "amount", "pe_observed", "source"]].drop_duplicates("date", keep="last"), record


def full_history(spec, offline=False, bond_context=None):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    frames, records, errors = {}, [], []
    providers = [("sina", sina_history), ("eastmoney", eastmoney_history),
        ("cnindex", cnindex_history) if spec.code == "399673.SZ" else ("csindex", csindex_history)]

    def collect(name, provider):
        try:
            source_frame, record = provider(spec, offline=offline)
            return source_frame, record, None
        except (ProviderError, ValueError, subprocess.SubprocessError) as exc:
            error = f"{name}: {exc}"
            if not offline:
                try:
                    cached, record = provider(spec, offline=True)
                    return cached, {**record, "replayed_cache": True}, error
                except (ProviderError, ValueError, subprocess.SubprocessError):
                    pass
            return None, None, error

    # Independent sources overlap network waits; SQLite publication remains serial.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="market-source") as executor:
        pending = [(name, executor.submit(copy_context().run, collect, name, provider)) for name, provider in providers]
        collected = {name: future.result() for name, future in pending}
    for name, (source_frame, record, error) in collected.items():
        if error:
            errors.append(error)
        if record:
            records.append(record)
        if source_frame is not None and name != "csindex":
            frames[name] = source_frame.set_index("date")
    if not frames:
        raise ProviderError("; ".join(errors))
    # Sina retains the longest backcast and three-decimal prices. Supplement amount from Eastmoney.
    if "sina" in frames and "eastmoney" in frames:
        a, b = frames["sina"], frames["eastmoney"]
        overlap = a.index.intersection(b.index)
        discrepancy = ((a.loc[overlap, "close"].astype(float) / b.loc[overlap, "close"] - 1).abs() > 0.002)
        if discrepancy.any():
            raise ProviderError(f"双源收盘价存在 {int(discrepancy.sum())} 个超过0.2%的差异，暂停发布")
        frame = a.combine_first(b)
        frame.loc[frame["amount"].notna() & frame.index.isin(a.index), "source"] = "sina+eastmoney"
    else:
        frame = next(iter(frames.values())).copy()
    if "cnindex" in frames:
        official = frames["cnindex"]
        overlap = frame.index.intersection(official.index)
        differences = (frame.loc[overlap, "close"].astype(float) / official.loc[overlap, "close"] - 1).abs()
        if (differences > .002).any():
            raise ProviderError("国证指数与历史价格源差异超过0.2%，暂停发布")
        # Do not mix an unexplained volume unit into the history.
        volume_ratio = (frame.loc[overlap, "volume"] / official.loc[overlap, "volume"]).replace([np.inf, -np.inf], np.nan).dropna()
        if len(volume_ratio) and abs(volume_ratio.median() - 1) > .01:
            raise ProviderError("国证指数成交量单位未通过交叉核对")
        frame = frame.combine_first(official)
        frame.loc[frame.index.isin(overlap), "source"] = frame.loc[frame.index.isin(overlap), "source"] + "+cnindex"
    if spec.code != "399673.SZ":
        official = collected["csindex"][0]
        if official is not None:
            official = official.set_index("date")
            # CSI sometimes repeats a nearby observation at a non-trading request boundary.
            # Only use dates already confirmed by the primary full-history source.
            overlap = frame.index.intersection(official.index)
            discarded = sorted(set(official.index) - set(frame.index))
            difference = (frame.loc[overlap, "close"].astype(float) / official.loc[overlap, "close"] - 1).abs()
            if (difference > .002).any():
                raise ProviderError("中证官方与已有收盘价差异超过0.2%，暂停发布")
            official = official.loc[overlap]
            frames["csindex"] = official
            frame = frame.combine_first(official)
            frame.loc[overlap, "source"] = frame.loc[overlap, "source"] + "+csindex"
            if discarded:
                errors.append(f"csindex: 剔除{len(discarded)}条未在主历史源确认的边界日期")
    if not offline and records and all(record.get("replayed_cache") for record in records):
        raise ProviderError("全部在线数据源失败，仅有历史缓存；保留已有快照并记录任务失败")
    frame = frame.reset_index().rename(columns={"index": "date"}).sort_values("date")
    frame = frame.drop_duplicates("date", keep="last").reset_index(drop=True)
    # A current-session partial bar must never be treated as a completed daily observation.
    from datetime import datetime
    local_now = datetime.now(SHANGHAI)
    completed_date = local_now.date() if local_now.hour >= 16 else (local_now - pd.Timedelta(days=1)).date()
    frame = frame[frame.date <= completed_date.isoformat()].reset_index(drop=True)
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    bad = (frame.close.isna() | (frame.close <= 0)
        | (frame[["open", "high", "low"]] <= 0).any(axis=1)
        | (frame.high + 0.02 < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low - 0.02 > frame[["open", "close", "high"]].min(axis=1)))
    if bad.any():
        raise ProviderError(f"{int(bad.sum())} 条OHLC不合法，暂停发布")
    if (frame[["volume", "amount"]] < 0).any().any():
        raise ProviderError("成交量/额存在负值")
    frame.loc[frame.volume <= 0, "volume"] = np.nan
    frame.loc[frame.amount <= 0, "amount"] = np.nan
    frame["pe_ttm"], frame["pb"] = np.nan, np.nan
    valuations, valuation_notes = load_valuations(spec, offline=offline)
    if "pe_observed" in frame:
        valid_pe = frame.loc[frame.pe_observed.notna(), ["date", "pe_observed"]]
        for _, row in valid_pe.iterrows():
            valuations.insert(0, {"date": row["date"],
                "available_date": (date.fromisoformat(str(row["date"])) + timedelta(days=1)).isoformat(),
                "pe_ttm": row["pe_observed"], "pb": np.nan, "source": "csindex",
                "raw_record": frame.loc[frame.date == row["date"], "pe_record"].iloc[0] if "pe_record" in frame else None})
        if len(valid_pe):
            valuation_notes.append("中证官方滚动市盈率仅覆盖实际提供区间；采用次日可用假设，非严格点时数据，不补齐早期缺失")
    if valuations:
        # Map an observation to the first index session at/after its stated availability.
        vf = pd.DataFrame(valuations)
        vf["_priority"] = np.arange(len(vf))
        vf = vf.sort_values(["available_date", "date", "_priority"]).groupby("available_date", as_index=False).last()
        for _, row in vf.iterrows():
            eligible = frame.index[frame.date >= row["available_date"]]
            if len(eligible):
                for col in ("pe_ttm", "pb"):
                    frame.loc[eligible[0], col] = pd.to_numeric(row[col], errors="coerce")
        # Intentionally do not forward-fill missing daily observations.
    from .equity_bond import align_spread_inputs
    bond_context = bond_context or {"observations": [], "source_records": [], "errors": []}
    frame, spread_inputs = align_spread_inputs(frame, valuations, bond_context, spec.code, records)
    frame.attrs["spread_inputs"] = spread_inputs
    frame["history_type"] = np.where(frame.date < spec.launch_date, "backcast", "live")
    frame["availability"] = frame.date + "T16:00:00+08:00"
    coverage = {
        "first_date": frame.date.iloc[0], "last_date": frame.date.iloc[-1], "rows": len(frame),
        "base_date": spec.base_date, "launch_date": spec.launch_date,
        "backcast_rows": int((frame.history_type == "backcast").sum()),
        "source_records": records, "provider_errors": errors, "notes": valuation_notes,
        "scope": "数据源返回的全部历史；发布前回溯单独标记，不宣称独立验证了所有交易日",
        "cross_source_checked": len(frames) >= 2,
        "cross_source_through": min(f.index.max() for f in frames.values()) if len(frames) >= 2 else None,
        "volume_rows": int(frame.volume.notna().sum()), "amount_rows": int(frame.amount.notna().sum()),
        "valuation_rows": int(frame.pe_ttm.notna().sum()),
        "calendar_verified": False,
        "bond_source_records": bond_context["source_records"], "bond_provider_errors": bond_context["errors"],
        "bond_series": "中债国债收益率曲线 · 10年期到期收益率；按同日观测配对，下一交易日信号",
        "bond_history_coverage": {"rows": len(bond_context["observations"]),
            "first_date": min((r["observation_date"] for r in bond_context["observations"]), default=None),
            "last_date": max((r["observation_date"] for r in bond_context["observations"]), default=None)},
    }
    return frame, coverage
