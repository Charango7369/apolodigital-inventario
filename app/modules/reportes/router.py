"""
Router FastAPI — módulo reportes

Endpoints:
  GET /reportes/utilidad-venta/{venta_id}
       Utilidad real con costo de lote, FUENTE DE VERDAD CONTABLE.

  GET /reportes/utilidad-legacy-estimada/{venta_id}
       Utilidad aproximada usando DetalleVenta.costo_unitario.
       Solo para ventas legacy (pre-migración) o con movimientos incompletos.

Próximas sesiones:
  GET /reportes/utilidad-periodo
  GET /reportes/utilidad-por-producto
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.reportes import service
from app.modules.reportes.schemas import (
    UtilidadVentaResponse,
    UtilidadLegacyEstimadaResponse,
)


router = APIRouter(prefix="/reportes", tags=["reportes"])


@router.get(
    "/utilidad-venta/{venta_id}",
    response_model=UtilidadVentaResponse,
)
def utilidad_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Utilidad real de una venta calculada con costo de lote.

    REGLAS:
    - Costo histórico inmutable: se reconstruye desde MovimientoStock,
      no desde Lote actual ni Variante.precio_costo. Cambios futuros en
      esos no afectan utilidad histórica.
    - Si CUALQUIER línea tiene movimientos legacy (lote_id NULL o
      costo_unitario NULL), toda la venta queda marcada como legacy
      y cost_real/profit/margin a nivel venta son NULL.
      Esto es opción A: honesto sobre lo que sabemos.
    - Para esos casos, usá GET /reportes/utilidad-legacy-estimada/{id}.
    - Descuento se prorratea proporcionalmente al subtotal de cada línea.
    - Utilidad = total (post-descuento) - cost_real.
    """
    resultado = service.calcular_utilidad_venta(
        db, user.negocio_id, venta_id,
    )
    if resultado is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Venta no encontrada o no pertenece al negocio",
        )
    return resultado


@router.get(
    "/utilidad-legacy-estimada/{venta_id}",
    response_model=UtilidadLegacyEstimadaResponse,
)
def utilidad_legacy_estimada(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Utilidad ESTIMADA para ventas legacy.

    Usa DetalleVenta.costo_unitario que es el precio_costo congelado al
    momento de la venta. NO usa el precio_costo actual de la variante
    (que pudo haber cambiado).

    Si DetalleVenta.costo_unitario también es NULL, no podemos estimar
    y devolvemos NULL en cost_estimado a nivel venta.

    Este endpoint NO debe usarse como fuente de verdad contable. Para
    ventas post-migración con datos completos, usá /utilidad-venta/{id}.
    """
    resultado = service.calcular_utilidad_legacy_estimada(
        db, user.negocio_id, venta_id,
    )
    if resultado is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Venta no encontrada o no pertenece al negocio",
        )
    return resultado


# ===========================================================================
# A.2 — Endpoints agregados
# ===========================================================================
from datetime import date

from app.modules.reportes.schemas import (
    UtilidadPeriodoResponse,
    UtilidadPorProductoResponse,
)


@router.get(
    "/utilidad-periodo",
    response_model=UtilidadPeriodoResponse,
)
def utilidad_periodo(
    desde: date,
    hasta: date,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Utilidad agregada del periodo con breakdown temporal automatico.

    Reglas:
    - Solo ventas COMPLETADA filtradas por completed_at en el rango.
    - Excluye ventas legacy (lote_id NULL o costo_unitario NULL en cualquier salida).
      Las cuenta en bloque informativo aparte.
    - Bloque informativo de canceladas (filtradas por cancelled_at).
    - Granularidad automatica: dia si rango <= 90 dias, mes si > 90.
    - Rango maximo: 365 dias.

    Casos borde:
    - Periodo sin ventas: revenue=0, profit=0, por_periodo=[]
    - Todas las ventas son legacy: revenue=0 calculado, info en bloque legacy
    """
    try:
        return service.calcular_utilidad_periodo(
            db, user.negocio_id, desde, hasta,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/utilidad-por-producto",
    response_model=UtilidadPorProductoResponse,
)
def utilidad_por_producto(
    desde: date,
    hasta: date,
    orden: str = "profit",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Utilidad agregada POR PRODUCTO en el periodo.

    Identifica los productos que mas dejan en absoluto (por defecto)
    o los de mayor margen porcentual (orden=margin) para Pareto de
    rentabilidad.

    Mismas reglas que utilidad-periodo:
    - Solo COMPLETADA, completed_at en rango.
    - Excluye legacy.
    - Rango max 365 dias.

    Parametro orden: profit | margin | revenue (default: profit DESC)
    """
    try:
        return service.calcular_utilidad_por_producto(
            db, user.negocio_id, desde, hasta, orden,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
