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
