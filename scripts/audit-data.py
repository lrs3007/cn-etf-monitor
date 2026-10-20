"""Audit every active snapshot, including full-date factor coverage and mature labels."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, select
from backend.app.config import HORIZONS, INDICES, SPREAD_FACTOR
from backend.app.db import SessionLocal
from backend.app.models import Bar, FactorValue, ForwardReturn, MarketIndex, Snapshot, FactorInput, MacroObservation, ValuationObservation


def main():
    report = []
    reference = None
    with SessionLocal() as session:
        for spec in INDICES:
            market = session.get(MarketIndex, spec.code)
            assert market and market.active_snapshot, f"{spec.code}: 尚未回填"
            snapshot = session.get(Snapshot, market.active_snapshot)
            sid = snapshot.id
            dates = session.scalars(select(Bar.trade_date).where(Bar.snapshot_id == sid).order_by(Bar.trade_date)).all()
            count = len(dates)
            assert count == snapshot.coverage["rows"]
            assert len(set(dates)) == count
            assert dates[0] == snapshot.coverage["first_date"] and dates[-1] == snapshot.coverage["last_date"]
            definitions = snapshot.coverage["factor_definitions"]
            for factor_id in definitions:
                factor_dates = session.scalars(select(FactorValue.trade_date).where(
                    FactorValue.snapshot_id == sid, FactorValue.factor_id == factor_id)).all()
                assert len(factor_dates) == count and set(factor_dates) == set(dates), f"{spec.code}/{factor_id}: 日期覆盖不完整"
            horizons = sorted({h for definition in definitions.values() for h in definition.get("horizons", HORIZONS)})
            for horizon in horizons:
                mature = session.scalar(select(func.count()).select_from(ForwardReturn).where(
                    ForwardReturn.snapshot_id == sid, ForwardReturn.horizon == horizon, ForwardReturn.status == "ok"))
                assert mature == count - horizon
            spread_checked = 0
            if SPREAD_FACTOR in definitions:
                rows = session.execute(select(FactorInput, MacroObservation, ValuationObservation, FactorValue)
                    .join(MacroObservation, FactorInput.bond_id == MacroObservation.id)
                    .join(ValuationObservation, FactorInput.valuation_id == ValuationObservation.id)
                    .join(FactorValue, (FactorValue.snapshot_id == FactorInput.snapshot_id)
                        & (FactorValue.factor_id == FactorInput.factor_id) & (FactorValue.trade_date == FactorInput.trade_date))
                    .where(FactorInput.snapshot_id == sid, FactorInput.factor_id == SPREAD_FACTOR)).all()
                for link, bond, pe, value in rows:
                    assert pe.observation_date == bond.observation_date < link.trade_date
                    assert max(pe.available_date, bond.available_date) <= link.trade_date
                    position = dates.index(link.trade_date)
                    assert dates[position - 1] == pe.observation_date
                    if value.status == "ok":
                        assert pe.pe_ttm > 0
                        assert abs(value.value - (100 / pe.pe_ttm - bond.value)) < 1e-10
                        spread_checked += 1
                assert spread_checked == snapshot.coverage["factor_coverage"][SPREAD_FACTOR]["valid"]
            if reference is None:
                reference = set(dates)
            expected = {d for d in reference if dates[0] <= d <= dates[-1]}
            missing = sorted(expected - set(dates))
            report.append({"index": spec.code, "name": spec.short_name, "first_date": dates[0], "last_date": dates[-1],
                "rows": count, "factor_rows": count * len(definitions), "missing_vs_csi300_sessions": missing,
                "amount_rows": snapshot.coverage["amount_rows"], "valuation_rows": snapshot.coverage["valuation_rows"],
                "backcast_rows": snapshot.coverage["backcast_rows"], "snapshot_id": sid})
            report[-1]["spread_input_rows_verified"] = spread_checked
    output = {"checks": "passed", "indices": report, "total_bars": sum(r["rows"] for r in report),
        "total_factor_rows": sum(r["factor_rows"] for r in report),
        "calendar_note": "指数间逐日交叉检查，不等于独立交易所日历认证"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    artifact = Path(__file__).resolve().parents[1] / "artifacts/coverage-report.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
