import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.config import INDICES
from backend.app.providers import ProviderError, full_history, load_valuations


@pytest.fixture(autouse=True)
def no_unmocked_supplemental_network(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ProviderError("fixture has no CSI supplement")
    monkeypatch.setattr("backend.app.providers.csindex_history", unavailable)


def mock_frame(price=1000):
    return pd.DataFrame({"date": ["2020-01-02", "2020-01-03"], "open": [price, price],
        "high": [price + 10, price + 10], "low": [price - 10, price - 10], "close": [price, price],
        "volume": [10000., 10000.], "amount": [np.nan, np.nan], "source": ["sina", "sina"]})


def test_dual_sources_are_checked_and_amount_is_supplemented(monkeypatch):
    a, b = mock_frame(), mock_frame()
    b["amount"] = 1e8
    b["source"] = "eastmoney"
    monkeypatch.setattr("backend.app.providers.sina_history", lambda *a, **k: (globals()["mock_frame"](), {}))
    monkeypatch.setattr("backend.app.providers.eastmoney_history", lambda *a, **k: (b, {}))
    monkeypatch.setattr("backend.app.providers.load_valuations", lambda *a, **k: ([], []))
    result, coverage = full_history(INDICES[0])
    assert result.amount.notna().all()
    assert coverage["cross_source_checked"]


def test_discrepant_source_prevents_publication(monkeypatch):
    monkeypatch.setattr("backend.app.providers.sina_history", lambda *a, **k: (mock_frame(), {}))
    monkeypatch.setattr("backend.app.providers.eastmoney_history", lambda *a, **k: (mock_frame(1020), {}))
    with pytest.raises(ProviderError, match="双源"):
        full_history(INDICES[0])


def test_official_close_only_base_is_not_fabricated(monkeypatch):
    official = mock_frame()
    base = pd.DataFrame([{"date": "2010-05-31", "open": np.nan, "high": np.nan,
        "low": np.nan, "close": 1000., "volume": np.nan, "amount": np.nan, "source": "cnindex"}])
    official = pd.concat([base, official], ignore_index=True)
    monkeypatch.setattr("backend.app.providers.sina_history", lambda *a, **k: (mock_frame(), {}))
    monkeypatch.setattr("backend.app.providers.eastmoney_history", lambda *a, **k: (mock_frame(), {}))
    monkeypatch.setattr("backend.app.providers.cnindex_history", lambda *a, **k: (official, {}))
    monkeypatch.setattr("backend.app.providers.load_valuations", lambda *a, **k: ([], []))
    result, _ = full_history(INDICES[3])
    assert result.date.iloc[0] == "2010-05-31"
    assert result.close.iloc[0] == 1000
    assert pd.isna(result.open.iloc[0]) and pd.isna(result.high.iloc[0])


def test_all_online_failures_cannot_appear_as_fresh_success(monkeypatch):
    def provider(*args, offline=False):
        if not offline:
            raise ProviderError("network unavailable")
        return mock_frame(), {}
    monkeypatch.setattr("backend.app.providers.sina_history", provider)
    monkeypatch.setattr("backend.app.providers.eastmoney_history", provider)
    with pytest.raises(ProviderError, match="全部在线数据源失败"):
        full_history(INDICES[0])


def test_csi_boundary_dates_are_not_imported_as_trading_sessions(monkeypatch):
    primary = mock_frame()
    official = pd.concat([primary, primary.iloc[:1].assign(date="2020-01-01")], ignore_index=True)
    official["amount"] = 1e8
    official["pe_observed"] = 20.
    official["source"] = "csindex"
    monkeypatch.setattr("backend.app.providers.sina_history", lambda *a, **k: (primary, {}))
    monkeypatch.setattr("backend.app.providers.eastmoney_history", lambda *a, **k: (primary, {}))
    monkeypatch.setattr("backend.app.providers.csindex_history", lambda *a, **k: (official, {}))
    monkeypatch.setattr("backend.app.providers.load_valuations", lambda *a, **k: ([], []))
    result, coverage = full_history(INDICES[0])
    assert "2020-01-01" not in result.date.tolist()
    assert result.amount.notna().all()
    assert pd.isna(result.pe_ttm.iloc[0])
    assert result.pe_ttm.iloc[1] == 20.  # First day's PE becomes eligible the next day.


def test_import_cannot_claim_values_before_the_observation_date(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.app.providers.DATA_DIR", tmp_path)
    path = tmp_path / "imports"
    path.mkdir()
    (path / "000300.SH-valuations.csv").write_text("date,available_date,pe_ttm,pb\n2020-01-03,2020-01-02,12,1.5\n")
    with pytest.raises(ProviderError, match="前视偏差"):
        load_valuations(INDICES[0], offline=True)
