#!/usr/bin/env python3
"""
Сервер опросника «Я / Мы — комплексная модель личности».

Раздаёт фронтенд (static/index.html) и сохраняет результаты опроса
в базу данных SQLite (opros.db рядом с этим файлом).

Запуск:
    python3 server.py              # http://0.0.0.0:8080
    python3 server.py 9000         # другой порт
    OPROS_DB=/path/to/base.db python3 server.py
    docker compose up -d           # на хостинге, за nginx: /opros/

Зависимости: только стандартная библиотека Python 3.

Доступ к профилям
-----------------
Браузер посетителя получает cookie opros_visitor со случайным токеном.
Тот, кто заполнил «Я о себе», становится владельцем профиля: в subjects
хранится хэш его токена. Сводный профиль (самооценка + оценки окружающих)
видит только владелец из своего браузера (или админ). Остальные участники
чужие профили не видят — ни список, ни содержимое.

REST API
--------
GET    /api/health                 — проверка работоспособности
GET    /api/subjects               — мои профили (владелец — этот браузер)
                                     + names: все псевдонимы для подсказок
GET    /api/subjects/<имя>         — профиль: самооценка + оценки окружающих
                                     (только владелец или админ, иначе 403)
POST   /api/responses              — сохранить результат опроса
         { "mode": "self" | "other",
           "subject": "псевдоним того, про кого анкета" (обязательно),
           "rater": "псевдоним того, кто заполнил" (для other обязательно;
                     для self совпадает с subject),
           "answers": [0..4 × 40], "overwrite": true|false }
         Ответ содержит author_id и subject_id — id людей в таблице subjects.
         Для self возвращается полный профиль, для other — только счётчики.
         Самооценку под чужим псевдонимом (владелец — другой браузер)
         перезаписать нельзя: 403.
DELETE /api/subjects/<имя>         — удалить профиль со всеми оценками
                                     (только владелец или админ)
DELETE /api/subjects               — удалить все профили (только админ)
POST   /api/admin/login            — вход в админку {email, password}
POST   /api/admin/logout           — выход
GET    /api/admin/me               — текущая сессия
GET    /api/admin/data             — содержимое базы (нужна сессия)
GET    /api/admin/export.csv       — выгрузка базы в CSV (нужна сессия)
GET    /admin                      — страница админки
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
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

# author_id (responses) и owner_hash (subjects) добавляются миграциями:
# на уже созданной базе CREATE TABLE не меняет существующие таблицы.

VISITOR_COOKIE = "opros_visitor"
VISITOR_COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60  # секунды


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
        migrate_author_id(conn)
        migrate_owner_hash(conn)


def migrate_owner_hash(conn):
    """owner_hash — хэш cookie-токена браузера, из которого заполнена самооценка."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(subjects)")}
    if "owner_hash" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN owner_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS subjects_owner ON subjects(owner_hash)"
    )


def new_visitor_token():
    return secrets.token_urlsafe(32)


def visitor_hash(token):
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_owner(subject, owner_hash):
    return bool(owner_hash) and subject["owner_hash"] == owner_hash


