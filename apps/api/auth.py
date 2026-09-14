"""本地账户认证：PBKDF2 密码派生与可撤销随机登录令牌。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from domain.clock import ensure_utc, utcnow
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_id
from repositories.models import AuthSession, UserAccount

PBKDF2_ITERATIONS = 310_000
SESSION_DAYS = 7


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.b64decode(salt), int(rounds)
        )
        return hmac.compare_digest(actual, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_payload(user: UserAccount) -> dict[str, str]:
    return {"id": user.id, "employee_id": user.employee_id, "username": user.username, "role": user.role}


class AuthService:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def register(self, *, employee_id: str, username: str, password: str) -> dict[str, object]:
        employee_id = employee_id.strip()
        username = username.strip()
        with self.factory() as session:
            user = UserAccount(
                id=new_id("user"),
                employee_id=employee_id,
                username=username,
                password_hash=_password_hash(password),
                role="developer",
                active=True,
            )
            session.add(user)
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                raise CodePilotError(ErrorCode.CONFLICT, "工号或用户名已被使用，请换一个。") from exc
            result = self._create_session(session, user)
            session.commit()
            return result

    def login(self, *, account: str, password: str) -> dict[str, object]:
        with self.factory() as session:
            user = session.execute(
                select(UserAccount).where(
                    or_(UserAccount.employee_id == account.strip(), UserAccount.username == account.strip())
                )
            ).scalar_one_or_none()
            if user is None or not user.active or not _password_matches(password, user.password_hash):
                raise CodePilotError(ErrorCode.UNAUTHORIZED, "用户名、工号或密码不正确。")
            result = self._create_session(session, user)
            session.commit()
            return result

    def authenticate(self, token: str) -> UserAccount:
        with self.factory() as session:
            row = session.execute(
                select(AuthSession, UserAccount)
                .join(UserAccount, UserAccount.id == AuthSession.user_id)
                .where(AuthSession.token_hash == _token_hash(token), AuthSession.revoked.is_(False))
            ).one_or_none()
            if row is None:
                raise CodePilotError(ErrorCode.UNAUTHORIZED)
            auth_session, user = row
            if ensure_utc(auth_session.expires_at) <= utcnow() or not user.active:
                raise CodePilotError(ErrorCode.UNAUTHORIZED)
            session.expunge(user)
            return user

    def logout(self, token: str) -> None:
        with self.factory() as session:
            auth_session = session.execute(
                select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
            ).scalar_one_or_none()
            if auth_session is not None:
                auth_session.revoked = True
                session.commit()

    def _create_session(self, session: Session, user: UserAccount) -> dict[str, object]:
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(days=SESSION_DAYS)
        session.add(
            AuthSession(
                id=new_id("session"),
                user_id=user.id,
                token_hash=_token_hash(token),
                expires_at=expires_at,
                revoked=False,
            )
        )
        return {"token": token, "expires_at": expires_at, "user": user_payload(user)}


__all__ = ["AuthService", "user_payload"]
