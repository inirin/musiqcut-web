import aiosqlite
from pathlib import Path

DB_PATH = "pipeline.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                theme       TEXT NOT NULL,
                mood        TEXT NOT NULL,
                length      TEXT DEFAULT 'short',
                source      TEXT DEFAULT 'manual',
                status      TEXT DEFAULT 'pending',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                video_path  TEXT,
                error_msg   TEXT
            );

            CREATE TABLE IF NOT EXISTS pipeline_steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  TEXT REFERENCES projects(id),
                step_no     INTEGER,
                step_name   TEXT,
                status      TEXT,
                started_at  DATETIME,
                finished_at DATETIME,
                output_data TEXT,
                error_msg   TEXT
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT REFERENCES projects(id),
                step_no       INTEGER,
                scene_no      INTEGER,
                feedback_type TEXT NOT NULL,
                content       TEXT,
                processed     INTEGER DEFAULT 0,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS platform_accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                platform        TEXT NOT NULL DEFAULT 'youtube',
                channel_id      TEXT,
                channel_title   TEXT,
                access_token    TEXT,
                refresh_token   TEXT,
                token_expires_at DATETIME,
                scope           TEXT,
                connected_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id      TEXT REFERENCES projects(id),
                platform        TEXT NOT NULL DEFAULT 'youtube',
                platform_video_id TEXT,
                platform_url    TEXT,
                title           TEXT,
                description     TEXT,
                tags            TEXT,
                status          TEXT DEFAULT 'pending',
                uploaded_at     DATETIME,
                error_msg       TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS auto_schedule (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_type   TEXT DEFAULT 'generation',
                enabled         INTEGER DEFAULT 0,
                interval_hours  REAL DEFAULT 2.0,
                last_success_at DATETIME,
                last_failure_at DATETIME,
                last_failure_reason TEXT,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

        """)
        await db.commit()

        # 기존 DB 마이그레이션 (새 컬럼 추가 — 이미 있으면 무시)
        for col, sql in [
            ("length", "ALTER TABLE projects ADD COLUMN length TEXT DEFAULT 'short'"),
            ("source", "ALTER TABLE projects ADD COLUMN source TEXT DEFAULT 'manual'"),
        ]:
            try:
                await db.execute(sql)
                await db.commit()
            except Exception:
                pass

        # 서버 재시작 시 좀비 프로젝트 정리 (running → failed)
        await db.execute(
            "UPDATE projects SET status='failed', error_msg='서버 재시작으로 중단됨' "
            "WHERE status='running'"
        )
        await db.execute(
            "UPDATE pipeline_steps SET status='failed', error_msg='서버 재시작으로 중단됨' "
            "WHERE status='running'"
        )
        await db.commit()


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db
