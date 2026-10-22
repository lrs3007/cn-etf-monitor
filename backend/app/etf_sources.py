"""Public ETF histories with raw archives, verified dates and explicit gaps."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import date, datetime, timedelta
import hashlib
import json
import re
import subprocess
import time

import numpy as np
import pandas as pd

from .config import DATA_DIR, ETF_MAP, ROOT, SHANGHAI, now_iso
from .job_control import check_cancelled, report_progress
from .providers import ProviderError, archive, fetch, _http_client


def mapping():
    result = {key: dict(value) for key, value in ETF_MAP.items()}
    path = DATA_DIR / "imports" / "etf-map.json"
    if path.exists():
        for key, value in json.loads(path.read_text()).items():
            if key not in result or not re.fullmatch(r"\d{6}\.(SH|SZ)", value.get("code", "")) or not value.get("name"):
                raise ValueError("etf-map.json 的指数、ETF代码或名称无效")
            result[key] = {"code": value["code"], "name": str(value["name"])}
    return result


def raw_text(code, source, url, params=None, offline=False, headers=None):
    if offline:
        files = sorted((DATA_DIR / "raw" / code).glob(source + "-*.txt"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise ProviderError(f"{source} 没有可重放归档")
        text = files[-1].read_text()
    else:
        text = fetch(url, params, headers=headers)
    record = archive(code, source, text)
    record["fetched_at"] = datetime.fromtimestamp((ROOT / record["path"]).stat().st_mtime, SHANGHAI).isoformat(timespec="seconds")
    record["replayed_cache"] = offline
    return text, record


def etf_prices(instrument, offline=False):
    symbol = instrument["code"][-2:].lower() + instrument["code"][:6]
    text, record = raw_text(instrument["code"], "etf_sina", f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js", offline=offline)
    match = re.search(r'=\s*"([^\"]+)"', text)
    if not match:
        raise ProviderError("ETF历史行情编码格式变化")
    decoded = subprocess.run(["node", str(ROOT / "scripts/decode-sina.cjs")], input=json.dumps(match.group(1)),
        capture_output=True, text=True, timeout=30, check=True)
    frame = pd.DataFrame(json.loads(decoded.stdout))
    if frame.empty:
        raise ProviderError("ETF价格历史为空")
    frame["date"] = frame.date.str[:10]
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame.get(column, pd.Series(np.nan, index=frame.index)), errors="coerce")
    if (frame.close <= 0).any() or frame.close.isna().any() or (frame[["volume", "amount"]] < 0).any().any():
        raise ProviderError("ETF价格、成交量或成交额无效")
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    # This endpoint provides unadjusted prices and volume in shares, amount in CNY.
    return frame[["date", "open", "high", "low", "close", "volume", "amount"]], record


def parse_nav(text):
    # Parse a JSON literal only. Never execute a remote JavaScript program.
    match = re.search(r"(?:var\s+)?Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not match:
        raise ProviderError("历史净值JSON数组缺失")
    rows = []
    for item in json.loads(match.group(1)):
        stamp = pd.to_datetime(item["x"], unit="ms", utc=True).tz_convert(SHANGHAI)
        nav = float(item["y"])
        growth = pd.to_numeric(item.get("equityReturn"), errors="coerce")
        if not np.isfinite(nav) or nav <= 0:
            raise ProviderError("历史单位净值非正或无效")
        rows.append({"date": stamp.strftime("%Y-%m-%d"), "nav": nav,
            "nav_return": float(growth) / 100 if pd.notna(growth) else np.nan})
    frame = pd.DataFrame(rows)
    if frame.empty or frame.date.duplicated().any():
        raise ProviderError("历史净值为空或日期重复")
    return frame.sort_values("date").reset_index(drop=True)


def etf_nav(instrument, offline=False):
    text, record = raw_text(instrument["code"], "etf_nav", f"https://fund.eastmoney.com/pingzhongdata/{instrument['code'][:6]}.js", offline=offline)
    return parse_nav(text), record


def collect_fund(instrument, offline=False):
    result = {"prices": pd.DataFrame(), "nav": pd.DataFrame(), "records": [], "errors": []}
    for name, provider in (("prices", etf_prices), ("nav", etf_nav)):
        check_cancelled()
        try:
            frame, record = provider(instrument, offline)
        except (ProviderError, ValueError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {type(exc).__name__}")
            if offline:
                continue
            try:
                frame, record = provider(instrument, True)
            except (ProviderError, ValueError, subprocess.SubprocessError):
                continue
        result[name] = frame
        result["records"].append(record)
    return result


def read_cache(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_cache(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    temporary.replace(path)


def sse_share_day(day, codes, offline=False, force=False):
    path = DATA_DIR / "etf-cache" / "sse-shares" / f"{day}.json"
    cached = read_cache(path)
    recent = day >= (date.today() - timedelta(days=7)).isoformat()
    if cached and set(codes).issubset(set(cached.get("codes", []))) and (offline or (not force and not recent and time.time() - cached.get("checked_at", 0) < 7 * 86400)):
        return cached
    if offline:
        return cached or {"rows": [], "missing": True}
    text = fetch("https://query.sse.com.cn/commonQuery.do", {
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "STAT_DATE": day,
        "isPagination": "true", "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1", "pageHelp.endPage": "1", "pageHelp.cacheSize": "1"},
        headers={"Referer": "https://www.sse.com.cn/"})
    payload = json.loads(text)
    rows = payload.get("result")
    if not isinstance(rows, list) or payload.get("actionErrors"):
        raise ProviderError("上交所份额返回格式无效")
    if int(payload.get("pageHelp", {}).get("total") or len(rows)) > len(rows):
        raise ProviderError("上交所份额分页被截断，拒绝使用部分数据")
    selected = []
    for row in rows:
        code = str(row["SEC_CODE"]).zfill(6) + ".SH"
        if code not in codes:
            continue
        if row["STAT_DATE"][:10] != day:
            raise ProviderError("上交所份额日期与请求不符")
        shares = float(str(row["TOT_VOL"]).replace(",", "")) * 10000
        if shares <= 0:
            raise ProviderError("上交所ETF份额无效")
        selected.append({"date": day, "code": code, "shares": shares})
    record = archive("ETF-SSE", "shares_" + day, text)
    result = {"rows": selected, "record": record, "checked_at": time.time(), "codes": sorted(codes)}
    save_cache(path, result)
    return result


def collect_sse_shares(days, codes, offline=False, force=False):
    result, records, errors = {}, [], []
    # Bounded queue: full initial backfill once; subsequent refreshes reuse closed dates.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="etf-shares") as executor:
        pending = {executor.submit(copy_context().run, sse_share_day, day, codes, offline, force): day for day in days}
        for number, future in enumerate(as_completed(pending), 1):
            check_cancelled()
            day = pending[future]
            try:
                item = future.result()
                for row in item["rows"]:
                    result[(row["code"], row["date"])] = {**row, "record": item.get("record")}
                if item.get("record"):
                    records.append(item["record"])
                if item.get("missing"):
                    errors.append(day)
            except (ProviderError, ValueError) as exc:
                errors.append(day)
                cached = read_cache(DATA_DIR / "etf-cache" / "sse-shares" / f"{day}.json")
                if cached:
                    for row in cached["rows"]:
                        result[(row["code"], row["date"])] = {**row, "record": cached.get("record")}
            report_progress("collect_etf", "回填并核对上交所ETF真实历史份额", number, len(days), "个日期",
                missing_dates=len(errors))
    return result, records, errors


def szse_share_window(code, start, end, offline=False, force=False):
    path = DATA_DIR / "etf-cache" / "szse-shares" / f"{code}-{start}-{end}.json"
    cached = read_cache(path)
    if cached and (offline or (not force and end < (date.today() - timedelta(days=7)).isoformat()
                               and time.time() - cached.get("checked_at", 0) < 7 * 86400)):
        return cached
    if offline:
        return cached or {"rows": [], "missing": True}
    rows, records, pages = [], [], 1
    page = 1
    while page <= pages:
        check_cancelled()
        text = fetch("https://fund.szse.cn/api/report/ShowReport/data", {"CATALOGID": "scsj_fund_jjgm",
            "SHOWTYPE": "JSON", "jjlb": "ETF", "txtDm": code[:6], "txtStart": start, "txtEnd": end,
            "PAGENO": str(page)}, headers={"Referer": "https://fund.szse.cn/"})
        payload = json.loads(text)
        if not isinstance(payload, list) or not payload or "metadata" not in payload[0]:
            raise ProviderError("深交所历史份额格式无效")
        block = payload[0]
        pages = int(block["metadata"].get("pagecount") or 0)
        for row in block.get("data", []):
            day = row["size_date"][:10]
            if str(row["fund_code"]).strip().zfill(6) != code[:6] or not start <= day <= end:
                raise ProviderError("深交所份额代码或日期不符")
            shares = float(str(row["current_size"]).replace(",", "")) * 10000
            if shares <= 0:
                raise ProviderError("深交所ETF份额无效")
            rows.append({"date": day, "code": code, "shares": shares})
        records.append(archive(code, f"etf_shares_{start}_{end}_{page}", text))
        page += 1
        if page > 100:
            raise ProviderError("深交所份额分页超出预期")
    if len(rows) != int(block["metadata"].get("recordcount") or 0):
        raise ProviderError("深交所份额分页覆盖不完整")
    result = {"rows": rows, "records": records, "checked_at": time.time()}
    save_cache(path, result)
    return result


def prepare_etf_context(specs, offline=False, force=False):
    instruments = mapping()
    result = {"instruments": instruments, "funds": {}, "shares": {}, "errors": [], "share_records": []}
    report_progress("collect_etf", "采集代表ETF的完整不复权行情与历史净值")
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="etf-history") as executor:
        pending = {executor.submit(copy_context().run, collect_fund, instruments[s.code], offline): s for s in specs}
        for future in as_completed(pending):
            spec = pending[future]
            result["funds"][spec.code] = future.result()
    now = datetime.now(SHANGHAI)
    completed = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
    sse_days, sse_codes = set(), set()
    for spec in specs:
        instrument = instruments[spec.code]
        prices = result["funds"][spec.code]["prices"]
        if prices.empty:
            continue
        days = prices.loc[prices.date <= completed.isoformat(), "date"].tolist()
        if instrument["code"].endswith(".SH"):
            sse_codes.add(instrument["code"]); sse_days.update(days)
        else:
            start = date.fromisoformat(days[0]) if days else completed
            while start <= completed:
                end = min(start + timedelta(days=178), completed)
                report_progress("collect_etf", f"深交所ETF份额：{start} → {end}")
                try:
                    block = szse_share_window(instrument["code"], start.isoformat(), end.isoformat(), offline, force)
                    for row in block["rows"]:
                        result["shares"][(row["code"], row["date"])] = {**row, "record": {"source": "szse_shares", "raw_records": block.get("records", [])}}
                    result["share_records"].extend(block.get("records", []))
                except (ProviderError, ValueError) as exc:
                    result["errors"].append(f"深交所份额 {start}：{type(exc).__name__}")
                start = end + timedelta(days=1)
    if sse_days:
        shares, records, gaps = collect_sse_shares(sorted(sse_days), sse_codes, offline, force)
        result["shares"].update(shares); result["share_records"].extend(records)
        if gaps:
            result["errors"].append(f"上交所历史份额有{len(gaps)}个日期未取得，未填充")
    return result
