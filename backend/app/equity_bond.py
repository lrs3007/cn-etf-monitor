"""Same-observation-day equity/bond inputs, eligible only after publication."""
import hashlib
import json
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import numpy as np
import pandas as pd

from .config import DATA_DIR, SHANGHAI, SPREAD_FACTOR, now_iso

SERIES_ID = "chinabond_cgb_ytm_10y"
SERIES_METADATA = {"name": "中债国债收益率曲线 · 10年期到期收益率", "unit": "%",
    "term_years": 10, "curve_type": "yield_to_maturity", "source": "chinabond",
    "url": "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery",
    "availability": "缺少历史发布时间，保守按次日可用；非严格点时数据",
    "calendar_verified": False, "pairing": "只有指数实际观测日才能参与配对；不使用债券源额外边界日期"}


class YieldTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_yields(text, start, end):
    from .providers import ProviderError
    parser = YieldTableParser()
    parser.feed(text)
    positions, result = None, []
    for cells in parser.rows:
        if all(column in cells for column in ("曲线名称", "日期", "10年")):
            positions = [cells.index(column) for column in ("曲线名称", "日期", "10年")]
            continue
        if not positions or len(cells) <= max(positions):
            continue
        name, day, value = [cells[i] for i in positions]
        if name != "中债国债收益率曲线":
            continue
        day = date.fromisoformat(day).isoformat()
        if not start <= day <= end:
            raise ProviderError("中债返回超出请求区间的记录，拒绝导入")
        if value in ("", "--", "-", "—"):
            continue
        value = float(value)
        if not np.isfinite(value) or not -5 <= value <= 30:
            raise ProviderError("中债收益率数值或百分比单位异常")
        result.append({"date": day, "available_date": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
            "value": value})
    if positions is None:
        raise ProviderError("中债历史表头变化，未找到10年期国债列")
    if len({row["date"] for row in result}) != len(result):
        raise ProviderError("中债历史数据存在重复日期")
    return sorted(result, key=lambda row: row["date"])


def identity(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def load_bond_history(offline=False, force=False):
    """Year-sized requests cover all local equity history. Closed years are archived."""
    from .providers import ProviderError, archive, fetch
    directory = DATA_DIR / "raw" / "CGB10Y"
    observations, records, errors = [], [], []
    today = datetime.now(SHANGHAI).date()
    for year in range(2002, today.year + 1):
        start, end = f"{year}-01-01", min(date(year, 12, 31), today).isoformat()
        files = sorted(directory.glob(f"chinabond-{year}-*.txt"), key=lambda p: p.stat().st_mtime)
        replayed = offline or (year < today.year and files and not force)
        print(f"[国债] {year}年 · {'归档重放' if replayed else '在线采集'}", flush=True)
        record = None
        try:
            if replayed:
                if not files:
                    raise ProviderError(f"没有{year}年国债历史归档")
                text = files[-1].read_text()
            else:
                text = fetch(SERIES_METADATA["url"], {"startDate": start, "endDate": end,
                    "gjqx": "0", "qxId": "ycqx", "locale": "cn_ZH"})
            rows = parse_yields(text, start, end)
            record = archive("CGB10Y", f"chinabond-{year}", text)
            record["cached"] = bool(replayed)
        except (ProviderError, ValueError) as exc:
            errors.append(f"国债{year}: {exc}")
            if not files:
                continue
            text = files[-1].read_text()
            rows = parse_yields(text, start, end)
            record = archive("CGB10Y", f"chinabond-{year}", text)
            record["replayed_cache"] = True
        records.append(record)
        # Preserve the actual original archive time when replaying an existing file.
        archive_path = DATA_DIR.parent / record["path"]
        record["fetched_at"] = datetime.fromtimestamp(archive_path.stat().st_mtime, SHANGHAI).isoformat(timespec="seconds")
        for row in rows:
            payload = {"series_id": SERIES_ID, "observation_date": row["date"],
                "available_date": row["available_date"], "value": row["value"],
                "raw_sha256": record["sha256"]}
            observations.append({"id": identity(payload), "series_id": SERIES_ID,
                "observation_date": row["date"], "available_date": row["available_date"], "value": row["value"],
                "provenance": {**record, "availability_assumed": True}})
    return {"observations": observations, "source_records": records, "errors": errors}


def align_spread_inputs(frame, valuations, bond_context, index_code, valuation_records=()):
    """Pair original dates before mapping to the first session after availability.

    No carry across missing observed sessions. Multiple weekend observations mapping
    to Monday resolve by observation date, then explicit provider priority.
    """
    from .providers import ProviderError
    dates = frame.date.to_numpy()
    frame["spread_pe_ttm"], frame["cgb_10y_yield"] = np.nan, np.nan
    bonds = {row["observation_date"]: row for row in bond_context["observations"]}
    latest, valuation_rows = {}, {}
    for priority, row in enumerate(valuations):
        pe = row.get("pe_ttm")
        if pe is None or not np.isfinite(float(pe)):
            continue
        day, available = str(row["date"]), str(row["available_date"])
        if available < day:
            raise ProviderError("PE可用日期早于观测日期")
        source = row.get("source", "valuation_import")
        raw = [r for r in valuation_records if r.get("source") == source]
        provenance = {"source": source, "raw_records": raw,
            "availability_assumed": row.get("availability_assumed", True), "raw_sha256": row.get("raw_sha256")}
        payload = {"index_code": index_code, "observation_date": day,
            "available_date": available, "pe_ttm": float(pe), "source": source,
            "raw_hashes": [r["sha256"] for r in raw], "raw_sha256": row.get("raw_sha256")}
        val = {"id": identity(payload), "index_code": index_code, "observation_date": day,
            "available_date": available, "pe_ttm": float(pe), "provenance": provenance}
        valuation_rows[val["id"]] = val
        bond = bonds.get(day)
        # Store even unpaired valuations for source auditing.
        eligible = max(available, bond["available_date"] if bond else available,
            (date.fromisoformat(day) + timedelta(days=1)).isoformat())
        if day not in dates:
            continue
        position = int(np.searchsorted(dates, eligible))
        if position >= len(dates):
            continue
        # A late-released old observation must not overwrite a newer observation.
        # Do not resurrect an old daily observation across later equity sessions.
        first_next = int(np.searchsorted(dates, day, side="right"))
        if position != first_next:
            continue
        rank = (day, priority)
        if position not in latest or rank > latest[position][0]:
            latest[position] = (rank, val, bond)
    inputs = []
    for position, (_, val, bond) in latest.items():
        frame.loc[position, "spread_pe_ttm"] = val["pe_ttm"]
        if bond:
            frame.loc[position, "cgb_10y_yield"] = bond["value"]
        inputs.append({"factor_id": SPREAD_FACTOR, "trade_date": str(dates[position]),
            "valuation_id": val["id"], "bond_id": bond["id"] if bond else None})
    return frame, {"inputs": inputs, "valuations": list(valuation_rows.values()),
        "bonds": bond_context["observations"]}
