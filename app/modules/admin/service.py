"""Service layer para gestión de negocios (multi-tenant)."""
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.modules.auth.models import User, UserRole
from app.modules.auth.service import hash_password
from app.modules.inventario.models import Negocio, Producto

from .schemas import NegocioCreate, NegocioUpdate


def list_negocios(db: Session, solo_activos: bool = False) -> list[dict]:
    """
    Lista todos los negocios con conteos de usuarios y productos.

    Solo para SUPERADMIN.
    """
    query = db.query(
        Negocio,
        func.count(User.id.distinct()).label("num_usuarios"),
        func.count(Producto.id.distinct()).label("num_productos"),
    ).outerjoin(
        User, User.negocio_id == Negocio.id
    ).outerjoin(
        Producto, Producto.negocio_id == Negocio.id
    ).group_by(Negocio.id)

    if solo_activos:
        query = query.filter(Negocio.activo == True)

    resultados = query.order_by(Negocio.created_at.desc()).all()

    items = []
    for negocio, num_u, num_p in resultados:
        items.append({
            "id": negocio.id,
            "nombre": negocio.nombre,
            "propietario": negocio.propietario,
            "telefono": negocio.telefono,
            "moneda": negocio.moneda,
            "activo": negocio.activo,
            "created_at": negocio.created_at,
            "num_usuarios": num_u,
            "num_productos": num_p,
        })
    return items


def get_negocio(db: Session, negocio_id: str) -> Negocio | None:
    return db.query(Negocio).filter(Negocio.id == negocio_id).first()


def create_negocio_con_admin(db: Session, data: NegocioCreate) -> dict:
    """
    Crea un negocio nuevo y su primer usuario administrador
    en una sola transacción atómica.

    Falla si el email del admin ya existe en cualquier negocio.
    """
    # Validar email único globalmente (un mismo email no puede ser admin
    # de dos negocios simultáneamente con el diseño actual)
    existing = db.query(User).filter(User.email == data.admin_email).first()
    if existing:
        raise ValueError(
            f"El email '{data.admin_email}' ya está registrado en otro negocio"
        )

    # Crear el negocio
    negocio = Negocio(
        nombre=data.nombre,
        propietario=data.propietario,
        telefono=data.telefono,
        moneda=data.moneda,
    )
    db.add(negocio)
    db.flush()  # obtener el id sin commit aún

    # Crear el admin del negocio
    admin = User(
        email=data.admin_email,
        password_hash=hash_password(data.admin_password),
        nombre=data.admin_nombre,
        negocio_id=negocio.id,
        rol=UserRole.ADMIN.value,
    )
    db.add(admin)

    db.commit()
    db.refresh(negocio)
    db.refresh(admin)

    return {
        "negocio": negocio,
        "admin_id": admin.id,
        "admin_email": admin.email,
        "admin_nombre": admin.nombre,
    }


def update_negocio(db: Session, negocio: Negocio, data: NegocioUpdate) -> Negocio:
    """Actualiza datos del negocio. Útil para suspender con activo=False."""
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(negocio, key, value)
    db.commit()
    db.refresh(negocio)
    return negocio


def toggle_negocio_activo(db: Session, negocio: Negocio) -> Negocio:
    """Suspende o reactiva un negocio. Efecto: sus usuarios no pueden loguearse."""
    negocio.activo = not negocio.activo
    db.commit()
    db.refresh(negocio)
    return negocio
