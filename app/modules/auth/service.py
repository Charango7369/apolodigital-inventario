"""
Service — módulo auth

Lógica de negocio para autenticación:
- Hash de contraseñas (bcrypt)
- Generación/validación de JWT
- CRUD de usuarios
"""

from datetime import datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.modules.auth.models import User, UserRole
from app.modules.auth.schemas import UserCreate

settings = get_settings()

# ---------------------------------------------------------------------------
# Configuración de seguridad
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT config — usa variables de entorno en producción
SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes


# ---------------------------------------------------------------------------
# Funciones de hash
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Genera hash bcrypt de la contraseña."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica contraseña contra su hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ---------------------------------------------------------------------------
# Funciones JWT
# ---------------------------------------------------------------------------
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Genera JWT con los datos proporcionados.
    
    El token incluye:
    - sub: user_id
    - negocio_id: para multi-tenant
    - rol: para permisos
    - exp: expiración
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """
    Decodifica y valida un JWT.
    Retorna el payload si es válido, None si no.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# CRUD de usuarios
# ---------------------------------------------------------------------------
def get_user_by_id(db: Session, user_id: str) -> User | None:
    """Busca usuario por ID."""
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_email(db: Session, email: str, negocio_id: str) -> User | None:
    """
    Busca usuario por email DENTRO de un negocio.
    Recuerda: email es único por negocio, no global.
    """
    return db.query(User).filter(
        and_(User.email == email, User.negocio_id == negocio_id)
    ).first()


def get_user_by_email_any_negocio(db: Session, email: str) -> User | None:
    """
    Busca usuario por email en cualquier negocio.
    Usado para login cuando no sabemos el negocio.
    NOTA: Si hay emails duplicados en distintos negocios, retorna el primero.
    """
    return db.query(User).filter(User.email == email).first()


def get_users_by_negocio(db: Session, negocio_id: str, skip: int = 0, limit: int = 100) -> list[User]:
    """Lista usuarios de un negocio."""
    return db.query(User).filter(
        User.negocio_id == negocio_id
    ).offset(skip).limit(limit).all()


def create_user(db: Session, user_data: UserCreate, negocio_id: str) -> User:
    """
    Crea nuevo usuario.
    Asume que ya verificaste que el email no existe en ese negocio.
    El negocio_id se pasa como parametro (viene del admin que crea el usuario).
    """
    user = User(
        email=user_data.email,
        password_hash=hash_password(user_data.password),
        nombre=user_data.nombre,
        negocio_id=negocio_id,
        rol=user_data.rol.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """
    Autentica usuario por email y contraseña.
    Retorna el usuario si es válido, None si no.
    """
    user = get_user_by_email_any_negocio(db, email)
    if not user:
        return None
    if not user.activo:
        return None
    if not verify_password(password, user.password_hash):
        return None
    
    # Actualizar último login
    user.ultimo_login = datetime.utcnow()
    db.commit()
    
    return user


def update_user_password(db: Session, user: User, new_password: str) -> User:
    """Actualiza contraseña de usuario."""
    user.password_hash = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


def deactivate_user(db: Session, user: User) -> User:
    """Desactiva usuario (soft delete)."""
    user.activo = False
    db.commit()
    db.refresh(user)
    return user
