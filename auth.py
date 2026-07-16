from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request, status

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ENGRAMIC_USERS_DB", BASE_DIR / "users.db"))
SESSION_COOKIE = "engramic_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7


@dataclass(frozen=True)
class CurrentUser:
    id: int
    name: str
    email: str
    role: str
    patient_id: str | None


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = _password_hash(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def initialize_auth() -> None:
    with _connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('doctor', 'patient')), patient_id TEXT)""")
        for name, email, password, role, patient_id in [
            ("Dr. Maya Santoso", "doctor@engramic.id", "Doctor123!", "doctor", None),
            ("Pak Budi", "patient@engramic.id", "Patient123!", "patient", "patient_budi"),
        ]:
            db.execute(
                "INSERT OR IGNORE INTO users(name,email,password_hash,role,patient_id) VALUES (?,?,?,?,?)",
                (name, email, _password_hash(password), role, patient_id),
            )


def create_user(name: str, email: str, password: str, role: str, patient_id: str | None) -> CurrentUser:
    try:
        with _connection() as db:
            cursor = db.execute(
                "INSERT INTO users(name,email,password_hash,role,patient_id) VALUES (?,?,?,?,?)",
                (name.strip(), email.strip().lower(), _password_hash(password), role, patient_id),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("An account with this email already exists") from exc
    return CurrentUser(user_id, name.strip(), email.strip().lower(), role, patient_id)


def authenticate(email: str, password: str) -> CurrentUser | None:
    with _connection() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row or not _password_matches(password, row["password_hash"]):
        return None
    return CurrentUser(row["id"], row["name"], row["email"], row["role"], row["patient_id"])


def _session_secret() -> bytes:
    configured = os.environ.get("ENGRAMIC_SESSION_SECRET")
    return configured.encode() if configured else hashlib.sha256(f"engramic:{DB_PATH.resolve()}".encode()).digest()


def create_session(user: CurrentUser) -> str:
    payload = {"uid": user.id, "exp": int(time.time()) + SESSION_TTL_SECONDS}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(_session_secret(), raw, hashlib.sha256).digest()
    return f"{raw.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _decode_session(token: str) -> int | None:
    try:
        raw_text, signature_text = token.split(".", 1)
        raw = raw_text.encode()
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        if not hmac.compare_digest(signature, hmac.new(_session_secret(), raw, hashlib.sha256).digest()):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw_text + "=" * (-len(raw_text) % 4)))
        return int(payload["uid"]) if int(payload["exp"]) >= int(time.time()) else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def get_current_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = _decode_session(token) if token else None
    if user_id is None:
        return None
    with _connection() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return CurrentUser(row["id"], row["name"], row["email"], row["role"], row["patient_id"]) if row else None


def require_role(request: Request, role: str) -> CurrentUser:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    if user.role != role:
        destination = "/doctor/dashboard" if user.role == "doctor" else "/patient/dashboard"
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": destination})
    return user


def authorize_patient_access(request: Request, patient_id: str) -> CurrentUser:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if user.role == "doctor" or (user.role == "patient" and user.patient_id == patient_id):
        return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot access this patient record")
