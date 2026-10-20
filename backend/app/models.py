from typing import Optional

from sqlalchemy import Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class MarketIndex(Base):
    __tablename__ = "market_indices"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    base_date: Mapped[str] = mapped_column(String(10))
    launch_date: Mapped[str] = mapped_column(String(10))
    active_snapshot: Mapped[Optional[str]] = mapped_column(String(40))


class Snapshot(Base):
    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    index_code: Mapped[str] = mapped_column(ForeignKey("market_indices.code"), index=True)
    created_at: Mapped[str] = mapped_column(String(40))
    engine_version: Mapped[str] = mapped_column(String(40))
    dataset_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    coverage: Mapped[dict] = mapped_column(JSON)


class Bar(Base):
    __tablename__ = "daily_bars"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    trade_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float)
    pb: Mapped[Optional[float]] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40))
    availability: Mapped[str] = mapped_column(String(40))
    history_type: Mapped[str] = mapped_column(String(20))


class FactorDefinition(Base):
    __tablename__ = "factor_definitions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)


class FactorValue(Base):
    __tablename__ = "factor_values"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    factor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    trade_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    value: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24))
    __table_args__ = (Index("ix_factor_history", "snapshot_id", "factor_id", "trade_date"),)


class ForwardReturn(Base):
    __tablename__ = "forward_returns"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    trade_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[Optional[float]] = mapped_column(Float)
    end_date: Mapped[Optional[str]] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(24))


class Evaluation(Base):
    __tablename__ = "factor_evaluations"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    factor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)


class RollingEvaluation(Base):
    __tablename__ = "rolling_evaluations"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    factor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[str] = mapped_column(String(10), primary_key=True)
    ic: Mapped[Optional[float]] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    started_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[Optional[str]] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict] = mapped_column(JSON)


class Assessment(Base):
    __tablename__ = "index_assessments"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)


class MacroSeries(Base):
    __tablename__ = "macro_series"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)


class MacroObservation(Base):
    __tablename__ = "macro_observations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("macro_series.id"))
    observation_date: Mapped[str] = mapped_column(String(10), index=True)
    available_date: Mapped[str] = mapped_column(String(10))
    value: Mapped[float] = mapped_column(Float)
    provenance: Mapped[dict] = mapped_column(JSON)


class ValuationObservation(Base):
    __tablename__ = "valuation_observations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    index_code: Mapped[str] = mapped_column(ForeignKey("market_indices.code"))
    observation_date: Mapped[str] = mapped_column(String(10))
    available_date: Mapped[str] = mapped_column(String(10))
    pe_ttm: Mapped[float] = mapped_column(Float)
    provenance: Mapped[dict] = mapped_column(JSON)


class FactorInput(Base):
    __tablename__ = "factor_inputs"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"), primary_key=True)
    factor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    trade_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    valuation_id: Mapped[Optional[str]] = mapped_column(ForeignKey("valuation_observations.id"))
    bond_id: Mapped[Optional[str]] = mapped_column(ForeignKey("macro_observations.id"))
