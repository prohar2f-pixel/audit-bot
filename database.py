import asyncpg
import json
from config import DATABASE_URL


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()

    async def _create_tables(self):
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS audits (
                id               SERIAL PRIMARY KEY,
                user_telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                url              TEXT NOT NULL,
                status           TEXT DEFAULT 'pending',
                scores_json      TEXT,
                average_score    FLOAT,
                created_at       TIMESTAMP DEFAULT NOW(),
                completed_at     TIMESTAMP
            )
        """)

    async def ensure_user(self, user):
        await self.pool.execute("""
            INSERT INTO users (telegram_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username   = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name  = EXCLUDED.last_name
        """, user.id, user.username, user.first_name, user.last_name)

    async def has_active_audit(self, telegram_id: int) -> bool:
        row = await self.pool.fetchrow(
            "SELECT id FROM audits WHERE user_telegram_id = $1 AND status = 'running'",
            telegram_id,
        )
        return row is not None

    async def create_audit(self, telegram_id: int, url: str) -> int:
        row = await self.pool.fetchrow("""
            INSERT INTO audits (user_telegram_id, url, status)
            VALUES ($1, $2, 'running')
            RETURNING id
        """, telegram_id, url)
        return row["id"]

    async def complete_audit(self, audit_id: int, result: dict):
        await self.pool.execute("""
            UPDATE audits
            SET status        = 'completed',
                scores_json   = $2,
                average_score = $3,
                completed_at  = NOW()
            WHERE id = $1
        """, audit_id,
            json.dumps(result["scores"], ensure_ascii=False),
            result["average_score"])

    async def fail_audit(self, audit_id: int):
        await self.pool.execute(
            "UPDATE audits SET status = 'failed' WHERE id = $1", audit_id
        )

    async def get_last_audit(self, telegram_id: int, url: str) -> dict | None:
        row = await self.pool.fetchrow("""
            SELECT average_score, scores_json, created_at
            FROM audits
            WHERE user_telegram_id = $1
              AND url = $2
              AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
        """, telegram_id, url)
        if row is None:
            return None
        return {
            "average_score": row["average_score"],
            "scores_json": row["scores_json"],
            "date": row["created_at"].strftime("%d.%m.%Y"),
            "created_at": row["created_at"],
        }

    async def delete_user_data(self, telegram_id: int):
        await self.pool.execute(
            "DELETE FROM audits WHERE user_telegram_id = $1", telegram_id
        )
        await self.pool.execute(
            "DELETE FROM users WHERE telegram_id = $1", telegram_id
        )
