"""
Service layer — módulo ventas

Cambios v2 (lotes):
- _completar_venta: el descuento usa FEFO automático (sin cambios desde el caller).
- cancelar_venta: ahora REVIERTE cada SALIDA_VENTA al lote original,
  preservando integridad de costeo. Antes hacía un AJUSTE_POSITIVO genérico.
"""


from datetime import datetime, time, date, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from sqlalchemy import func, and_, select
from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import Cliente, Venta, DetalleVenta, EstadoVenta, MetodoPago
from app.modules.ventas.schemas import (
    ClienteCreate, ClienteUpdate,
    VentaCreate, VentaUpdate, VentaCompletarRequest,
    DetalleVentaCreate,
)
from app.modules.inventario.models import (
    Variante, Producto, Stock, Almacen, MovimientoStock,
)
from app.modules.inventario.service import (
    get_almacen_principal, get_or_create_stock, crear_movimiento,
)
from app.modules.inventario.schemas import MovimientoCreate


ZONA_LOCAL = ZoneInfo("America/La_Paz")

def obtener_limites_utc(fecha_local: date):
    """
    Toma un día de calendario y devuelve los milisegundos exactos
    de inicio y fin en UTC, listos para la base de datos ingenua de SQLAlchemy.
    """
    inicio_local = datetime.combine(fecha_local, time.min, tzinfo=ZONA_LOCAL)
    fin_local = datetime.combine(fecha_local, time.max, tzinfo=ZONA_LOCAL)
    
    inicio_utc = inicio_local.astimezone(timezone.utc).replace(tzinfo=None)
    fin_utc = fin_local.astimezone(timezone.utc).replace(tzinfo=None)
    
    return inicio_utc, fin_utc
# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------
def get_clientes(
    db: Session,
    negocio_id: str,
    solo_activos: bool = True,
    busqueda: str | None = None
) -> list[Cliente]:
    query = db.query(Cliente).filter(Cliente.negocio_id == negocio_id)
    if solo_activos:
        query = query.filter(Cliente.activo == True)
    if busqueda:
        term = f"%{busqueda}%"
        query = query.filter(
            (Cliente.nombre.ilike(term)) |
            (Cliente.telefono.ilike(term)) |
            (Cliente.nit.ilike(term))
        )
    return query.order_by(Cliente.nombre).all()


def get_cliente(db: Session, negocio_id: str, cliente_id: str) -> Cliente | None:
    return db.query(Cliente).filter(
        Cliente.id == cliente_id,
        Cliente.negocio_id == negocio_id
    ).first()


def get_cliente_by_telefono(db: Session, negocio_id: str, telefono: str) -> Cliente | None:
    return db.query(Cliente).filter(
        Cliente.telefono == telefono,
        Cliente.negocio_id == negocio_id
    ).first()


def create_cliente(db: Session, negocio_id: str, data: ClienteCreate) -> Cliente:
    cliente = Cliente(negocio_id=negocio_id, **data.model_dump())
    db.add(cliente)
    db.commit()
    db.refresh(cliente)
    return cliente


def update_cliente(db: Session, cliente: Cliente, data: ClienteUpdate) -> Cliente:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(cliente, key, value)
    db.commit()
    db.refresh(cliente)
    return cliente


# ---------------------------------------------------------------------------
# Número de venta
# ---------------------------------------------------------------------------
def get_siguiente_numero_venta(db: Session, negocio_id: str) -> int:
    result = db.query(func.max(Venta.numero)).filter(
        Venta.negocio_id == negocio_id
    ).scalar()
    return (result or 0) + 1


