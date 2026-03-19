import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def get_engine() -> Engine:
    db_path = os.environ.get("DB_PATH", "/data/app.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", future=True)


def init_db(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS accounts_snapshot (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          account_number TEXT NOT NULL,
          label TEXT NOT NULL,
          value_eur REAL NOT NULL
        );
        """))

        conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_accounts_snapshot_acc_ts
        ON accounts_snapshot(account_number, ts);
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS refresh_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          status TEXT NOT NULL,
          stored_rows INTEGER NOT NULL,
          message TEXT
        );
        """))

        conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_refresh_log_ts
        ON refresh_log(ts);
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS keepalive_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          status TEXT NOT NULL,
          message TEXT
        );
        """))

        conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_keepalive_log_ts
        ON keepalive_log(ts);
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS deposits (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          account_number TEXT NOT NULL,
          label TEXT NOT NULL,
          amount_eur REAL NOT NULL,
          note TEXT
        );
        """))

        conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_deposits_acc_ts
        ON deposits(account_number, ts);
        """))
