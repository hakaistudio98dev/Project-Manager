import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Employee, Project, ProjectMember, Role, User

bearer = HTTPBearer(auto_error=False)

# Grup role untuk RBAC (least privilege)
FINANCE_VIEW = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE)      # boleh lihat gaji / rate detail
PROJECT_MANAGE = (Role.ADMIN, Role.MANAGEMENT, Role.PM)
APPROVERS_BUDGET = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE)
APPROVERS_EXPENSE = (Role.ADMIN, Role.MANAGEMENT, Role.FINANCE)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    it = settings.PASSWORD_ITERATIONS
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, it)
    return f"pbkdf2_sha256${it}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        _, it, salt, dk = hashed.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def create_token(user: User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user.id), "org": user.org_id, "role": user.role.value, "exp": exp}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                     db: Session = Depends(get_db)) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = jwt.decode(cred.credentials, settings.JWT_SECRET,
                             algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")
    return user


def require_roles(*roles: Role):
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user
    return dep


def employee_of(db: Session, user: User) -> Employee | None:
    return db.scalar(select(Employee).where(Employee.user_id == user.id))


def load_project(db: Session, user: User, project_id: int, *, manage: bool = False) -> Project:
    """Cek akses project: admin/management semua; PM hanya project miliknya;
    finance/sales read-only; staff hanya project tempat ia menjadi member."""
    p = db.get(Project, project_id)
    if not p or p.org_id != user.org_id or p.is_deleted:
        raise HTTPException(404, "Project not found")
    if user.role in (Role.ADMIN, Role.MANAGEMENT):
        return p
    if user.role == Role.PM:
        if p.pm_id != user.id:
            raise HTTPException(403, "Not the PM of this project")
        return p
    if manage:
        raise HTTPException(403, "Cannot manage this project")
    if user.role in (Role.FINANCE, Role.SALES):
        return p
    emp = employee_of(db, user)
    if not emp or not db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == p.id, ProjectMember.employee_id == emp.id)):
        raise HTTPException(403, "Not a member of this project")
    return p
