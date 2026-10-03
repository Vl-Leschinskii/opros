#!/usr/bin/env python3
"""
Сервер опросника «Я / Мы — комплексная модель личности».

Раздаёт фронтенд (static/index.html) и сохраняет результаты опроса
в базу данных SQLite (opros.db рядом с этим файлом).

Запуск:
    python3 server.py              # http://0.0.0.0:8080
    python3 server.py 9000         # другой порт
    OPROS_DB=/path/to/base.db python3 server.py

Зависимости: только стандартная библиотека Python 3.

REST API
--------
GET    /api/health                 — проверка работоспособности
GET    /api/subjects               — список профилей
GET    /api/subjects/<имя>         — профиль: самооценка + оценки окружающих
POST   /api/responses              — сохранить результат опроса
         { "mode": "self" | "other", "subject": "Имя",
           "rater": "Имя оценивающего" (необязательно),
           "answers": [0..4 × 40], "overwrite": true|false }
DELETE /api/subjects/<имя>         — удалить профиль со всеми оценками
DELETE /api/subjects               — удалить все профили
"""

import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("OPROS_DB", os.path.join(BASE_DIR, "opros.db"))

# Порядок векторов совпадает с массивом VECTORS во фронтенде.
# Вопрос с индексом k относится к вектору VECTORS[k % 8].
VECTOR_IDS = ["mysh", "kozh", "or", "zr", "an", "ur", "zv", "ob"]
YA_VECTORS = {"an", "ur", "zv", "ob"}       # Re — мотивация
MY_VECTORS = {"mysh", "kozh", "or", "zr"}   # Im — взаимодействие
QUESTIONS_PER_VECTOR = 5
N_QUESTIONS = len(VECTOR_IDS) * QUESTIONS_PER_VECTOR  # 40
MAX_NAME_LEN = 40

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    name_norm  TEXT    NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS responses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    mode       TEXT    NOT NULL CHECK (mode IN ('self', 'other')),
    rater      TEXT    NOT NULL DEFAULT '',
    answers    TEXT    NOT NULL,            -- JSON-массив из 40 ответов (0..4)
    s_mysh     INTEGER NOT NULL,
    s_kozh     INTEGER NOT NULL,
    s_or       INTEGER NOT NULL,
    s_zr       INTEGER NOT NULL,
    s_an       INTEGER NOT NULL,
    s_ur       INTEGER NOT NULL,
    s_zv       INTEGER NOT NULL,
    s_ob       INTEGER NOT NULL,
    ya         INTEGER NOT NULL,            -- Re = an + ur + zv + ob
    my         INTEGER NOT NULL,            -- Im = mysh + kozh + or + zr
    quadrant   TEXT    NOT NULL,            -- I, II, III, IV
    created_at INTEGER NOT NULL             -- unix time, мс
);

-- У каждого человека может быть только одна самооценка.
CREATE UNIQUE INDEX IF NOT EXISTS responses_one_self
    ON responses(subject_id) WHERE mode = 'self';

CREATE INDEX IF NOT EXISTS responses_subject
    ON responses(subject_id, created_at);
