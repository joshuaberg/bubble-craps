"""Data API — read-only REST service for Tesserae dashboard widgets and other consumers."""

import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import psycopg2
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# -- Configuration (from environment variables) --------------------------------

DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "bubble_craps")
DB_USER = os.environ.get("DB_USER", "bubble_craps")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "bubble_craps")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")

# -- Database ------------------------------------------------------------------

db_conn = None

# Tables that the game server will create (not yet deployed)
GAME_TABLES = {"tables", "rounds", "players", "bets", "table_membership"}
available_tables = set()


def connect_db_startup():
    """Connect to Postgres with retries at startup (waits for container to be ready)."""
    for attempt in range(30):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
            )
            conn.autocommit = True
            logger.info("Connected to Postgres")
            return conn
        except psycopg2.OperationalError:
            logger.warning("Postgres not ready, retrying in 2s (attempt %d/30)", attempt + 1)
            time.sleep(2)
    logger.error("Could not connect to Postgres after 30 attempts")
    sys.exit(1)


def reconnect_db():
    """Reconnect to Postgres during operation. Short retry (3 attempts, ~6s)."""
    global db_conn
    if db_conn is not None:
        try:
            db_conn.close()
        except Exception:
            pass

    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
            )
            conn.autocommit = True
            logger.info("Reconnected to Postgres")
            db_conn = conn
            return True
        except psycopg2.OperationalError:
            logger.warning("Postgres reconnect failed (attempt %d/3)", attempt + 1)
            time.sleep(2)

    logger.error("Could not reconnect to Postgres after 3 attempts")
    db_conn = None
    return False


def with_db(fn):
    """Run fn(conn). On connection error, reconnect once and retry.
    Returns the result of fn, or None if both attempts fail."""
    global db_conn
    if db_conn is None:
        if not reconnect_db():
            return None
    try:
        return fn(db_conn)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        logger.warning("DB connection lost, reconnecting")
        if reconnect_db():
            try:
                return fn(db_conn)
            except psycopg2.Error:
                logger.exception("DB operation failed after reconnect")
        return None


