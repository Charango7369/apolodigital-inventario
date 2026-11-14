"""
SaleCancellationService — Servicio de cancelación integral de ventas con consistencia transaccional y financiera (Bloque 2.4).

Responsabilidades:
1. Validar pertenencia multi-tenant estricta (negocio_id derivado exclusivamente del contexto autenticado).
2. Adquisición determinista de bloqueos pesimistas (with_for_update) sobre Venta, PagoVenta y CuentaPorCobrar.
3. Rechazo estricto de doble cancelación sobre ventas ya anuladas (CANCELADA).
4. Devolución atómica de existencias mediante crear_movimiento (tipo ENTRADA, motivo DEVOLUCION_VENTA).
   - Preserva negocio, almacén, producto, variante, lote original consumido, cantidad y referencia de venta.
   - No actualiza directamente el stock ni duplica reingresos.
5. Reversión compensatoria de pagos confirmados delegando en PaymentService.revertir_pago.
   - Inmutabilidad estricta: nunca modifica ni elimina los registros de PagoVenta.
   - Respeta reversiones parciales previas y revierte únicamente el saldo reversible no compensado.
   - Diferencia la reversión contable de la devolución física de efectivo (es_devolucion_efectivo solo para EFECTIVO).
6. Cancelación explícita de pagos PENDING:
   - Se marcan como FAILED con trazabilidad de auditoría.
   - Quedan bloqueados para confirmación futura tanto por estado FAILED como por venta CANCELADA.
7. Cancelación de cuentas por cobrar:
   - Estado transiciona a CANCELADA y saldo_pendiente a Bs 0.00.
   - Preserva historial completo de cargos y abonos para auditoría; excluye la deuda de la cartera activa exigible.
8. Actualización de cabecera de venta:
   - estado = CANCELADA, estado_financiero = NOT_APPLICABLE, cancelled_at = timestamp UTC.
9. Transacción única y atómica:
   - Si cualquier operación falla (inventario, reversión, cartera o cabecera), rollback total e íntegro.
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import (
    Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar,
    EstadoVenta, EstadoFinanciero, EstadoPago, EstadoCuentaCobrar, MetodoPago,
)
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.inventario.models import MovimientoStock, Stock, Lote
from app.modules.inventario.schemas import MovimientoCreate
from app.modules.inventario.service import crear_movimiento


class SaleCancellationService:

    @classmethod
    def cancelar_venta(
        cls,
        db: Session,
        negocio_id: str,
        venta_id: str,
        usuario_id: str,
        motivo: str = "Cancelación de venta",
        devolver_efectivo: bool = False,
        commit: bool = True,
    ) -> dict:
        """
        Ejecuta la cancelación integral de una venta con devolución de inventario,
        reversiones compensatorias de pagos y anulación de cartera en una única transacción atómica.
        """
        if not motivo or len(motivo.strip()) < 3:
            raise ValueError("Debe proporcionar un motivo válido para la cancelación (mínimo 3 caracteres)")

        motivo_limpio = motivo.strip()

        try:
            # -------------------------------------------------------------------
            # 1. Bloqueo determinista de Venta (Previene deadlocks e IDOR)
            # -------------------------------------------------------------------
            venta = db.query(Venta).filter(
                Venta.id == venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta no encontrada o no pertenece a su negocio")

            if venta.estado == EstadoVenta.CANCELADA.value:
                raise ValueError("La venta ya se encuentra cancelada")

            # -------------------------------------------------------------------
            # 2. Bloqueo determinista de Pagos de la Venta (orden ascendente por ID)
            # -------------------------------------------------------------------
            pagos = db.query(PagoVenta).filter(
                PagoVenta.venta_id == venta.id,
                PagoVenta.negocio_id == negocio_id
            ).with_for_update().order_by(PagoVenta.id.asc()).all()

            # -------------------------------------------------------------------
            # 3. Bloqueo determinista de Cuenta por Cobrar si existe
            # -------------------------------------------------------------------
            cxc = db.query(CuentaPorCobrar).filter(
                CuentaPorCobrar.venta_id == venta.id,
                CuentaPorCobrar.negocio_id == negocio_id
            ).with_for_update().first()

            # -------------------------------------------------------------------
            # 4. Devolución de existencias mediante crear_movimiento
            #    Aplica únicamente si la venta estaba COMPLETADA (descontó stock)
            # -------------------------------------------------------------------
            movimientos_reingreso = []

            if venta.estado == EstadoVenta.COMPLETADA.value:
                # Buscar todas las salidas generadas originalmente por esta venta (orden determinista)
                movs_salida = db.query(MovimientoStock).filter(
                    MovimientoStock.referencia_id == str(venta.id),
                    MovimientoStock.tipo == "SALIDA_VENTA",
                    MovimientoStock.almacen_id == venta.almacen_id,
                ).order_by(MovimientoStock.id.asc()).all()

                for mov in movs_salida:
                    cantidad_devolver = abs(Decimal(str(mov.cantidad)))
                    if cantidad_devolver <= Decimal("0.00"):
                        continue

                    # Registrar movimiento de tipo ENTRADA y motivo DEVOLUCION_VENTA
                    # Preserva variante, almacén, lote original consumido y costo
                    mov_data = MovimientoCreate(
                        variante_id=mov.variante_id,
                        almacen_id=mov.almacen_id,
                        tipo="ENTRADA",
                        cantidad=cantidad_devolver,
                        lote_id=mov.lote_id,
                        costo_unitario=mov.costo_unitario,
                        referencia_id=str(venta.id),
                        motivo=f"DEVOLUCION_VENTA: Cancelación venta #{venta.numero} - {motivo_limpio}",
                    )
                    nuevos_movs = crear_movimiento(db, negocio_id, mov_data, usuario_id)
                    movimientos_reingreso.extend(nuevos_movs)

            # -------------------------------------------------------------------
            # 5. Tratamiento de pagos: Reversiones de CONFIRMED y anulación de PENDING
            # -------------------------------------------------------------------
            reversiones_generadas = []
            pagos_pendientes_cancelados = []
            total_revertido_acumulado = Decimal("0.00")
            hubo_devolucion_efectivo = False

            for p in pagos:
                if p.estado == EstadoPago.PENDING.value:
                    # Inhabilitar confirmación concurrente o posterior
                    p.estado = EstadoPago.FAILED.value
                    p.referencia = (p.referencia or "") + f" [CANCELADO: Venta anulada - {motivo_limpio}]"
                    pagos_pendientes_cancelados.append(p.id)
                    db.flush()

                elif p.estado == EstadoPago.CONFIRMED.value:
                    # Determinar el saldo que todavía no ha sido revertido
                    reversiones_previas = db.query(
                        func.coalesce(func.sum(ReversionPago.monto_revertido), Decimal("0.00"))
                    ).filter(
                        ReversionPago.pago_id == p.id,
                        ReversionPago.negocio_id == negocio_id
                    ).scalar()

                    disponible = Decimal(str(p.monto)) - Decimal(str(reversiones_previas))

                    if disponible > Decimal("0.00"):
                        # REGLA H-15: La devolución física solo aplica a pagos efectuados en EFECTIVO
                        es_efectivo = (p.metodo_pago == MetodoPago.EFECTIVO.value)
                        devolucion_fisica = devolver_efectivo and es_efectivo
                        if devolucion_fisica:
                            hubo_devolucion_efectivo = True

                        reversion = PaymentService.revertir_pago(
                            db=db,
                            negocio_id=negocio_id,
                            pago_id=p.id,
                            usuario_id=usuario_id,
                            monto_a_revertir=disponible,
                            motivo=f"Cancelación venta #{venta.numero}: {motivo_limpio}",
                            es_devolucion_efectivo=devolucion_fisica,
                            commit=False,  # Mantener atomicidad en esta frontera
                        )
                        reversiones_generadas.append(reversion)
                        total_revertido_acumulado += disponible

            # -------------------------------------------------------------------
            # 6. Cancelación de Cuenta por Cobrar asociada
            # -------------------------------------------------------------------
            if cxc:
                cxc.estado = EstadoCuentaCobrar.CANCELADA.value
                cxc.saldo_pendiente = Decimal("0.00")
                cxc.updated_at = datetime.now(timezone.utc)
                cxc.notas = (cxc.notas or "") + f"\n[CANCELADA: Venta anulada - {motivo_limpio}]"
                db.flush()

            # -------------------------------------------------------------------
            # 7. Actualización del Estado Operativo y Financiero de la Venta
            # -------------------------------------------------------------------
            ahora = datetime.now(timezone.utc)
            venta.estado = EstadoVenta.CANCELADA.value
            venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value
            venta.cancelled_at = ahora
            venta.notas = (venta.notas or "") + f"\n[CANCELADA] {motivo_limpio}"
            db.flush()

            # -------------------------------------------------------------------
            # 8. Cierre atómico de la transacción
            # -------------------------------------------------------------------
            if commit:
                db.commit()
                db.refresh(venta)
            else:
                db.flush()

            return {
                "venta_id": venta.id,
                "numero_ticket": venta.numero,
                "estado_operativo": venta.estado,
                "estado_financiero": venta.estado_financiero,
                "total_venta": venta.total,
                "total_revertido": total_revertido_acumulado,
                "es_devolucion_efectivo": hubo_devolucion_efectivo,
                "reversiones_generadas": len(reversiones_generadas),
                "pagos_pendientes_cancelados": len(pagos_pendientes_cancelados),
                "movimientos_inventario_reingresados": len(movimientos_reingreso),
                "cuenta_cobrar_cancelada": cxc is not None,
                "cancelled_at": venta.cancelled_at,
                "motivo": motivo_limpio,
            }

        except Exception:
            if commit:
                db.rollback()
            raise

    @classmethod
    def consultar_auditoria_cancelacion(
        cls,
        db: Session,
        negocio_id: str,
        venta_id: str,
    ) -> dict:
        """
        Consulta los detalles de auditoría para una venta cancelada:
        reversiones registradas, pagos anulados y reingresos de stock.
        """
        venta = db.query(Venta).options(
            joinedload(Venta.pagos).joinedload(PagoVenta.reversiones),
            joinedload(Venta.cuenta_cobrar),
        ).filter(
            Venta.id == venta_id,
            Venta.negocio_id == negocio_id
        ).first()

        if not venta:
            raise ValueError("Venta no encontrada o no pertenece a su negocio")

        reversiones = []
        for p in venta.pagos:
            for r in p.reversiones:
                reversiones.append({
                    "id": r.id,
                    "pago_id": r.pago_id,
                    "monto_revertido": r.monto_revertido,
                    "motivo": r.motivo,
                    "fecha_reversion": r.fecha_reversion,
                    "es_devolucion_efectivo": r.es_devolucion_efectivo,
                })

        movs_reingreso = db.query(MovimientoStock).filter(
            MovimientoStock.referencia_id == str(venta.id),
            MovimientoStock.tipo == "ENTRADA",
            MovimientoStock.almacen_id == venta.almacen_id,
        ).all()

        reingresos = [
            {
                "id": m.id,
                "variante_id": m.variante_id,
                "lote_id": m.lote_id,
                "cantidad": m.cantidad,
                "motivo": m.motivo,
                "created_at": m.created_at,
            }
            for m in movs_reingreso
        ]

        return {
            "venta_id": venta.id,
            "numero_ticket": venta.numero,
            "estado_operativo": venta.estado,
            "estado_financiero": venta.estado_financiero,
            "total_venta": venta.total,
            "cancelled_at": venta.cancelled_at,
            "notas": venta.notas,
            "reversiones": reversiones,
            "movimientos_reingreso": reingresos,
            "cuenta_cobrar": {
                "id": venta.cuenta_cobrar.id,
                "estado": venta.cuenta_cobrar.estado,
                "saldo_pendiente": venta.cuenta_cobrar.saldo_pendiente,
            } if venta.cuenta_cobrar else None,
        }