"""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def now_ms():
    return int(time.time() * 1000)


def normalize_name(name):
    return " ".join(name.split()).casefold()


def compute_scores(answers):
    scores = {vid: 0 for vid in VECTOR_IDS}
    for idx, value in enumerate(answers):
        scores[VECTOR_IDS[idx % len(VECTOR_IDS)]] += value
    ya = sum(scores[v] for v in YA_VECTORS)
    my = sum(scores[v] for v in MY_VECTORS)
    u, v = ya - 40, my - 40
    if u >= 0 and v >= 0:
        quadrant = "I"
    elif u < 0 and v >= 0:
        quadrant = "II"
    elif u < 0 and v < 0:
        quadrant = "III"
    else:
        quadrant = "IV"
    return scores, ya, my, quadrant


def validate_name(raw, field, required=True):
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ApiError(400, f"Поле «{field}» должно быть строкой")
    name = " ".join(raw.split())
    if required and not name:
        raise ApiError(400, f"Поле «{field}» обязательно")
    if len(name) > MAX_NAME_LEN:
        raise ApiError(400, f"Поле «{field}» длиннее {MAX_NAME_LEN} символов")
    return name


def validate_answers(raw):
    if not isinstance(raw, list) or len(raw) != N_QUESTIONS:
        raise ApiError(400, f"Ожидается {N_QUESTIONS} ответов")
    answers = []
    for a in raw:
        if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 4:
            raise ApiError(400, "Каждый ответ должен быть целым числом от 0 до 4")
        answers.append(a)
    return answers


def find_subject(conn, name):
    return conn.execute(
        "SELECT * FROM subjects WHERE name_norm = ?", (normalize_name(name),)
    ).fetchone()


def ensure_subject(conn, name):
    row = find_subject(conn, name)
    if row:
        return row
    conn.execute(
        "INSERT INTO subjects(name, name_norm, created_at) VALUES (?, ?, ?)",
        (name, normalize_name(name), now_ms()),
    )
    return find_subject(conn, name)


def response_to_dict(row):
    return {
        "id": row["id"],
        "rater": row["rater"],
        "answers": json.loads(row["answers"]),
        "scores": {vid: row["s_" + vid] for vid in VECTOR_IDS},
        "ya": row["ya"],
        "my": row["my"],
        "quadrant": row["quadrant"],
        "ts": row["created_at"],
    }


def build_profile(conn, subject):
    rows = conn.execute(
        "SELECT * FROM responses WHERE subject_id = ? ORDER BY created_at, id",
        (subject["id"],),
    ).fetchall()
    profile = {"name": subject["name"], "self": None, "others": []}
    for row in rows:
        item = response_to_dict(row)
        if row["mode"] == "self":
            profile["self"] = item
        else:
            profile["others"].append(item)
    return profile


def list_subjects(conn):
    rows = conn.execute(
        """
        SELECT s.name,
               SUM(CASE WHEN r.mode = 'self'  THEN 1 ELSE 0 END) AS has_self,
               SUM(CASE WHEN r.mode = 'other' THEN 1 ELSE 0 END) AS others_count,
               MAX(r.created_at) AS updated_at
        FROM subjects s
        LEFT JOIN responses r ON r.subject_id = s.id
        GROUP BY s.id
        ORDER BY s.name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {
            "name": r["name"],
            "has_self": bool(r["has_self"]),
            "others_count": r["others_count"] or 0,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def save_response(conn, payload):
    mode = payload.get("mode")
    if mode not in ("self", "other"):
        raise ApiError(400, "Поле «mode» должно быть 'self' или 'other'")
    subject_name = validate_name(payload.get("subject"), "subject")
    rater = validate_name(payload.get("rater"), "rater", required=False)
    answers = validate_answers(payload.get("answers"))
    overwrite = bool(payload.get("overwrite"))

    scores, ya, my, quadrant = compute_scores(answers)

    with conn:  # транзакция
        subject = ensure_subject(conn, subject_name)
        if mode == "self":
            existing = conn.execute(
                "SELECT id FROM responses WHERE subject_id = ? AND mode = 'self'",
                (subject["id"],),
            ).fetchone()
            if existing and not overwrite:
                raise ApiError(409, "У этого профиля уже есть самооценка")
            if existing:
                conn.execute("DELETE FROM responses WHERE id = ?", (existing["id"],))
        cur = conn.execute(
            """
            INSERT INTO responses(subject_id, mode, rater, answers,
                                  s_mysh, s_kozh, s_or, s_zr, s_an, s_ur, s_zv, s_ob,
                                  ya, my, quadrant, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject["id"], mode, rater, json.dumps(answers),
                scores["mysh"], scores["kozh"], scores["or"], scores["zr"],
                scores["an"], scores["ur"], scores["zv"], scores["ob"],
                ya, my, quadrant, now_ms(),
            ),
        )
        response_id = cur.lastrowid

    return {"ok": True, "id": response_id, "subject": build_profile(conn, subject)}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "OprosServer/1.0"
    protocol_version = "HTTP/1.1"

    # --- утилиты -----------------------------------------------------------

    def _send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_file(self, filename, content_type):
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            raise ApiError(404, "Файл не найден")
        self._send_bytes(200, body, content_type)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Некорректный Content-Length")
        if length > 1_000_000:
            raise ApiError(413, "Слишком большой запрос")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Некорректный JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "Ожидается JSON-объект")
        return data

    def _subject_name_from_path(self, path):
        prefix = "/api/subjects/"
        if not path.startswith(prefix):
            return None
        name = unquote(path[len(prefix):]).strip()
        if not name:
            raise ApiError(400, "Имя профиля не указано")
        return name

    def _dispatch(self, method):
        path = urlparse(self.path).path
        try:
            handler = getattr(self, f"_handle_{method}")
            if not handler(path):
                raise ApiError(404, "Не найдено")
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except Exception as e:  # noqa: BLE001
            self.log_error("Внутренняя ошибка: %r", e)
            self._send_json({"error": "Внутренняя ошибка сервера"}, 500)

    # --- маршруты ----------------------------------------------------------

    def _handle_GET(self, path):
        if path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
            return True
        if path == "/api/health":
            self._send_json({"ok": True, "db": DB_PATH})
            return True
        if path == "/api/subjects":
            with connect() as conn:
                self._send_json({"subjects": list_subjects(conn)})
            return True
        name = self._subject_name_from_path(path)
        if name is not None:
            with connect() as conn:
                subject = find_subject(conn, name)
                if not subject:
                    raise ApiError(404, "Профиль не найден")
                self._send_json(build_profile(conn, subject))
            return True
        return False

    def _handle_HEAD(self, path):
        return self._handle_GET(path)

    def _handle_POST(self, path):
        if path == "/api/responses":
            payload = self._read_json()
            with connect() as conn:
                result = save_response(conn, payload)
            self._send_json(result, 201)
            return True
        return False

    def _handle_DELETE(self, path):
        if path == "/api/subjects":
            with connect() as conn, conn:
                conn.execute("DELETE FROM responses")
                conn.execute("DELETE FROM subjects")
            self._send_json({"ok": True})
            return True
        name = self._subject_name_from_path(path)
        if name is not None:
            with connect() as conn, conn:
                subject = find_subject(conn, name)
                if not subject:
                    raise ApiError(404, "Профиль не найден")
                conn.execute("DELETE FROM subjects WHERE id = ?", (subject["id"],))
            self._send_json({"ok": True})
            return True
        return False

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    init_db()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Опросник: http://localhost:{port}/   (база: {DB_PATH})")
    print("Остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
