"""Copy all active full-history snapshots to an isolated real PostgreSQL and test the API.

Requires requirements-dev.txt. The temporary test database is deleted on exit;
the source SQLite/PostgreSQL database is only read.
"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pgserver
from psycopg.types.json import Json
from sqlalchemy import JSON, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.app.config import DATA_DIR, INDICES, ROOT
from backend.app.db import Base, SessionLocal, engine as source_engine
from backend.app.models import Evaluation, MarketIndex, Snapshot


def main():
    with SessionLocal() as session:
        active = session.scalars(select(MarketIndex.active_snapshot).where(MarketIndex.active_snapshot.is_not(None))).all()
    if len(active) != len(INDICES):
        raise SystemExit("请先完成五个指数的历史回填")
    with tempfile.TemporaryDirectory(prefix="cn-etf-pgqa-", dir=DATA_DIR) as directory:
        pg = pgserver.get_server(Path(directory) / "pgdata", cleanup_mode="stop")
        target = create_engine(make_url(pg.get_uri()).set(drivername="postgresql+psycopg"))
        try:
            Base.metadata.create_all(target)
            counts = {}
            connection = target.raw_connection()
            try:
                with source_engine.connect() as source:
                    for table in Base.metadata.sorted_tables:
                        query = select(table)
                        if table.name == "data_snapshots":
                            query = query.where(table.c.id.in_(active))
                        elif "snapshot_id" in table.c:
                            query = query.where(table.c.snapshot_id.in_(active))
                        columns = ", ".join(f'"{col.name}"' for col in table.columns)
                        json_columns = [isinstance(col.type, JSON) for col in table.columns]
                        count = 0
                        with connection.cursor().copy(f'COPY "{table.name}" ({columns}) FROM STDIN') as copy:
                            for row in source.execution_options(yield_per=2000).execute(query):
                                copy.write_row(tuple(Json(v) if is_json and v is not None else v for v, is_json in zip(row, json_columns)))
                                count += 1
                        counts[table.name] = count
                        print(f"PostgreSQL COPY {table.name}: {count}", flush=True)
                connection.commit()
            finally:
                connection.close()
            TargetSession = sessionmaker(bind=target)
            with TargetSession() as session:
                version = session.scalar(text("SELECT version()"))
                for name, expected in counts.items():
                    assert session.scalar(select(func.count()).select_from(Base.metadata.tables[name])) == expected
            # Exercise the same HTTP application against the real target database.
            from fastapi.testclient import TestClient
            from backend.app import api
            api.SessionLocal = TargetSession
            api.seed = lambda: None
            with TestClient(api.app) as client:
                for spec in INDICES:
                    response = client.get(f"/api/v1/dashboard/{spec.code}?horizon=20")
                    assert response.status_code == 200, response.text[:300]
                    dashboard = response.json()
                    assert len(dashboard["factors"]) == 19
                    assert len(dashboard["prices"]) == dashboard["coverage"]["rows"]
                    response = client.get(f"/api/v1/indices/{spec.code}/factors/momentum_20?horizon=60")
                    assert response.status_code == 200
            result = {"status": "passed", "postgres_version": version, "tables": counts,
                "scope": "全部五个指数的当前完整历史快照，COPY后逐表计数并验证HTTP API", "temporary_database_removed": True}
            artifact = ROOT / "artifacts/postgres-verification.json"
            artifact.parent.mkdir(exist_ok=True)
            artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            target.dispose()
            pg.cleanup()


if __name__ == "__main__":
    main()