def migrate_author_id(conn):
    """У каждого ответа есть author_id — кто заполнил, и subject_id — про кого."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(responses)")}
    if "author_id" not in cols:
        conn.execute(
            "ALTER TABLE responses ADD COLUMN author_id INTEGER REFERENCES subjects(id)"
        )
    conn.execute(
        """
        UPDATE responses
           SET author_id = subject_id
         WHERE mode = 'self' AND author_id IS NULL
        """
    )
    pending = conn.execute(
        """
        SELECT id, rater FROM responses
         WHERE mode = 'other' AND author_id IS NULL AND trim(rater) != ''
        """
    ).fetchall()
    for row in pending:
        author = ensure_subject(conn, row["rater"])
        conn.execute(
            "UPDATE responses SET author_id = ? WHERE id = ?",
            (author["id"], row["id"]),
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS responses_author ON responses(author_id)"
    )


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
    keys = row.keys()
    author_name = row["author_name"] if "author_name" in keys and row["author_name"] else row["rater"]
    return {
        "id": row["id"],
        "author_id": row["author_id"],
        "author_name": author_name or "",
        "subject_id": row["subject_id"],
        "rater": author_name or row["rater"] or "",
        "answers": json.loads(row["answers"]),
        "scores": {vid: row["s_" + vid] for vid in VECTOR_IDS},
        "ya": row["ya"],
        "my": row["my"],
        "quadrant": row["quadrant"],
        "ts": row["created_at"],
    }


def build_profile(conn, subject):
    rows = conn.execute(
        """
        SELECT r.*, a.name AS author_name
          FROM responses r
          LEFT JOIN subjects a ON a.id = r.author_id
         WHERE r.subject_id = ?
         ORDER BY r.created_at, r.id
        """,
        (subject["id"],),
    ).fetchall()
    profile = {"id": subject["id"], "name": subject["name"], "self": None, "others": []}
    for row in rows:
        item = response_to_dict(row)
        if row["mode"] == "self":
            profile["self"] = item
        else:
            profile["others"].append(item)
    return profile


def subject_summary(conn, subject):
    """Только счётчики, без ответов — это можно показать и не владельцу."""
    row = conn.execute(
        """
        SELECT SUM(CASE WHEN mode = 'self'  THEN 1 ELSE 0 END) AS has_self,
               SUM(CASE WHEN mode = 'other' THEN 1 ELSE 0 END) AS others_count
          FROM responses WHERE subject_id = ?
        """,
        (subject["id"],),
    ).fetchone()
    return {
        "id": subject["id"],
        "name": subject["name"],
        "has_self": bool(row["has_self"]),
        "others_count": row["others_count"] or 0,
    }


def list_names(conn):
    """Псевдонимы всех людей, по которым есть ответы — для подсказок при вводе."""
    rows = conn.execute(
        """
        SELECT s.name FROM subjects s
         WHERE EXISTS (SELECT 1 FROM responses r WHERE r.subject_id = s.id)
         ORDER BY s.name COLLATE NOCASE
        """
    ).fetchall()
    return [r["name"] for r in rows]


def list_subjects(conn, owner_hash):
    """Профили, владелец которых — этот браузер. Без токена список пуст."""
    if not owner_hash:
        return []
    rows = conn.execute(
        """
        SELECT s.id, s.name,
               SUM(CASE WHEN r.mode = 'self'  THEN 1 ELSE 0 END) AS has_self,
               SUM(CASE WHEN r.mode = 'other' THEN 1 ELSE 0 END) AS others_count,
               MAX(r.created_at) AS updated_at
        FROM subjects s
        LEFT JOIN responses r ON r.subject_id = s.id
        WHERE s.owner_hash = ?
        GROUP BY s.id
        HAVING COUNT(r.id) > 0
        ORDER BY s.name COLLATE NOCASE
        """,
        (owner_hash,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "has_self": bool(r["has_self"]),
            "others_count": r["others_count"] or 0,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def save_response(conn, payload, owner_hash):
    mode = payload.get("mode")
    if mode not in ("self", "other"):
        raise ApiError(400, "Поле «mode» должно быть 'self' или 'other'")
    # subject — псевдоним того, про кого анкета. Для «о другом» он обязателен,
    # чтобы все ответы об одном человеке сходились на одном id.
    subject_name = validate_name(payload.get("subject"), "псевдоним")
    answers = validate_answers(payload.get("answers"))
    overwrite = bool(payload.get("overwrite"))
    if mode == "other":
        rater = validate_name(payload.get("rater"), "ваш псевдоним")
    else:
        rater = subject_name

    scores, ya, my, quadrant = compute_scores(answers)

    with conn:  # транзакция
        subject = ensure_subject(conn, subject_name)
        author = subject if mode == "self" else ensure_subject(conn, rater)
        if mode == "self":
            if not owner_hash:
                raise ApiError(400, "В браузере отключены cookie — самооценку сохранить нельзя")
            existing = conn.execute(
                "SELECT id FROM responses WHERE subject_id = ? AND mode = 'self'",
                (subject["id"],),
            ).fetchone()
            # Профиль с владельцем из другого браузера перезаписать нельзя:
            # иначе любой мог бы «забрать» чужой псевдоним и увидеть оценки о нём.
            if subject["owner_hash"] and not is_owner(subject, owner_hash):
                raise ApiError(
                    403,
                    "Псевдоним «%s» уже занят: самооценка под ним заполнена из другого "
                    "браузера. Выберите другой псевдоним." % subject["name"],
                )
            if existing and not overwrite:
                raise ApiError(409, "У этого профиля уже есть самооценка")
            if existing:
                conn.execute("DELETE FROM responses WHERE id = ?", (existing["id"],))
            conn.execute(
                "UPDATE subjects SET owner_hash = ? WHERE id = ?",
                (owner_hash, subject["id"]),
            )
        cur = conn.execute(
            """
            INSERT INTO responses(subject_id, author_id, mode, rater, answers,
                                  s_mysh, s_kozh, s_or, s_zr, s_an, s_ur, s_zv, s_ob,
                                  ya, my, quadrant, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject["id"], author["id"], mode, author["name"], json.dumps(answers),
                scores["mysh"], scores["kozh"], scores["or"], scores["zr"],
                scores["an"], scores["ur"], scores["zv"], scores["ob"],
                ya, my, quadrant, now_ms(),
            ),
        )
        response_id = cur.lastrowid

    # Полный профиль (с чужими ответами) — только владельцу, то есть автору
    # самооценки. Оценившему другого человека возвращаются одни счётчики.
    summary = subject_summary(conn, subject)
    if mode == "self":
        summary.update(build_profile(conn, subject))
    return {
        "ok": True,
        "id": response_id,
        "author_id": author["id"],
        "subject_id": subject["id"],
        "subject": summary,
    }