# ---------------------------------------------------------------------------
# Ventas
# ---------------------------------------------------------------------------
def get_ventas(
    db: Session,
    negocio_id: str,
    estado: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    cliente_id: str | None = None,
    page: int = 1,
    per_page: int = 50
) -> tuple[list[Venta], int]:
    query = db.query(Venta).filter(Venta.negocio_id == negocio_id)

    if estado:
        query = query.filter(Venta.estado == estado)
    if fecha_desde:
        inicio_utc, _ = obtener_limites_utc(fecha_desde)
        query = query.filter(Venta.created_at >= inicio_utc)
    if fecha_hasta:
        _, fin_utc = obtener_limites_utc(fecha_hasta)
        query = query.filter(Venta.created_at <= fin_utc)
    if cliente_id:
        query = query.filter(Venta.cliente_id == cliente_id)

    total = query.count()
    ventas = query.options(
        joinedload(Venta.detalles)
    ).order_by(Venta.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    return ventas, total


def get_venta(db: Session, negocio_id: str, venta_id: str) -> Venta | None:
    return db.query(Venta).options(
        joinedload(Venta.detalles),
        joinedload(Venta.cliente)
    ).filter(
        Venta.id == venta_id,
        Venta.negocio_id == negocio_id
    ).first()


def get_venta_by_numero(db: Session, negocio_id: str, numero: int) -> Venta | None:
    return db.query(Venta).options(
        joinedload(Venta.detalles)
    ).filter(
        Venta.numero == numero,
        Venta.negocio_id == negocio_id
    ).first()


def crear_venta(
    db: Session,
    negocio_id: str,
    data: VentaCreate,
    usuario_id: str
) -> Venta:
    """Crea una venta con todos sus detalles."""
    if data.almacen_id:
        almacen = db.query(Almacen).filter(
            Almacen.id == data.almacen_id,
            Almacen.negocio_id == negocio_id
        ).first()
        if not almacen:
            raise ValueError("Almacén no encontrado")
    else:
        almacen = get_almacen_principal(db, negocio_id)
        if not almacen:
            raise ValueError("No hay almacén principal configurado")

    numero = get_siguiente_numero_venta(db, negocio_id)
    venta = Venta(
        negocio_id=negocio_id,
        almacen_id=almacen.id,
        numero=numero,
        cliente_id=data.cliente_id,
        cliente_nombre=data.cliente_nombre,
        metodo_pago=data.metodo_pago,
        monto_recibido=data.monto_recibido,
        notas=data.notas,
        usuario_id=usuario_id,
        estado=EstadoVenta.PENDIENTE.value
    )
    db.add(venta)
    db.flush()

    subtotal = Decimal("0")
    for linea in data.detalles:
        detalle = _crear_detalle(db, negocio_id, venta.id, linea)
        subtotal += detalle.subtotal

    venta.subtotal = subtotal
    venta.descuento = data.descuento
    venta.total = subtotal - data.descuento

    if data.monto_recibido and data.monto_recibido >= venta.total:
        venta.cambio = data.monto_recibido - venta.total

    if data.completar:
        _completar_venta(db, negocio_id, venta, usuario_id)

    db.commit()
    db.refresh(venta)
    return venta


def _crear_detalle(
    db: Session,
    negocio_id: str,
    venta_id: str,
    data: DetalleVentaCreate
) -> DetalleVenta:
    """Crea una línea de detalle de venta"""
    variante = db.query(Variante).join(Producto).filter(
        Variante.id == data.variante_id,
        Producto.negocio_id == negocio_id,
        Variante.activa == True,
        Producto.activo == True
    ).first()

    if not variante:
        raise ValueError(f"Variante {data.variante_id} no encontrada o inactiva")

    precio = data.precio_unitario if data.precio_unitario else variante.precio_venta
    subtotal = (precio * data.cantidad) - data.descuento_linea

    detalle = DetalleVenta(
        venta_id=venta_id,
        variante_id=variante.id,
        producto_nombre=variante.producto.nombre,
        variante_sku=variante.sku,
        cantidad=data.cantidad,
        precio_unitario=precio,
        descuento_linea=data.descuento_linea,
        subtotal=subtotal,
        costo_unitario=variante.precio_costo
    )
    db.add(detalle)
    db.flush()
    return detalle


def _completar_venta(db: Session, negocio_id: str, venta: Venta, usuario_id: str) -> None:
    """
    Completa una venta: cambia estado y descuenta stock con FEFO automático.

    El descuento usa el service de inventario que aplica FEFO sobre los lotes.
    Una sola línea de venta puede generar N movimientos si el FEFO reparte
    entre varios lotes.
    """
    

    if venta.estado != EstadoVenta.PENDIENTE.value:
        raise ValueError(
            f"Solo se pueden completar ventas pendientes. Estado actual: {venta.estado}"
        )

    for detalle in venta.detalles:
        # Resolver variante para chequeo de servicios
        variante = db.query(Variante).options(
            joinedload(Variante.producto)
        ).filter(Variante.id == detalle.variante_id).first()

        if variante and variante.producto.es_servicio:
            continue  # Servicios no descuentan stock

        # Pre-chequeo rápido contra el caché Stock (mensaje claro al usuario).
        stock = get_or_create_stock(db, detalle.variante_id, venta.almacen_id)
        if stock.cantidad_actual < detalle.cantidad:
            raise ValueError(
                f"Stock insuficiente para {detalle.producto_nombre}. "
                f"Disponible: {stock.cantidad_actual}, Solicitado: {detalle.cantidad}"
            )
        # 1. DESCUENTO EXPLÍCITO DEL STOCK GENERAL EN LA SESIÓN
        stock.cantidad_actual -= detalle.cantidad    

        # ⬅️ AQUÍ ESTÁ LA MAGIA REPARADA
        mov_data = MovimientoCreate(
            variante_id=detalle.variante_id,
            almacen_id=venta.almacen_id,
            tipo="SALIDA_VENTA",
            cantidad=detalle.cantidad,
            referencia_id=str(venta.id), # EL ENLACE VITAL (UUID, no el número)
            motivo=f"Venta #{venta.numero}",
        )
        
        crear_movimiento(db, negocio_id, mov_data, usuario_id)

    venta.estado = EstadoVenta.COMPLETADA.value
    # Genera un datetime consciente de su zona horaria (UTC explícito)
    venta.completed_at = datetime.now(timezone.utc)

def completar_venta(
    db: Session,
    negocio_id: str,
    venta: Venta,
    data: VentaCompletarRequest,
    usuario_id: str
) -> Venta:
    """Completa una venta pendiente"""
    if data.metodo_pago:
        venta.metodo_pago = data.metodo_pago
    if data.monto_recibido:
        venta.monto_recibido = data.monto_recibido
        if data.monto_recibido >= venta.total:
            venta.cambio = data.monto_recibido - venta.total

    _completar_venta(db, negocio_id, venta, usuario_id)
    db.commit()
    db.refresh(venta)
    return venta


def cancelar_venta(
    db: Session,
    negocio_id: str,
    venta: Venta,
    usuario_id: str,
    motivo: str | None = None
) -> Venta:
    """
    Cancela una venta.

    Si la venta estaba COMPLETADA, devuelve cada salida al lote original.
    Esto preserva la integridad del costeo (cada unidad vuelve a su lote).

    Las ventas que se completaron antes de la migración a lotes pueden tener
    movimientos sin lote_id. En ese caso, raise para forzar reversión manual
    (caso poco común; típicamente requeriría un AJUSTE_POSITIVO con
    creación de un lote nuevo).
    """
    if venta.estado == EstadoVenta.CANCELADA.value:
        raise ValueError("La venta ya está cancelada")

    if venta.estado == EstadoVenta.COMPLETADA.value:
        # Buscar TODAS las salidas originales de esta venta.
        # Una sola venta puede haber generado N movimientos por FEFO.
        movs_originales = db.query(MovimientoStock).filter(
            MovimientoStock.referencia_id == str(venta.id),
            MovimientoStock.tipo == "SALIDA_VENTA",
            MovimientoStock.almacen_id == venta.almacen_id,
        ).all()

        if not movs_originales:
            # Venta de servicios puros, o venta sin descuento de stock previo.
            # No hay nada que devolver.
            pass

        for mov in movs_originales:
            if not mov.lote_id:
                raise ValueError(
                    f"El movimiento {mov.id} no tiene lote asociado (legacy "
                    f"pre-migración). La cancelación requiere reversión manual: "
                    f"crear un AJUSTE_POSITIVO con un lote nuevo de igual cantidad."
                )

            cantidad_devolver = -mov.cantidad  # mov.cantidad es negativa
            
            # REVERTIR EN EL STOCK GENERAL
            stock_general = get_or_create_stock(db, mov.variante_id, mov.almacen_id)
            stock_general.cantidad_actual += cantidad_devolver

            mov_data = MovimientoCreate(
                variante_id=mov.variante_id,
                almacen_id=mov.almacen_id,
                tipo="DEVOLUCION_CLIENTE",
                cantidad=cantidad_devolver,
                lote_id=mov.lote_id,  # ← devolver al lote ORIGINAL
                costo_unitario=mov.costo_unitario,  # ← preservar costo historico inmutable
                referencia_id=str(venta.id),
                motivo=(
                    f"Cancelación venta #{venta.numero}"
                    + (f": {motivo}" if motivo else "")
                ),
            )
            crear_movimiento(db, negocio_id, mov_data, usuario_id)

    venta.estado = EstadoVenta.CANCELADA.value
    # Por esto (consistente con el resto del módulo):
    venta.cancelled_at = datetime.now(timezone.utc)
    if motivo:
        venta.notas = (venta.notas or "") + f"\n[CANCELADA] {motivo}"

    db.commit()
    db.refresh(venta)
    return venta


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------
def get_resumen_ventas_dia(db: Session, negocio_id: str, fecha: date) -> dict:
    inicio, fin = obtener_limites_utc(fecha)
    
    ventas = db.query(Venta).filter(
        Venta.negocio_id == negocio_id,
        Venta.estado == EstadoVenta.COMPLETADA.value,
        Venta.created_at >= inicio,
        Venta.created_at <= fin
    ).all()

    total_ventas = len(ventas)
    monto_total = sum(v.total for v in ventas)
    ticket_promedio = monto_total / total_ventas if total_ventas > 0 else Decimal("0")

    por_metodo = {}
    for v in ventas:
        metodo = v.metodo_pago
        por_metodo[metodo] = por_metodo.get(metodo, Decimal("0")) + v.total

    return {
        "fecha": fecha.isoformat(),
        "total_ventas": total_ventas,
        "monto_total": monto_total,
        "ticket_promedio": ticket_promedio,
        "por_metodo_pago": por_metodo
    }


def get_resumen_caja(db: Session, negocio_id: str, fecha: date) -> dict:
    inicio, fin = obtener_limites_utc(fecha)
    
    ventas = db.query(Venta).filter(
        Venta.negocio_id == negocio_id,
        Venta.created_at >= inicio,
        Venta.created_at <= fin
    ).all()

    completadas = [v for v in ventas if v.estado == EstadoVenta.COMPLETADA.value]
    canceladas = [v for v in ventas if v.estado == EstadoVenta.CANCELADA.value]

    totales = {
        MetodoPago.EFECTIVO.value: Decimal("0"),
        MetodoPago.QR.value: Decimal("0"),
        MetodoPago.TARJETA.value: Decimal("0"),
        MetodoPago.TRANSFERENCIA.value: Decimal("0"),
        MetodoPago.CREDITO.value: Decimal("0"),
    }

    for v in completadas:
        if v.metodo_pago in totales:
            totales[v.metodo_pago] += v.total

    return {
        "fecha": fecha.isoformat(),
        "ventas_completadas": len(completadas),
        "ventas_canceladas": len(canceladas),
        "total_efectivo": totales[MetodoPago.EFECTIVO.value],
        "total_qr": totales[MetodoPago.QR.value],
        "total_tarjeta": totales[MetodoPago.TARJETA.value],
        "total_transferencia": totales[MetodoPago.TRANSFERENCIA.value],
        "total_credito": totales[MetodoPago.CREDITO.value],
        "total_general": sum(totales.values())
    }
def obtener_utilidad_periodo(
    db: Session,
    negocio_id: str,
    fecha_inicio: datetime,
    fecha_fin: datetime
) -> dict:
    """
    Agrega la utilidad de todas las ventas completadas en un rango de fechas.
    Separa el ingreso FEFO (costeo real) del ingreso Legacy (sin costeo).
    """
    stmt = select(Venta.id).where(
        Venta.negocio_id == negocio_id,
        Venta.estado == "COMPLETADA", # O EstadoVenta.COMPLETADA.value
        Venta.created_at >= fecha_inicio,
        Venta.created_at <= fecha_fin
    )
    ventas_ids = db.execute(stmt).scalars().all()

    # Contadores de Alta Precisión
    total_revenue_fefo = Decimal("0.00")
    total_cost_fefo = Decimal("0.00")
    
    total_revenue_legacy = Decimal("0.00")
    ventas_legacy_count = 0
    ventas_fefo_count = 0

    for v_id in ventas_ids:
        # Reutilizamos tu motor existente (asume que se llama así tu función anterior)
        reporte_individual = obtener_reporte_utilidad_venta(db, v_id)
        
        if reporte_individual["legacy"]:
            total_revenue_legacy += Decimal(str(reporte_individual["revenue"]))
            ventas_legacy_count += 1
        else:
            total_revenue_fefo += Decimal(str(reporte_individual["revenue"]))
            total_cost_fefo += Decimal(str(reporte_individual["cost_real"]))
            ventas_fefo_count += 1
            
        # El Garbage Collector de Python destruirá el pesado arreglo "detalle" 
        # en la siguiente iteración, salvando la memoria RAM.

    # Matemáticas de rentabilidad blindadas contra ZeroDivisionError
    profit_fefo = total_revenue_fefo - total_cost_fefo
    margin_fefo = (profit_fefo / total_revenue_fefo * 100) if total_revenue_fefo > 0 else Decimal("0.00")

    return {
        "periodo": {
            "inicio": fecha_inicio.isoformat(),
            "fin": fecha_fin.isoformat()
        },
        "metricas_fefo": {
            "ventas_analizadas": ventas_fefo_count,
            "revenue": round(total_revenue_fefo, 2),
            "cost_real": round(total_cost_fefo, 2),
            "profit": round(profit_fefo, 2),
            "margin_porcentaje": round(margin_fefo, 2)
        },
        "alertas_legacy": {
            "ventas_sin_costeo": ventas_legacy_count,
            "revenue_fantasma": round(total_revenue_legacy, 2),
            "advertencia": "El revenue_fantasma no tiene costos asociados. No asuma el 100% como ganancia."
        },
        "resumen_general": {
            "total_ventas": ventas_fefo_count + ventas_legacy_count,
            "ingreso_bruto_caja": round(total_revenue_fefo + total_revenue_legacy, 2)
        }
    }