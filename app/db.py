import logging
import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("meesman")


def get_engine() -> Engine:
    db_path = os.environ.get("DB_PATH", "/data/app.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30},
    )

    # WAL: lezen en schrijven blokkeren elkaar niet (webserver + scheduler
    # schrijven door elkaar); busy_timeout voorkomt 'database is locked'.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}
    if column not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        logger.info("Migratie: kolom %s.%s toegevoegd", table, column)


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

    # Dwing uniciteit op (account_number, ts) af; ruim eerst eventuele
    # bestaande duplicaten op zodat de index aangemaakt kan worden.
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM accounts_snapshot
                WHERE id NOT IN (
                    SELECT MAX(id) FROM accounts_snapshot
                    GROUP BY account_number, ts
                );
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_snapshot_acc_ts
                ON accounts_snapshot(account_number, ts);
            """))
    except Exception as e:
        logger.warning("Unieke index op accounts_snapshot kon niet worden aangemaakt: %s", e)

    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS refresh_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          status TEXT NOT NULL,
          stored_rows INTEGER NOT NULL,
          message TEXT
        );
        """))
        # v9.7: duur van de refresh in seconden (voor de statuspagina)
        _add_column_if_missing(conn, "refresh_log", "duration_s", "REAL")

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

        # v9.7: per-rekening instellingen (archiveren) + detectie van
        # rekeningen die uit de Meesman-scrape verdwenen zijn
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS account_settings (
          account_number TEXT PRIMARY KEY,
          archived INTEGER NOT NULL DEFAULT 0,
          missing_since TEXT
        );
        """))
