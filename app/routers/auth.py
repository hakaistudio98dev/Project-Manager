from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import Organization, Role, User
from ..schemas import BootstrapIn, LoginIn, TokenOut, UserIn, UserOut
from ..security import (create_token, get_current_user, hash_password, require_roles,
                        verify_password)

router = APIRouter(prefix="/api", tags=["Auth & Users"])


@router.post("/auth/bootstrap", response_model=UserOut, status_code=201)
def bootstrap(body: BootstrapIn, db: Session = Depends(get_db)):
    """Setup awal: buat organisasi + admin pertama. Hanya bisa sekali."""
    if db.scalar(select(func.count(Organization.id))):
        raise HTTPException(409, "System already bootstrapped")
    org = Organization(name=body.org_name)
    db.add(org)
    db.flush()
    admin = User(org_id=org.id, email=body.admin_email, full_name=body.admin_name,
                 password_hash=hash_password(body.admin_password), role=Role.ADMIN)
    db.add(admin)
    db.flush()
    audit.log(db, admin, "bootstrap", "organization", org.id, new={"name": org.name})
    db.commit()
    return admin


@router.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email))
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email atau password salah")
    return TokenOut(access_token=create_token(user))


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db),
                actor: User = Depends(require_roles(Role.ADMIN))):
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(409, "Email already registered")
    u = User(org_id=actor.org_id, email=body.email, full_name=body.full_name,
             password_hash=hash_password(body.password), role=body.role)
    db.add(u)
    db.flush()
    audit.log(db, actor, "create", "user", u.id, new={"email": u.email, "role": u.role})
    db.commit()
    return u


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db),
               actor: User = Depends(require_roles(Role.ADMIN, Role.MANAGEMENT, Role.PM))):
    return db.scalars(select(User).where(User.org_id == actor.org_id).order_by(User.id)).all()