# ----------------------------------------------------------------------------
# Админка: почта из списка + пароль ADMIN_TOKEN, сессия в подписанной cookie
# ----------------------------------------------------------------------------

ADMIN_COOKIE = "opros_admin"
ADMIN_SESSION_MS = 14 * 24 * 60 * 60 * 1000

CSV_COLUMNS = [
    "response_id", "created_at",
    "author_id", "author", "subject_id", "subject",
    "mode",
    "ya", "my", "quadrant",
    "mysh", "kozh", "or", "zr", "an", "ur", "zv", "ob",
] + [f"q{n:02d}" for n in range(1, N_QUESTIONS + 1)]


def admin_emails():
    raw = os.environ.get("ADMIN_EMAILS", "vleschinskii@gmail.com")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def admin_secret():
    secret = os.environ.get("ADMIN_TOKEN", "")
    if not secret:
        raise ApiError(503, "Админка не настроена: нет ADMIN_TOKEN")
    return secret.encode("utf-8")


def _sign(payload):
    digest = hmac.new(admin_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def check_admin_password(password):
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or not password:
        return False
    # Одинаковая длина дайджеста — сравнение не зависит от длины строк.
    a = hmac.new(b"opros-admin-pw", password.encode("utf-8"), hashlib.sha256).digest()
    b = hmac.new(b"opros-admin-pw", expected.encode("utf-8"), hashlib.sha256).digest()
    return hmac.compare_digest(a, b)


def create_admin_session(email):
    exp = int(time.time() * 1000) + ADMIN_SESSION_MS
    payload = f"{email}|{exp}"
    return f"{payload}|{_sign(payload)}"


def verify_admin_session(token):
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    email, exp_raw, sig = parts
    try:
        good = _sign(f"{email}|{exp_raw}")
    except ApiError:
        return None
    if len(good) != len(sig) or not hmac.compare_digest(good, sig):
        return None
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    if exp < int(time.time() * 1000):
        return None
    if email not in admin_emails():
        return None
    return email


def cookie_header(name, value, max_age, secure):
    path = os.environ.get("OPROS_PUBLIC_PREFIX", "").strip() or "/"
    parts = [
        f'{name}="{value}"',
        f"Path={path}",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def admin_cookie_header(value, max_age, secure):
    return cookie_header(ADMIN_COOKIE, value, max_age, secure)


def iso_utc(ms):
    if ms is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def load_admin_rows(conn):
    """Одна строка на ответ. Профиль без ответов тоже попадает в выгрузку."""
    query = """
        SELECT s.id AS subject_id, s.name AS subject, s.created_at AS subject_created_at,
               r.id AS response_id, r.created_at, r.mode,
               r.author_id, a.name AS author_name, r.rater,
               r.ya, r.my, r.quadrant,
               r.s_mysh, r.s_kozh, r.s_or, r.s_zr, r.s_an, r.s_ur, r.s_zv, r.s_ob,
               r.answers
        FROM subjects s
        JOIN responses r ON r.subject_id = s.id
        LEFT JOIN subjects a ON a.id = r.author_id
        ORDER BY s.name COLLATE NOCASE, r.created_at, r.id
    """
    rows = []
    for rec in conn.execute(query):
        answers = json.loads(rec["answers"]) if rec["answers"] else []
        rows.append({
            "response_id": rec["response_id"],
            "created_at": iso_utc(rec["created_at"] or rec["subject_created_at"]),
            "ts": rec["created_at"] or rec["subject_created_at"],
            "subject_id": rec["subject_id"],
            "subject": rec["subject"],
            "author_id": rec["author_id"],
            "author": rec["author_name"] or rec["rater"] or "",
            "mode": rec["mode"] or "",
            "rater": rec["author_name"] or rec["rater"] or "",
            "ya": rec["ya"],
            "my": rec["my"],
            "quadrant": rec["quadrant"] or "",
            "scores": {vid: rec["s_" + vid] for vid in VECTOR_IDS} if rec["response_id"] else None,
            "answers": answers,
        })
    return rows


def rows_to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        scores = row["scores"] or {}
        item = {
            "response_id": row["response_id"] if row["response_id"] is not None else "",
            "created_at": row["created_at"],
            "author_id": "" if row.get("author_id") is None else row["author_id"],
            "author": row.get("author") or "",
            "subject_id": "" if row.get("subject_id") is None else row["subject_id"],
            "subject": row["subject"],
            "mode": row["mode"],
            "ya": "" if row["ya"] is None else row["ya"],
            "my": "" if row["my"] is None else row["my"],
            "quadrant": row["quadrant"],
        }
        for vid in VECTOR_IDS:
            item[vid] = "" if row["scores"] is None else scores.get(vid, "")
        for n in range(N_QUESTIONS):
            answers = row["answers"]
            item[f"q{n + 1:02d}"] = answers[n] if n < len(answers) else ""
        writer.writerow(item)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


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
        self._send_pending_cookies()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self._send_pending_cookies()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_pending_cookies(self):
        for cookie in getattr(self, "_pending_cookies", ()):
            self.send_header("Set-Cookie", cookie)
        self._pending_cookies = []

    # --- идентификация посетителя по cookie браузера ---------------------

    def _visitor_token(self):
        """Токен браузера. Если cookie ещё нет — выдаём новую вместе с ответом."""
        cached = getattr(self, "_visitor_cache", None)
        if cached:
            return cached
        token = self._cookies().get(VISITOR_COOKIE, "")
        if not (token and 20 <= len(token) <= 128
                and all(c.isalnum() or c in "-_" for c in token)):
            token = new_visitor_token()
            self._pending_cookies = getattr(self, "_pending_cookies", []) + [
                cookie_header(VISITOR_COOKIE, token, VISITOR_COOKIE_MAX_AGE, self._secure_cookie())
            ]
        self._visitor_cache = token
        return token

    def _owner_hash(self):
        return visitor_hash(self._visitor_token())

    def _can_view(self, subject):
        return is_owner(subject, self._owner_hash()) or bool(self._admin_email())

    def _cookies(self):
        found = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            found[key.strip()] = value.strip().strip('"')
        return found

    def _admin_email(self):
        return verify_admin_session(self._cookies().get(ADMIN_COOKIE))

    def _require_admin(self):
        email = self._admin_email()
        if not email:
            raise ApiError(401, "Нужен вход")
        return email

    def _secure_cookie(self):
        return (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"

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
        # Одно соединение keep-alive обслуживает несколько запросов подряд.
        self._visitor_cache = None
        self._pending_cookies = []
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
        if path in ("/", "/index.html", "/admin", "/admin/"):
            self._visitor_token()  # выдать cookie при первом заходе
            self._send_file("index.html", "text/html; charset=utf-8")
            return True
        if path == "/api/admin/me":
            email = self._require_admin()
            self._send_json({"email": email})
            return True
        if path == "/api/admin/data":
            self._require_admin()
            with connect() as conn:
                self._send_json({"rows": load_admin_rows(conn)})
            return True
        if path == "/api/admin/export.csv":
            self._require_admin()
            with connect() as conn:
                body = rows_to_csv(load_admin_rows(conn))
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="opros.csv"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return True
        if path == "/api/health":
            self._send_json({"ok": True, "db": DB_PATH})
            return True
        if path == "/api/subjects":
            owner_hash = self._owner_hash()
            with connect() as conn:
                self._send_json({
                    "subjects": list_subjects(conn, owner_hash),
                    "names": list_names(conn),
                })
            return True
        name = self._subject_name_from_path(path)
        if name is not None:
            with connect() as conn:
                subject = find_subject(conn, name)
                if not subject:
                    raise ApiError(404, "Профиль не найден")
                if not self._can_view(subject):
                    raise ApiError(
                        403,
                        "Профиль виден только тому, кто заполнил «Я о себе» "
                        "под этим псевдонимом, и только из его браузера",
                    )
                self._send_json(build_profile(conn, subject))
            return True
        return False

    def _handle_HEAD(self, path):
        return self._handle_GET(path)

    def _handle_POST(self, path):
        if path == "/api/admin/login":
            payload = self._read_json()
            email = str(payload.get("email") or "").strip().lower()
            password = str(payload.get("password") or "")
            if not os.environ.get("ADMIN_TOKEN"):
                raise ApiError(503, "Админка не настроена: нет ADMIN_TOKEN")
            if email not in admin_emails() or not check_admin_password(password):
                raise ApiError(401, "Неверная почта или пароль")
            cookie = admin_cookie_header(
                create_admin_session(email),
                max_age=ADMIN_SESSION_MS // 1000,
                secure=self._secure_cookie(),
            )
            self._send_json({"ok": True, "email": email}, extra_headers=[("Set-Cookie", cookie)])
            return True
        if path == "/api/admin/logout":
            cookie = admin_cookie_header("", max_age=0, secure=self._secure_cookie())
            self._send_json({"ok": True}, extra_headers=[("Set-Cookie", cookie)])
            return True
        if path == "/api/responses":
            payload = self._read_json()
            owner_hash = self._owner_hash()
            with connect() as conn:
                result = save_response(conn, payload, owner_hash)
            self._send_json(result, 201)
            return True
        return False

    def _handle_DELETE(self, path):
        if path == "/api/subjects":
            self._require_admin()
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
                if not self._can_view(subject):
                    raise ApiError(403, "Удалить профиль может только его владелец")
                # Ответы этого человека о других остаются, но без ссылки на удалённый id.
                conn.execute(
                    "UPDATE responses SET author_id = NULL WHERE author_id = ? AND subject_id != ?",
                    (subject["id"], subject["id"]),
                )
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
