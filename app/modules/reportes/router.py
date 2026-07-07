"""
Router FastAPI — módulo reportes
Consolida endpoints de Inventario y Ventas Financieras.
"""

# 1. Librerías Estándar
from datetime import date
from typing import List

# 2. Librerías de Terceros
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

# 3. Módulos Locales de la Aplicación
from app.database import get_db
from app.dependencies import get_current_user  # Ajusta a tu auth real
from app.modules.auth.models import User
from app.modules.inventario.models import Almacen, Producto, Stock, Variante
from app.modules.reportes import service
from app.modules.reportes.schemas import (
    AlertaStockDTO,
    StockBaseDTO,
    UtilidadLegacyEstimadaResponse,
    UtilidadPeriodoResponse,
    UtilidadPorProductoResponse,
    UtilidadVentaResponse,
)

router = APIRouter(tags=["reportes"])

# ===========================================================================
# 1. ENDPOINTS DE INVENTARIO (Stock)
# ===========================================================================

@router.get("/stock-actual", response_model=List[StockBaseDTO])
def reporte_stock_actual(
    almacen_id: str = Query(None, description="Filtrar por ID de almacén"),
    categoria_id: str = Query(None, description="Filtrar por ID de categoría"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista el stock actual consolidado y valorizado."""
    # El router delega el 100% del trabajo pesado al servicio
    return service.obtener_reporte_stock_actual(
        db=db, 
        negocio_id=user.negocio_id, 
        almacen_id=almacen_id, 
        categoria_id=categoria_id
    )

@router.get("/alertas-stock", response_model=List[AlertaStockDTO])
def reporte_alertas_stock(
    almacen_id: str = Query(None, description="Filtrar por ID de almacén"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista productos bajo stock mínimo o agotados."""
    return service.obtener_reporte_alertas_stock(
        db=db,
        negocio_id=user.negocio_id,
        almacen_id=almacen_id
    )


# ===========================================================================
# 2. ENDPOINTS DE UTILIDAD (Ventas)
# ===========================================================================

@router.get("/utilidad-venta/{venta_id}", response_model=UtilidadVentaResponse)
def utilidad_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resultado = service.calcular_utilidad_venta(db, user.negocio_id, venta_id)
    if resultado is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Venta no encontrada")
    return resultado

@router.get("/utilidad-legacy-estimada/{venta_id}", response_model=UtilidadLegacyEstimadaResponse)
def utilidad_legacy_estimada(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resultado = service.calcular_utilidad_legacy_estimada(db, user.negocio_id, venta_id)
    if resultado is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Venta no encontrada")
    return resultado

@router.get("/utilidad-periodo", response_model=UtilidadPeriodoResponse)
def utilidad_periodo(
    desde: date = Query(..., description="Fecha inicial (ej. 2026-06-01)"),
    hasta: date = Query(..., description="Fecha final (ej. 2026-06-30)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return service.calcular_utilidad_periodo(db, user.negocio_id, desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.get("/utilidad-productos", response_model=UtilidadPorProductoResponse)
def utilidad_productos(
    desde: date = Query(..., description="Fecha inicial (ej. 2026-06-01)"),
    hasta: date = Query(..., description="Fecha final (ej. 2026-06-30)"),
    orden: str = Query("profit", pattern="^(profit|margin|revenue)$", description="Criterio de ordenamiento: profit, margin, revenue"),
    limit: int = Query(10, ge=1, le=50, description="Límite estricto de elementos por top (1 a 50)"),
    user: User = Depends(get_current_user), # Candado de seguridad intacto
    db: Session = Depends(get_db)
):
    """
    Retorna la rentabilidad fragmentada a nivel de producto raíz.
    Divide el catálogo en el Top de mayor rentabilidad (motores) y el Top de mayores fugas de capital.
    """
    try:
        return service.calcular_utilidad_por_producto(
            db=db, 
            negocio_id=user.negocio_id, 
            desde=desde, 
            hasta=hasta, 
            orden=orden, 
            limit=limit
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    
