"""
Router FastAPI — módulo ventas y gestión financiera (Bloque 2.5).

Endpoints para:
1. Clientes (CRUD y búsquedas telefónicas).
2. Ventas POS con creación financiera multi-pago e idempotencia (`POST /ventas`).
3. Cancelación integral de ventas con reingreso de existencias y reversiones (`POST /ventas/{id}/cancelar`).
4. Auditoría de cancelaciones (`GET /ventas/{id}/auditoria-cancelacion`).
5. Gestión de pagos: registro, confirmación transaccional, marcación de fallidos, reversiones compensatorias y conciliaciones.
6. Gestión de cartera: listado con cálculo dinámico de mora, agregación por cliente, abonos e historiales de estado de cuenta.
7. Reportes operativos de ventas y gaveta de caja.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.modules.auth.models import User, UserRole

from app.modules.ventas import service
from app.modules.ventas.models import (
    Cliente, Venta, PagoVenta, CuentaPorCobrar, EstadoVenta, EstadoCuentaCobrar,
)
from app.modules.ventas.schemas import (
    # Clientes
    ClienteCreate, ClienteUpdate, ClienteResponse,
    # Ventas
    VentaCreate, VentaUpdate, VentaCompletarRequest, VentaResponse, VentaListResponse,
    VentaCancelacionRequest, VentaCancelacionResponse,
    # Pagos
    PagoCreate, PagoResponse, PagoConfirmarRequest, PagoMarcarFallidoRequest,
    ReversionPagoCreate, ReversionPagoResponse,
    ConciliacionPagoCreate, ConciliacionPagoResponse,
    HistorialPagosVentaResponse,
    # Cartera
    AbonoCreate, AbonoResponse, CuentaPorCobrarDetalleResponse,
    CarteraClienteItemResponse, EstadoCuentaClienteResponse,
    # Reportes
    ResumenVentasDia, ResumenCaja,
)
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.ventas.services.accounts_receivable_service import AccountsReceivableService
from app.modules.ventas.services.sale_cancellation_service import SaleCancellationService
from app.modules.ventas.idempotency import ejecutar_con_idempotencia

router = APIRouter()


# ===========================================================================
# 1. CLIENTES
# ===========================================================================
@router.get("/clientes", response_model=list[ClienteResponse])
def listar_clientes(
    solo_activos: bool = True,
    busqueda: str | None = Query(None, min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista clientes del negocio autenticado"""
    return service.get_clientes(db, user.negocio_id, solo_activos, busqueda)