def probe_tables():
    """Check which tables exist in the database."""
    def _probe(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('rolls', 'tables', 'rounds', 'players',
                                      'bets', 'table_membership')
                """
            )
            return {row[0] for row in cur.fetchall()}
    result = with_db(_probe)
    return result if result is not None else set()


def has_table(name):
    """Check if a table is available, re-probing if previously missing."""
    global available_tables
    if name in available_tables:
        return True
    # Re-probe in case the game server was deployed
    available_tables = probe_tables()
    return name in available_tables


# -- FastAPI app ---------------------------------------------------------------

app = FastAPI(title="Bubble Craps Data API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    global db_conn, available_tables
    db_conn = connect_db_startup()
    available_tables = probe_tables()
    logger.info("Available tables: %s", available_tables)


# -- Roll endpoints ------------------------------------------------------------

@app.get("/api/rolls")
def get_rolls(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    device_id: Optional[str] = None,
    status: Optional[str] = None,
):
    """Recent rolls, newest first."""
    def _query(conn):
        conditions = []
        params = []

        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)
        if status:
            conditions.append("roll_status = %s")
            params.append(status)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        with conn.cursor() as cur:
            # Get total count
            cur.execute(f"SELECT COUNT(*) FROM rolls {where}", params)
            total = cur.fetchone()[0]

            # Get page
            cur.execute(
                f"""
                SELECT roll_id, device_id, die1, die2, total,
                       roll_status, timestamp
                FROM rolls
                {where}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()

        rolls = [
            {
                "roll_id": r[0],
                "device_id": r[1],
                "die1": r[2],
                "die2": r[3],
                "total": r[4],
                "roll_status": r[5],
                "timestamp": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
        return {"rolls": rolls, "count": len(rolls), "total": total}

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/rolls/distribution")
def get_rolls_distribution(
    device_id: Optional[str] = None,
    since: Optional[str] = None,
):
    """Roll total histogram (only successful rolls)."""
    def _query(conn):
        conditions = ["roll_status = 'ok'"]
        params = []

        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)
        if since:
            conditions.append("timestamp >= %s")
            params.append(since)

        where = "WHERE " + " AND ".join(conditions)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT total, COUNT(*) as count
                FROM rolls
                {where}
                GROUP BY total
                ORDER BY total
                """,
                params,
            )
            rows = cur.fetchall()

        distribution = [{"total": r[0], "count": r[1]} for r in rows]
        total_rolls = sum(r[1] for r in rows)

        return {
            "distribution": distribution,
            "total_rolls": total_rolls,
            "device_id": device_id,
            "since": since,
        }

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/rolls/stats")
def get_rolls_stats(device_id: Optional[str] = None):
    """Summary statistics."""
    def _query(conn):
        condition = "WHERE device_id = %s" if device_id else ""
        params = [device_id] if device_id else []

        with conn.cursor() as cur:
            # Counts
            cur.execute(f"SELECT COUNT(*) FROM rolls {condition}", params)
            total_rolls = cur.fetchone()[0]

            cur.execute(
                f"SELECT COUNT(*) FROM rolls {condition + ' AND ' if condition else 'WHERE '}roll_status = 'ok'",
                params,
            )
            successful_rolls = cur.fetchone()[0]

            failed_rolls = total_rolls - successful_rolls
            success_rate = round(successful_rolls / total_rolls, 3) if total_rolls > 0 else 0

            # Last roll
            cur.execute(
                f"""
                SELECT roll_id, die1, die2, total, timestamp
                FROM rolls
                {condition}
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                params,
            )
            last_row = cur.fetchone()

            # First and last timestamps
            cur.execute(
                f"SELECT MIN(timestamp), MAX(timestamp) FROM rolls {condition}",
                params,
            )
            ts_row = cur.fetchone()

        last_roll = None
        if last_row:
            last_roll = {
                "roll_id": last_row[0],
                "die1": last_row[1],
                "die2": last_row[2],
                "total": last_row[3],
                "timestamp": last_row[4].isoformat() if last_row[4] else None,
            }

        return {
            "total_rolls": total_rolls,
            "successful_rolls": successful_rolls,
            "failed_rolls": failed_rolls,
            "success_rate": success_rate,
            "last_roll": last_roll,
            "first_roll_at": ts_row[0].isoformat() if ts_row and ts_row[0] else None,
            "last_roll_at": ts_row[1].isoformat() if ts_row and ts_row[1] else None,
        }

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


# -- Game endpoints (gracefully unavailable until game server is deployed) -----

@app.get("/api/tables")
def get_tables(status: str = "active"):
    """List active game tables."""
    if not has_table("tables"):
        return {"tables": [], "count": 0, "available": False}

    def _query(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.table_id, t.device_id, t.type, t.status, t.created_at,
                       (SELECT COUNT(*) FROM table_membership tm
                        WHERE tm.table_id = t.table_id AND tm.left_at IS NULL)
                FROM tables t
                WHERE t.status = %s
                ORDER BY t.created_at DESC
                """,
                (status,),
            )
            rows = cur.fetchall()

        tables = [
            {
                "table_id": str(r[0]),
                "device_id": r[1],
                "type": r[2],
                "status": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "player_count": r[5],
            }
            for r in rows
        ]
        return {"tables": tables, "count": len(tables)}

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/tables/{table_id}")
def get_table(table_id: str):
    """Full table state including current round and seated players."""
    if not has_table("tables"):
        return {"available": False, "message": "Game server tables not yet created."}

    def _query(conn):
        with conn.cursor() as cur:
            # Table info
            cur.execute(
                "SELECT table_id, device_id, type, status, created_at FROM tables WHERE table_id = %s",
                (table_id,),
            )
            table_row = cur.fetchone()
            if not table_row:
                return None

            # Current round (most recent incomplete round)
            cur.execute(
                """
                SELECT round_id, phase, point, rolls, outcome, started_at
                FROM rounds
                WHERE table_id = %s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (table_id,),
            )
            round_row = cur.fetchone()

            # Seated players
            cur.execute(
                """
                SELECT p.player_id, p.name, p.balance
                FROM players p
                JOIN table_membership tm ON tm.player_id = p.player_id
                WHERE tm.table_id = %s AND tm.left_at IS NULL
                """,
                (table_id,),
            )
            player_rows = cur.fetchall()

            # Active bet count
            cur.execute(
                "SELECT COUNT(*) FROM bets WHERE table_id = %s AND status = 'active'",
                (table_id,),
            )
            active_bets = cur.fetchone()[0]

        current_round = None
        if round_row:
            current_round = {
                "round_id": str(round_row[0]),
                "phase": round_row[1],
                "point": round_row[2],
                "rolls": round_row[3],
                "outcome": round_row[4],
                "started_at": round_row[5].isoformat() if round_row[5] else None,
            }

        players = [
            {"player_id": str(r[0]), "name": r[1], "balance": float(r[2])}
            for r in player_rows
        ]

        return {
            "table_id": str(table_row[0]),
            "device_id": table_row[1],
            "type": table_row[2],
            "status": table_row[3],
            "current_round": current_round,
            "players": players,
            "active_bets": active_bets,
        }

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/tables/{table_id}/bets")
def get_table_bets(table_id: str, status: str = "active"):
    """Bets on a table."""
    if not has_table("bets"):
        return {"bets": [], "count": 0, "available": False}

    def _query(conn):
        with conn.cursor() as cur:
            condition = "AND b.status = %s" if status != "all" else ""
            params = [table_id, status] if status != "all" else [table_id]

            cur.execute(
                f"""
                SELECT b.bet_id, p.name, b.type, b.amount, b.status, b.payout
                FROM bets b
                JOIN players p ON p.player_id = b.player_id
                WHERE b.table_id = %s {condition}
                ORDER BY b.placed_at DESC
                """,
                params,
            )
            rows = cur.fetchall()

        bets = [
            {
                "bet_id": str(r[0]),
                "player_name": r[1],
                "type": r[2],
                "amount": float(r[3]),
                "status": r[4],
                "payout": float(r[5]) if r[5] is not None else None,
            }
            for r in rows
        ]
        return {"bets": bets, "count": len(bets)}

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/players")
def get_players():
    """All players and balances."""
    if not has_table("players"):
        return {"players": [], "count": 0, "available": False}

    def _query(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT player_id, name, balance FROM players ORDER BY name"
            )
            rows = cur.fetchall()

        players = [
            {"player_id": str(r[0]), "name": r[1], "balance": float(r[2])}
            for r in rows
        ]
        return {"players": players, "count": len(players)}

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


@app.get("/api/players/{player_id}")
def get_player(player_id: str):
    """Single player detail with recent bet history and stats."""
    if not has_table("players"):
        return {"available": False, "message": "Game server tables not yet created."}

    def _query(conn):
        with conn.cursor() as cur:
            # Player info
            cur.execute(
                "SELECT player_id, name, balance, created_at FROM players WHERE player_id = %s",
                (player_id,),
            )
            player_row = cur.fetchone()
            if not player_row:
                return None

            # Recent bets
            cur.execute(
                """
                SELECT bet_id, table_id, type, amount, status, payout, resolved_at
                FROM bets
                WHERE player_id = %s
                ORDER BY placed_at DESC
                LIMIT 20
                """,
                (player_id,),
            )
            bet_rows = cur.fetchall()

            # Stats
            cur.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE status = 'won'),
                    COUNT(*) FILTER (WHERE status = 'lost'),
                    COALESCE(SUM(CASE WHEN status = 'won' THEN payout ELSE 0 END), 0)
                    - COALESCE(SUM(CASE WHEN status = 'lost' THEN amount ELSE 0 END), 0)
                FROM bets
                WHERE player_id = %s AND status IN ('won', 'lost')
                """,
                (player_id,),
            )
            stats_row = cur.fetchone()

        recent_bets = [
            {
                "bet_id": str(r[0]),
                "table_id": str(r[1]),
                "type": r[2],
                "amount": float(r[3]),
                "status": r[4],
                "payout": float(r[5]) if r[5] is not None else None,
                "resolved_at": r[6].isoformat() if r[6] else None,
            }
            for r in bet_rows
        ]

        return {
            "player_id": str(player_row[0]),
            "name": player_row[1],
            "balance": float(player_row[2]),
            "created_at": player_row[3].isoformat() if player_row[3] else None,
            "recent_bets": recent_bets,
            "stats": {
                "total_bets": stats_row[0],
                "wins": stats_row[1],
                "losses": stats_row[2],
                "net": float(stats_row[3]),
            },
        }

    result = with_db(_query)
    if result is None:
        return {"error": "database unavailable"}, 503
    return result


# -- Health endpoint -----------------------------------------------------------

@app.get("/api/health")
def health():
    """Health check — DB connection status and available tables."""
    tables = probe_tables()
    all_table_names = ["rolls", "tables", "rounds", "players", "bets", "table_membership"]

    return {
        "status": "ok" if db_conn is not None else "degraded",
        "database": "connected" if db_conn is not None else "disconnected",
        "tables_available": {name: name in tables for name in all_table_names},
    }
