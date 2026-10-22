from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import os

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'monitor.db'}")
SHANGHAI = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 5, 10, 20, 60)
ENGINE_VERSION = "2026.10.v5-spread"
SPREAD_FACTOR = "equity_bond_spread_10y"
EXTRA_FACTOR_HORIZONS = {SPREAD_FACTOR: (120, 252)}


def factor_horizons(factor_id):
    return HORIZONS + EXTRA_FACTOR_HORIZONS.get(factor_id, ())


def now_iso():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IndexSpec:
    code: str
    name: str
    short_name: str
    symbol: str
    secid: str
    base_date: str
    launch_date: str
    description: str


INDICES = (
    IndexSpec("000300.SH", "沪深300指数", "沪深300", "sh000300", "1.000300", "2004-12-31", "2005-04-08", "沪深市场 · 大盘核心资产"),
    IndexSpec("000905.SH", "中证500指数", "中证500", "sh000905", "1.000905", "2004-12-31", "2007-01-15", "沪深市场 · 中盘成长"),
    IndexSpec("000016.SH", "上证50指数", "上证50", "sh000016", "1.000016", "2003-12-31", "2004-01-02", "上海市场 · 大盘蓝筹"),
    IndexSpec("399673.SZ", "创业板50指数", "创业板50", "sz399673", "0.399673", "2010-05-31", "2014-06-18", "深圳市场 · 创业板高流动性"),
    IndexSpec("000688.SH", "上证科创板50成份指数", "科创50", "sh000688", "1.000688", "2019-12-31", "2020-07-23", "上海市场 · 科技创新"),
)
INDEX_MAP = {item.code: item for item in INDICES}

# Representative funds, not an aggregate of every ETF tracking the index.
ETF_MAP = {
    "000300.SH": {"code": "510300.SH", "name": "沪深300ETF华泰柏瑞"},
    "000905.SH": {"code": "510500.SH", "name": "中证500ETF南方"},
    "000016.SH": {"code": "510050.SH", "name": "上证50ETF华夏"},
    "399673.SZ": {"code": "159949.SZ", "name": "创业板50ETF华安"},
    "000688.SH": {"code": "588000.SH", "name": "科创50ETF华夏"},
}