@router.post("/clientes", response_model=ClienteResponse, status_code=status.HTTP_201_CREATED)
def crear_cliente(
    data: ClienteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Crea un nuevo cliente para el negocio"""
    return service.create_cliente(db, user.negocio_id, data)


@router.get("/clientes/{cliente_id}", response_model=ClienteResponse)
def obtener_cliente(
    cliente_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Obtiene un cliente por ID verificando pertenencia multi-tenant"""
    cliente = service.get_cliente(db, user.negocio_id, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.get("/clientes/telefono/{telefono}", response_model=ClienteResponse)
def buscar_cliente_por_telefono(
    telefono: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Busca cliente por teléfono dentro del negocio"""
    cliente = service.get_cliente_by_telefono(db, user.negocio_id, telefono)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.put("/clientes/{cliente_id}", response_model=ClienteResponse)
def actualizar_cliente(
    cliente_id: str,
    data: ClienteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Actualiza datos de un cliente existente"""
    cliente = service.get_cliente(db, user.negocio_id, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return service.update_cliente(db, cliente, data)


# ===========================================================================
# 2. CARTERA Y CUENTAS POR COBRAR (Rutas declaradas antes de /ventas/{id})
# ===========================================================================
@router.get("/ventas/cartera", response_model=list[CuentaPorCobrarDetalleResponse])
def listar_cartera(
    estado: str | None = None,
    solo_vencidas: bool = False,
    cliente_id: str | None = None,
    incluir_canceladas: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Consulta la cartera de cuentas por cobrar del negocio con cálculo dinámico de morosidad
    en huso horario comercial (America/La_Paz).
    Por defecto excluye cuentas canceladas.
    """
    try:
        return AccountsReceivableService.consultar_cartera(
            db, user.negocio_id, estado, solo_vencidas, cliente_id, incluir_canceladas
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/ventas/cartera/clientes", response_model=list[CarteraClienteItemResponse])
def listar_cartera_por_cliente(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna la cartera agregada por cliente en el negocio:
    exposición crediticia total, saldos operativos, importes presuntivos y días de mora máximos.
    """
    try:
        return AccountsReceivableService.consultar_cartera_por_cliente(db, user.negocio_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/ventas/cartera/{cuenta_id}", response_model=CuentaPorCobrarDetalleResponse)
def obtener_cuenta_por_cobrar(
    cuenta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Consulta el detalle operativo y financiero de una cuenta por cobrar"""
    cxc = db.query(CuentaPorCobrar).filter(
        CuentaPorCobrar.id == cuenta_id,
        CuentaPorCobrar.negocio_id == user.negocio_id
    ).first()
    if not cxc:
        raise HTTPException(status_code=404, detail="Cuenta por cobrar no encontrada")

    return AccountsReceivableService.consultar_cuenta_venta(db, user.negocio_id, cxc.venta_id)


@router.post("/ventas/cartera/{cuenta_id}/abono", response_model=AbonoResponse, status_code=status.HTTP_201_CREATED)
def registrar_abono_cartera(
    cuenta_id: str,
    data: AbonoCreate,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registra una amortización (abono) a una cuenta por cobrar.
    Protegido con bloqueo pesimista y control de sobrepago.
    Soporta idempotencia mediante la cabecera X-Idempotency-Key.
    """
    def action(commit: bool = True):
        try:
            return AccountsReceivableService.registrar_abono(
                db=db,
                negocio_id=user.negocio_id,
                cuenta_id=cuenta_id,
                usuario_id=user.id,
                monto=data.monto,
                metodo_pago=data.metodo_pago,
                referencia=data.referencia,
                confirmar_inmediato=data.confirmar_inmediato,
                commit=commit,
            )
        except ValueError as ve:
            if "no encontrada" in str(ve).lower():
                raise HTTPException(status_code=404, detail=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/cartera/{cuenta_id}/abono",
        idempotency_key=x_idempotency_key,
        request_data=data,
        action=action,
        status_code=201,
    )


@router.get("/ventas/clientes/{cliente_id}/estado-cuenta", response_model=EstadoCuentaClienteResponse)
def obtener_estado_cuenta_cliente(
    cliente_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene el estado de cuenta consolidado para un cliente específico"""
    cliente = db.query(Cliente).filter(
        Cliente.id == cliente_id,
        Cliente.negocio_id == user.negocio_id
    ).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    try:
        return AccountsReceivableService.estado_de_cuenta_cliente(db, user.negocio_id, cliente_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===========================================================================
# 3. PAGOS Y COBROS (Rutas declaradas antes de /ventas/{id})
# ===========================================================================
@router.post("/ventas/pagos/{pago_id}/confirmar", response_model=PagoResponse)
def confirmar_pago(
    pago_id: str,
    data: PagoConfirmarRequest | None = None,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Confirma un pago en estado PENDING (ej. comprobante QR verificado).
    Valida transaccionalmente que el saldo operativo restante cubra el importe.
    Soporta idempotencia mediante X-Idempotency-Key.
    """
    ref_conf = data.referencia_confirmacion if data else None
    notas = data.notas if data else None

    def action(commit: bool = True):
        try:
            return PaymentService.confirmar_pago(
                db=db,
                negocio_id=user.negocio_id,
                pago_id=pago_id,
                usuario_id=user.id,
                referencia_confirmacion=ref_conf,
                notas=notas,
                commit=commit,
            )
        except ValueError as ve:
            if "no encontrado" in str(ve).lower():
                raise HTTPException(status_code=404, detail=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/pagos/{pago_id}/confirmar",
        idempotency_key=x_idempotency_key,
        request_data=data,
        action=action,
        status_code=200,
    )


@router.post("/ventas/pagos/{pago_id}/marcar-fallido", response_model=PagoResponse)
def marcar_pago_fallido(
    pago_id: str,
    data: PagoMarcarFallidoRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Marca un pago en espera (PENDING) como fallido o rechazado"""
    try:
        return PaymentService.marcar_pago_fallido(
            db=db,
            negocio_id=user.negocio_id,
            pago_id=pago_id,
            usuario_id=user.id,
            motivo=data.motivo,
        )
    except ValueError as ve:
        if "no encontrado" in str(ve).lower():
            raise HTTPException(status_code=404, detail=str(ve))
        raise HTTPException(status_code=400, detail=str(ve))


@router.post("/ventas/pagos/{pago_id}/revertir", response_model=ReversionPagoResponse)
def revertir_pago(
    pago_id: str,
    data: ReversionPagoCreate,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Revierte (total o parcialmente) un pago confirmado mediante un evento compensatorio inmutable.
    Soporta idempotencia mediante X-Idempotency-Key.
    """
    def action(commit: bool = True):
        try:
            return PaymentService.revertir_pago(
                db=db,
                negocio_id=user.negocio_id,
                pago_id=pago_id,
                usuario_id=user.id,
                monto_a_revertir=data.monto,
                motivo=data.motivo,
                es_devolucion_efectivo=data.es_devolucion_efectivo,
                commit=commit,
            )
        except ValueError as ve:
            if "no encontrado" in str(ve).lower():
                raise HTTPException(status_code=404, detail=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/pagos/{pago_id}/revertir",
        idempotency_key=x_idempotency_key,
        request_data=data,
        action=action,
        status_code=200,
    )


@router.post("/ventas/pagos/{pago_id}/conciliar", response_model=ConciliacionPagoResponse)
def conciliar_pago_historico(
    pago_id: str,
    data: ConciliacionPagoCreate,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Aprueba una conciliación documental para un pago presuntivo histórico.
    Exige rol ADMIN o SUPERADMIN.
    Soporta idempotencia mediante X-Idempotency-Key.
    """
    roles_autorizados = {UserRole.ADMIN.value, UserRole.SUPERADMIN.value, "admin", "superadmin"}
    if user.rol not in roles_autorizados:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo un usuario con rol Administrador puede aprobar conciliaciones históricas"
        )

    def action(commit: bool = True):
        try:
            return PaymentService.conciliar_pago_historico(
                db=db,
                negocio_id=user.negocio_id,
                pago_id=pago_id,
                usuario_id=user.id,
                motivo=data.motivo,
                referencia_documental=data.referencia_documental,
                notas=data.notas,
                commit=commit,
            )
        except ValueError as ve:
            if "no encontrado" in str(ve).lower():
                raise HTTPException(status_code=404, detail=str(ve))
            raise HTTPException(status_code=400, detail=str(ve))
        except PermissionError as pe:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/pagos/{pago_id}/conciliar",
        idempotency_key=x_idempotency_key,
        request_data=data,
        action=action,
        status_code=200,
    )


# ===========================================================================
# 4. VENTAS (POS & Cabecera)
# ===========================================================================
@router.get("/ventas")
def listar_ventas(
    estado: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    cliente_id: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista ventas con paginación y filtros"""
    ventas, total = service.get_ventas(
        db, user.negocio_id, estado, fecha_desde, fecha_hasta, cliente_id, page, per_page
    )

    items = []
    for v in ventas:
        items.append({
            "id": v.id,
            "numero": v.numero,
            "cliente_nombre": v.cliente_nombre or (v.cliente.nombre if v.cliente else None),
            "total": v.total,
            "metodo_pago": v.metodo_pago,
            "estado": v.estado,
            "estado_financiero": getattr(v, "estado_financiero", "NOT_APPLICABLE"),
            "created_at": v.created_at,
            "items_count": len(v.detalles)
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    }


@router.post("/ventas", response_model=VentaResponse, status_code=status.HTTP_201_CREATED)
def crear_venta(
    data: VentaCreate,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Crea una nueva venta en el sistema POS con integridad transaccional y financiera completa.
    Soporta pagos de contado, crédito puro, anticipos y multi-pago.
    Protegido contra solicitudes duplicadas mediante la cabecera X-Idempotency-Key.
    """
    idemp_key = x_idempotency_key or data.idempotency_key

    def action(commit: bool = True):
        try:
            return SaleService.crear_venta_financiera(db, user.negocio_id, user.id, data, commit=commit)
        except ValueError as e:
            msg = str(e)
            if "no encontrado" in msg.lower():
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint="/api/v1/ventas",
        idempotency_key=idemp_key,
        request_data=data,
        action=action,
        status_code=201,
    )


@router.get("/ventas/{venta_id}", response_model=VentaResponse)
def obtener_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Obtiene una venta completa con sus detalles, pagos y cuenta por cobrar"""
    venta = service.get_venta(db, user.negocio_id, venta_id)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return venta


@router.get("/ventas/numero/{numero}", response_model=VentaResponse)
def obtener_venta_por_numero(
    numero: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Busca venta por número de ticket secuencial dentro del negocio"""
    venta = service.get_venta_by_numero(db, user.negocio_id, numero)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return venta


@router.get("/ventas/{venta_id}/historial", response_model=HistorialPagosVentaResponse)
def obtener_historial_pagos_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Obtiene el historial cronológico completo de cobros, reversiones y conciliaciones de una venta"""
    try:
        return PaymentService.consultar_historial(db, user.negocio_id, venta_id)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))


@router.post("/ventas/{venta_id}/pagos", response_model=PagoResponse, status_code=status.HTTP_201_CREATED)
def registrar_pago_venta(
    venta_id: str,
    data: PagoCreate,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Registra un pago adicional vinculado a una venta existente.
    Protegido contra sobrepagos y solicitudes repetidas mediante X-Idempotency-Key.
    """
    def action(commit: bool = True):
        try:
            return PaymentService.registrar_pago(
                db=db,
                negocio_id=user.negocio_id,
                venta_id=venta_id,
                usuario_id=user.id,
                monto=data.monto,
                metodo_pago=data.metodo_pago,
                referencia=data.referencia,
                confirmar_inmediato=(data.estado == "CONFIRMED") if data.estado else None,
                commit=commit,
            )
        except ValueError as ve:
            msg = str(ve)
            if "no encontrada" in msg.lower():
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/{venta_id}/pagos",
        idempotency_key=x_idempotency_key,
        request_data=data,
        action=action,
        status_code=201,
    )


@router.post("/ventas/{venta_id}/completar", response_model=VentaResponse)
def completar_venta(
    venta_id: str,
    data: VentaCompletarRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Completa una venta en estado pendiente (descuenta stock)"""
    venta = service.get_venta(db, user.negocio_id, venta_id)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    try:
        return service.completar_venta(db, user.negocio_id, venta, data, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ventas/{venta_id}/cancelar", response_model=VentaCancelacionResponse)
def cancelar_venta(
    venta_id: str,
    data: VentaCancelacionRequest | None = None,
    motivo: str | None = None,
    x_idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Cancela integralmente una venta en una sola frontera transaccional atómica:
    - Reingresa el inventario consumido a los lotes originales (ENTRADA / DEVOLUCION_VENTA).
    - Revierte compensatoriamente los pagos confirmados respetando reversiones parciales previas.
    - Invalida pagos PENDING (pasan a FAILED) impidiendo su confirmación futura.
    - Anula la cuenta por cobrar asociada (estado CANCELADA, saldo Bs 0.00).
    - Actualiza la venta a CANCELADA y estado financiero a NOT_APPLICABLE.
    Protegido con X-Idempotency-Key.
    """
    motivo_final = (data.motivo if data and data.motivo else motivo) or "Cancelación de venta"
    devolver_ef = data.devolver_efectivo if data else False
    payload_idemp = data.model_dump() if data else {"motivo": motivo_final, "devolver_efectivo": devolver_ef}

    def action(commit: bool = True):
        try:
            return SaleCancellationService.cancelar_venta(
                db=db,
                negocio_id=user.negocio_id,
                venta_id=venta_id,
                usuario_id=user.id,
                motivo=motivo_final,
                devolver_efectivo=devolver_ef,
                commit=commit,
            )
        except ValueError as ve:
            msg = str(ve)
            if "no encontrada" in msg.lower():
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)

    return ejecutar_con_idempotencia(
        db=db,
        negocio_id=user.negocio_id,
        usuario_id=user.id,
        metodo="POST",
        endpoint=f"/api/v1/ventas/{venta_id}/cancelar",
        idempotency_key=x_idempotency_key,
        request_data=payload_idemp,
        action=action,
        status_code=200,
    )


@router.get("/ventas/{venta_id}/auditoria-cancelacion")
def auditoria_cancelacion_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Consulta la auditoría detallada de la cancelación de una venta"""
    try:
        return SaleCancellationService.consultar_auditoria_cancelacion(db, user.negocio_id, venta_id)
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))


# ===========================================================================
# 5. REPORTES
# ===========================================================================
@router.get("/reportes/ventas-dia", response_model=ResumenVentasDia)
def reporte_ventas_dia(
    fecha: date = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Resumen de ventas de un día (default: hoy)"""
    if fecha is None:
        fecha = date.today()
    return service.get_resumen_ventas_dia(db, user.negocio_id, fecha)


@router.get("/reportes/caja", response_model=ResumenCaja)
def reporte_caja(
    fecha: date = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Resumen de caja del día (default: hoy)"""
    if fecha is None:
        fecha = date.today()
    return service.get_resumen_caja(db, user.negocio_id, fecha)
