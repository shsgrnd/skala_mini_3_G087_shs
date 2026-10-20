"""Wave 12 — 프로세스 밖(SQLite 파일)에 두는 세션 저장소.

서버가 재시작돼도(새 프로세스, 새 인스턴스) 같은 session.db 파일을 다시 열므로
대화 이력이 유지된다. in-memory dict였다면 재시작 즉시 소실됐을 상태다.
"""
import json
import sqlite3
from threading import RLock
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "session.db"


class SqliteSessionStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._lock = RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " thread_id TEXT, role TEXT, content TEXT, created TEXT)"
        )
        # 기존 messages를 변경하지 않는 추가형 migration으로 사용자 원문을 보존한다.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS thread_summaries ("
            "thread_id TEXT PRIMARY KEY, summary TEXT NOT NULL, "
            "last_compacted_rowid INTEGER NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL)"
        )
        self.conn.commit()

    def load(self, thread_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT role, content FROM messages WHERE thread_id=? ORDER BY rowid",
                (thread_id,),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def append(self, thread_id: str, role: str, content: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO messages VALUES (?,?,?,?)",
                              (thread_id, role, content, datetime.now().isoformat()))

    def memory_snapshot(self, thread_id: str) -> tuple[list[dict], dict | None]:
        # 두 SELECT를 같은 읽기 트랜잭션으로 묶어 다른 프로세스의 중간 상태를 읽지 않는다.
        with self._lock:
            self.conn.execute('BEGIN')
            try:
                rows = self.conn.execute(
                    'SELECT rowid, role, content FROM messages WHERE thread_id=? ORDER BY rowid',
                    (thread_id,)).fetchall()
                saved = self.conn.execute(
                    'SELECT summary,last_compacted_rowid,version FROM thread_summaries WHERE thread_id=?',
                    (thread_id,)).fetchone()
            finally:
                self.conn.rollback()
        messages = [dict(rowid=i, role=role, content=text) for i, role, text in rows]
        summary = (dict(summary=json.loads(saved[0]), last_compacted_rowid=saved[1], version=saved[2])
                   if saved else None)
        return messages, summary

    def save_turn(self, thread_id: str, query: str, reply: str, *, summary: dict | None = None,
                  through_rowid: int = 0, expected_version: int = 0) -> bool:
        """대화 쌍과 요약을 원자적으로 저장한다. 오래된 요약 후보는 CAS로 거부한다."""
        now = datetime.now().isoformat()
        with self._lock, self.conn:
            self.conn.executemany('INSERT INTO messages VALUES (?,?,?,?)',
                                  [(thread_id,'user',query,now),(thread_id,'assistant',reply,now)])
            if summary is None:
                return False
            if not self.conn.execute('SELECT 1 FROM messages WHERE thread_id=? AND rowid=?',
                                     (thread_id,through_rowid)).fetchone():
                raise ValueError('Summary checkpoint does not belong to this thread')
            cursor = self.conn.execute(
                'INSERT INTO thread_summaries VALUES (?,?,?,?,1) '
                'ON CONFLICT(thread_id) DO UPDATE SET summary=excluded.summary, '
                'last_compacted_rowid=excluded.last_compacted_rowid, updated_at=excluded.updated_at, '
                'version=thread_summaries.version+1 WHERE thread_summaries.version=? '
                'AND thread_summaries.last_compacted_rowid < excluded.last_compacted_rowid',
                (thread_id,json.dumps(summary,ensure_ascii=False),through_rowid,now,expected_version))
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self.conn.close()
