"""SQLite store: one row per agent run, one row per executed step (with its JSON output), and the generated files.
This replaces n8n's execution history – the portal and the ARIS event-log export read from here."""
from __future__ import annotations
import json, sqlite3, threading, time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, stopped_at TEXT,
  token TEXT, source TEXT, applicant_name TEXT, insurance_number TEXT, application_id TEXT, final_status TEXT, route TEXT,
  decision TEXT, channel TEXT, error TEXT, intake TEXT);
CREATE TABLE IF NOT EXISTS steps (
  run_id INTEGER, seq INTEGER, node TEXT, started_ms REAL, duration_ms INTEGER, output TEXT, error TEXT,
  PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS files (run_id INTEGER, name TEXT, mime TEXT, data BLOB, PRIMARY KEY (run_id, name));
"""


class DB:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(str(path), check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._c.executescript(SCHEMA)
            self._c.commit()

    def _x(self, sql: str, args=()):
        with self._lock:
            cur = self._c.execute(sql, args)
            self._c.commit()
            return cur

    def q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    # ---- runs
    def new_run(self, agent: str, token: str, source: str, intake: dict) -> int:
        now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'
        return self._x('INSERT INTO runs(agent,status,started_at,token,source,intake) VALUES (?,?,?,?,?,?)',
                       (agent, 'running', now, token, source, json.dumps(intake, ensure_ascii=False))).lastrowid

    def update_run(self, run_id: int, **fields) -> None:
        if fields.get('status') in ('success', 'error') and 'stopped_at' not in fields:
            fields['stopped_at'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'
        cols = ', '.join(f'{k}=?' for k in fields)
        self._x(f'UPDATE runs SET {cols} WHERE id=?', (*fields.values(), run_id))

    def run(self, run_id: int) -> dict | None:
        r = self.q('SELECT * FROM runs WHERE id=?', (run_id,))
        return r[0] if r else None

    def runs(self, agent: str | None = None, limit: int = 50) -> list[dict]:
        if agent:
            return self.q('SELECT * FROM runs WHERE agent=? ORDER BY id DESC LIMIT ?', (agent, limit))
        return self.q('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,))

    # ---- steps
    def add_step(self, run_id: int, node: str, started_ms: float, duration_ms: int, output, error: str | None = None) -> None:
        seq = self.q('SELECT COALESCE(MAX(seq),0)+1 AS n FROM steps WHERE run_id=?', (run_id,))[0]['n']
        self._x('INSERT INTO steps VALUES (?,?,?,?,?,?,?)',
                (run_id, seq, node, started_ms, duration_ms, json.dumps(output, ensure_ascii=False, default=str) if output is not None else None, error))

    def steps(self, run_id: int, with_output: bool = True) -> list[dict]:
        rows = self.q('SELECT * FROM steps WHERE run_id=? ORDER BY seq', (run_id,))
        for r in rows:
            r['output'] = json.loads(r['output']) if (with_output and r['output']) else None
        return rows

    def outputs(self, run_id: int) -> dict[str, list[dict]]:
        """node name -> its latest output (like n8n's runData)."""
        out: dict[str, list[dict]] = {}
        for r in self.steps(run_id):
            if r['output'] is not None:
                out[r['node']] = r['output']
        return out

    # ---- files
    def put_file(self, run_id: int, name: str, mime: str, data: bytes) -> None:
        self._x('INSERT OR REPLACE INTO files VALUES (?,?,?,?)', (run_id, name, mime, data))

    def files(self, run_id: int) -> list[dict]:
        return self.q('SELECT name, mime, data FROM files WHERE run_id=?', (run_id,))
