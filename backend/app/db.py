from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 60} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    # v2: official base-date observations sometimes contain a close only.
    # Preserve those genuine observations; never fabricate open/high/low.
    columns = {c["name"]: c for c in inspect(engine).get_columns("daily_bars")}
    if not columns["open"]["nullable"]:
        with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                from .models import Bar
                connection.execute(text("ALTER TABLE daily_bars RENAME TO daily_bars_legacy_ohlc"))
                Bar.__table__.create(connection)
                connection.execute(text("INSERT INTO daily_bars SELECT * FROM daily_bars_legacy_ohlc"))
                connection.execute(text("DROP TABLE daily_bars_legacy_ohlc"))
            else:
                for name in ("open", "high", "low"):
                    connection.execute(text(f"ALTER TABLE daily_bars ALTER COLUMN {name} DROP NOT NULL"))
