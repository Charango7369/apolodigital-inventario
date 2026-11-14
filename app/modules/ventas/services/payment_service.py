"""
PaymentService — Servicio de gestión de pagos, confirmaciones, reversiones y conciliaciones (Fase 2.2.1).

Responsabilidades:
1. Registrar pagos nuevos vinculados a una venta dentro del tenant con origen OPERATIVO.
2. Controlar transiciones de estado (PENDING -> CONFIRMED, FAILED).
3. Revertir pagos total o parcialmente mediante eventos compensatorios inmutables (ReversionPago).
4. Desacoplar dimensión de estado (PENDING, CONFIRMED, FAILED) de la procedencia/evidencia (OPERATIVO, HISTORICO_PRESUNTIVO).
5. Conciliar pagos históricos presuntivos mediante registros independientes e inmutables (ConciliacionPago) sin mutar PagoVenta.
6. Consultar historial cronológico completo de cobros, reversiones y conciliaciones.
7. Mantener consistencia atómica con el saldo de CuentaPorCobrar y Venta.estado_financiero (PAID, PAID_PRESUNTIVO, PARTIAL, CREDIT).
8. Bloqueo pesimista con_for_update para concurrencia segura en PostgreSQL.
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import (
    Venta, PagoVenta, ReversionPago, CuentaPorCobrar, ConciliacionPago,
    EstadoVenta, EstadoFinanciero, EstadoPago, OrigenPago, EstadoConciliacion, MetodoPago,
)
from app.modules.auth.models import User, UserRole


class PaymentService:

    @classmethod
    def registrar_pago(
        cls,
        db: Session,
        negocio_id: str,
        venta_id: str,
        usuario_id: str,
        monto: Decimal,
        metodo_pago: str,
        referencia: str | None = None,
        confirmar_inmediato: bool | None = None,
        commit: bool = True,
    ) -> PagoVenta:
        """
        Registra un pago para una venta existente dentro del tenant con origen OPERATIVO.
        El frontend no impone el estado: si confirmar_inmediato es None,
        EFECTIVO/TARJETA inician CONFIRMED, mientras QR/TRANSFERENCIA inician PENDING.
        """
        try:
            if monto <= Decimal("0.00"):
                raise ValueError("El importe del pago debe ser estrictamente mayor a cero")

            # Bloqueo pesimista sobre la venta
            venta = db.query(Venta).filter(
                Venta.id == venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta no encontrada o no pertenece a su negocio")

            if venta.estado == EstadoVenta.CANCELADA.value:
                raise ValueError("No se pueden registrar pagos en una venta cancelada")

            # Calcular saldo pendiente actual de la venta desde base de datos
            pagos_actuales = db.query(PagoVenta).options(
                joinedload(PagoVenta.reversiones)
            ).filter(
                PagoVenta.venta_id == venta.id,
                PagoVenta.negocio_id == negocio_id
            ).all()

            cobrado_neto = Decimal("0.00")
            for p in pagos_actuales:
                if p.estado == EstadoPago.CONFIRMED.value:
                    cobrado_neto += (p.monto - p.total_revertido)

            saldo_pendiente_venta = venta.total - cobrado_neto
            if monto > saldo_pendiente_venta:
                raise ValueError(
                    f"El monto a pagar (Bs {monto}) supera el saldo pendiente de la venta (Bs {saldo_pendiente_venta})"
                )

            # Transición controlada por backend
            if confirmar_inmediato is None:
                estado = EstadoPago.CONFIRMED.value if metodo_pago in ("EFECTIVO", "TARJETA") else EstadoPago.PENDING.value
            elif confirmar_inmediato is True:
                estado = EstadoPago.CONFIRMED.value
            else:
                estado = EstadoPago.PENDING.value

            ahora = datetime.now(timezone.utc)
            pago = PagoVenta(
                id=str(uuid.uuid4()),
                negocio_id=negocio_id,
                venta_id=venta.id,
                monto=monto,
                metodo_pago=metodo_pago,
                estado=estado,
                origen=OrigenPago.OPERATIVO.value,
                usuario_id=usuario_id,
                fecha_pago=ahora,
                referencia=referencia,
            )
            db.add(pago)
            db.flush()

            # Si el pago nace confirmado, actualizar cuenta por cobrar y estado financiero
            if estado == EstadoPago.CONFIRMED.value:
                cls._aplicar_efecto_pago_confirmado(db, negocio_id, venta, monto)

            if commit:
                db.commit()
                db.refresh(pago)
            else:
                db.flush()
            return pago

        except Exception:
            if commit:
                db.rollback()
            raise

    @classmethod
    def confirmar_pago(
        cls,
        db: Session,
        negocio_id: str,
        pago_id: str,
        usuario_id: str,
        referencia_confirmacion: str | None = None,
        notas: str | None = None,
        commit: bool = True,
    ) -> PagoVenta:
        """
        Confirma explícitamente un pago en estado PENDING (ej. comprobante QR o transferencia verificado).
        Valida transaccionalmente que el saldo operativo restante cubra el importe (Observación A).
        """
        try:
            # 1. Resolver metadatos del pago para orden determinista de bloqueos (Venta -> Pago)
            pago_meta = db.query(PagoVenta.id, PagoVenta.venta_id, PagoVenta.estado).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).first()

            if not pago_meta:
                raise ValueError("Pago no encontrado o no pertenece a su negocio")

            if pago_meta.estado == EstadoPago.CONFIRMED.value:
                raise ValueError("El pago ya se encuentra confirmado")

            if pago_meta.estado == EstadoPago.FAILED.value:
                raise ValueError("No se puede confirmar un pago en estado fallido")

            if pago_meta.estado != EstadoPago.PENDING.value:
                raise ValueError(f"Estado inválido para confirmación: {pago_meta.estado}")

            # 2. Bloqueo determinista de la Venta primero (Venta -> Pago para prevenir deadlocks)
            venta = db.query(Venta).filter(
                Venta.id == pago_meta.venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta asociada no encontrada")

            if venta.estado == EstadoVenta.CANCELADA.value:
                raise ValueError("No se puede confirmar un pago para una venta cancelada")

            # 3. Bloqueo determinista del Pago
            pago = db.query(PagoVenta).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).with_for_update().first()

            # VALIDACIÓN TRANSACCIONAL DE CONCURRENCIA (Observación A - Fase 2.3):
            # Recalcular el saldo operativo pendiente antes de confirmar este pago.
            pagos_confirmados = db.query(PagoVenta).filter(
                PagoVenta.venta_id == venta.id,
                PagoVenta.negocio_id == negocio_id,
                PagoVenta.estado == EstadoPago.CONFIRMED.value
            ).all()

            reversiones_por_pago = dict(
                db.query(
                    ReversionPago.pago_id,
                    func.coalesce(func.sum(ReversionPago.monto_revertido), Decimal("0.00"))
                ).filter(
                    ReversionPago.venta_id == venta.id,
                    ReversionPago.negocio_id == negocio_id
                ).group_by(ReversionPago.pago_id).all()
            )

            cobrado_neto = Decimal("0.00")
            for pc in pagos_confirmados:
                rev = Decimal(str(reversiones_por_pago.get(pc.id, Decimal("0.00"))))
                cobrado_neto += max(Decimal("0.00"), pc.monto - rev)

            saldo_operativo_restante = max(Decimal("0.00"), venta.total - cobrado_neto)
            if pago.monto > saldo_operativo_restante:
                raise ValueError(
                    f"La confirmación del pago por Bs {pago.monto} supera el saldo operativo pendiente "
                    f"de la venta (Bs {saldo_operativo_restante})"
                )

            # Transición a confirmado
            pago.estado = EstadoPago.CONFIRMED.value

            anotaciones = []
            if referencia_confirmacion:
                anotaciones.append(f"CONF:{referencia_confirmacion}")
            if notas:
                anotaciones.append(f"NOTAS:{notas}")

            if anotaciones:
                tag = f" [{' '.join(anotaciones)}]"
                pago.referencia = (pago.referencia or "") + tag

            db.flush()

            # Aplicar saldo a cartera y actualizar estado financiero
            cls._aplicar_efecto_pago_confirmado(db, negocio_id, venta, pago.monto)

            if commit:
                db.commit()
                db.refresh(pago)
            else:
                db.flush()
            return pago

        except Exception:
            db.rollback()
            raise

    @classmethod
    def marcar_pago_fallido(
        cls,
        db: Session,
        negocio_id: str,
        pago_id: str,
        usuario_id: str,
        motivo: str,
        commit: bool = True,
    ) -> PagoVenta:
        """Marca un pago PENDING como FAILED. Es naturalmente idempotente ante reintentos."""
        try:
            pago = db.query(PagoVenta).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).with_for_update().first()

            if not pago:
                raise ValueError("Pago no encontrado o no pertenece a su negocio")

            # IDEMPOTENCIA NATURAL: Si ya está en FAILED, retornar sin error ni duplicar notas
            if pago.estado == EstadoPago.FAILED.value:
                return pago

            if pago.estado != EstadoPago.PENDING.value:
                raise ValueError(f"Solo se pueden marcar como fallidos los pagos pendientes. Estado actual: {pago.estado}")

            pago.estado = EstadoPago.FAILED.value
            pago.referencia = (pago.referencia or "") + f" [FALLIDO: {motivo}]"

            if commit:
                db.commit()
                db.refresh(pago)
            else:
                db.flush()
            return pago

        except Exception:
            if commit:
                db.rollback()
            raise

    @classmethod
    def revertir_pago(
        cls,
        db: Session,
        negocio_id: str,
        pago_id: str,
        usuario_id: str,
        monto_a_revertir: Decimal | None = None,
        motivo: str = "Reversión contable de pago",
        es_devolucion_efectivo: bool = False,
        commit: bool = True,
    ) -> ReversionPago:
        """
        Revierte total o parcialmente un pago CONFIRMED mediante un evento compensatorio inmutable.
        Actualiza la CuentaPorCobrar (reabriendo saldo) y el estado financiero de la Venta.
        """
        try:
            if not motivo or len(motivo.strip()) < 3:
                raise ValueError("Debe proporcionar un motivo válido para la reversión (mínimo 3 caracteres)")

            # 1. Resolver metadatos del pago para orden determinista de bloqueos (Venta -> Pago)
            pago_meta = db.query(PagoVenta.id, PagoVenta.venta_id, PagoVenta.estado).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).first()

            if not pago_meta:
                raise ValueError("Pago no encontrado o no pertenece a su negocio")

            if pago_meta.estado != EstadoPago.CONFIRMED.value:
                raise ValueError(f"Solo se pueden revertir pagos confirmados. Estado actual: {pago_meta.estado}")

            # 2. Bloqueo determinista de la Venta primero (Venta -> Pago -> CxC)
            venta = db.query(Venta).filter(
                Venta.id == pago_meta.venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta asociada no encontrada")

            # 3. Bloqueo determinista del Pago
            pago = db.query(PagoVenta).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).with_for_update().first()

            reversiones_previas = db.query(
                func.coalesce(func.sum(ReversionPago.monto_revertido), Decimal("0.00"))
            ).filter(
                ReversionPago.pago_id == pago.id,
                ReversionPago.negocio_id == negocio_id
            ).scalar()

            disponible = Decimal(str(pago.monto)) - Decimal(str(reversiones_previas))
            if disponible <= Decimal("0.00"):
                raise ValueError("El pago ya ha sido completamente revertido")

            if monto_a_revertir is None:
                monto_a_revertir = disponible

            if monto_a_revertir <= Decimal("0.00"):
                raise ValueError("El importe a revertir debe ser estrictamente mayor a cero")

            if monto_a_revertir > disponible:
                raise ValueError(
                    f"El importe a revertir (Bs {monto_a_revertir}) supera el saldo reversible disponible (Bs {disponible})"
                )

            ahora = datetime.now(timezone.utc)
            reversion = ReversionPago(
                id=str(uuid.uuid4()),
                negocio_id=negocio_id,
                pago_id=pago.id,
                venta_id=pago.venta_id,
                monto_revertido=monto_a_revertir,
                motivo=motivo.strip(),
                usuario_id=usuario_id,
                fecha_reversion=ahora,
                es_devolucion_efectivo=es_devolucion_efectivo,
                pago=pago,
            )
            db.add(reversion)
            db.flush()

            # Reabrir saldo en CuentaPorCobrar si existe
            cxc = db.query(CuentaPorCobrar).filter(
                CuentaPorCobrar.venta_id == venta.id,
                CuentaPorCobrar.negocio_id == negocio_id
            ).with_for_update().first()

            if cxc:
                cxc.saldo_pendiente = min(cxc.monto_total_venta, cxc.saldo_pendiente + monto_a_revertir)
                if cxc.estado == "PAGADA" and cxc.saldo_pendiente > Decimal("0.00"):
                    cxc.estado = "PENDIENTE"
                cxc.updated_at = ahora
                db.flush()

            # Recalcular estado financiero de la venta
            cls._recalcular_estado_financiero_venta(db, venta)

            if commit:
                db.commit()
                db.refresh(reversion)
            else:
                db.flush()
            return reversion

        except Exception:
            db.rollback()
            raise

    @classmethod
    def conciliar_pago_historico(
        cls,
        db: Session,
        negocio_id: str,
        pago_id: str,
        usuario_id: str,
        motivo: str,
        referencia_documental: str,
        notas: str | None = None,
        commit: bool = True,
    ) -> ConciliacionPago:
        """
        Permite a un usuario autorizado (ADMIN o SUPERADMIN) verificar formalmente un pago presuntivo
        migrado de Fase 1.3 mediante un evento inmutable e independiente de ConciliacionPago.
        PagoVenta permanece estrictamente inmutable (sin mutar origen ni referencia).
        Amortiza la deuda operativa de la cartera (CuentaPorCobrar) al reconocerse el cobro verificado.
        """
        try:
            if not motivo or len(motivo.strip()) < 3:
                raise ValueError("Debe proporcionar un motivo válido para la conciliación (mínimo 3 caracteres)")
            if not referencia_documental or len(referencia_documental.strip()) < 1:
                raise ValueError("Debe proporcionar una referencia documental obligatoria para la conciliación")

            # Verificación de autorización por rol
            usuario = db.query(User).filter(
                User.id == usuario_id,
                User.negocio_id == negocio_id,
                User.activo == True
            ).first()

            if not usuario:
                raise ValueError("Usuario no encontrado o no pertenece a su negocio")

            roles_autorizados = {UserRole.ADMIN.value, UserRole.SUPERADMIN.value, "admin", "superadmin"}
            if usuario.rol not in roles_autorizados:
                raise PermissionError("Solo un usuario con rol Administrador puede aprobar conciliaciones históricas")

            # Bloqueo pesimista del pago
            pago = db.query(PagoVenta).options(
                joinedload(PagoVenta.conciliaciones)
            ).filter(
                PagoVenta.id == pago_id,
                PagoVenta.negocio_id == negocio_id
            ).with_for_update().first()

            if not pago:
                raise ValueError("Pago no encontrado o no pertenece a su negocio")

            if pago.origen != OrigenPago.HISTORICO_PRESUNTIVO.value:
                raise ValueError(
                    f"Solo los pagos con origen HISTORICO_PRESUNTIVO pueden ser conciliados. Origen actual: {pago.origen}"
                )

            if pago.esta_conciliado:
                raise ValueError("El pago histórico ya cuenta con una conciliación aprobada")

            # Bloqueo de la venta asociada
            venta = db.query(Venta).filter(
                Venta.id == pago.venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta asociada no encontrada")

            ahora = datetime.now(timezone.utc)
            conciliacion = ConciliacionPago(
                id=str(uuid.uuid4()),
                negocio_id=negocio_id,
                pago_id=pago.id,
                venta_id=pago.venta_id,
                usuario_id=usuario_id,
                fecha_conciliacion=ahora,
                motivo=motivo.strip(),
                referencia_documental=referencia_documental.strip(),
                estado=EstadoConciliacion.APROBADA.value,
                notas=notas.strip() if notas else None,
                pago=pago,
            )
            db.add(conciliacion)
            db.flush()

            # Amortizar saldo operativo en CuentaPorCobrar si existe
            cxc = db.query(CuentaPorCobrar).filter(
                CuentaPorCobrar.venta_id == venta.id,
                CuentaPorCobrar.negocio_id == negocio_id
            ).with_for_update().first()

            if cxc:
                cxc.saldo_pendiente = max(Decimal("0.00"), cxc.saldo_pendiente - pago.monto)
                if cxc.saldo_pendiente == Decimal("0.00"):
                    cxc.estado = "PAGADA"
                cxc.updated_at = ahora
                db.flush()

            # Recalcular estado financiero de la venta (transicionará a PAID si todos los cobros están verificados)
            cls._recalcular_estado_financiero_venta(db, venta)

            if commit:
                db.commit()
                db.refresh(conciliacion)
            else:
                db.flush()
            return conciliacion

        except Exception:
            if commit:
                db.rollback()
            raise

    @classmethod
    def consultar_historial(
        cls,
        db: Session,
        negocio_id: str,
        venta_id: str,
    ) -> dict:
        """
        Obtiene el desglose cronológico completo de cobros, reversiones y conciliaciones para una venta,
        discriminando entre cobros verificados y cobros históricos presuntivos.
        """
        venta = db.query(Venta).options(
            joinedload(Venta.pagos).joinedload(PagoVenta.reversiones),
            joinedload(Venta.pagos).joinedload(PagoVenta.conciliaciones),
            joinedload(Venta.cuenta_cobrar),
        ).filter(
            Venta.id == venta_id,
            Venta.negocio_id == negocio_id
        ).first()

        if not venta:
            raise ValueError("Venta no encontrada o no pertenece a su negocio")

        total_cobrado_verificado = Decimal("0.00")
        total_cobrado_presuntivo = Decimal("0.00")
        total_revertido = Decimal("0.00")
        items = []

        for p in sorted(venta.pagos, key=lambda x: x.fecha_pago):
            reversiones_pago = []
            rev_sum = Decimal("0.00")
            for r in p.reversiones:
                rev_sum += r.monto_revertido
                reversiones_pago.append({
                    "id": r.id,
                    "pago_id": r.pago_id,
                    "venta_id": r.venta_id,
                    "monto_revertido": r.monto_revertido,
                    "motivo": r.motivo,
                    "usuario_id": r.usuario_id,
                    "fecha_reversion": r.fecha_reversion,
                    "es_devolucion_efectivo": r.es_devolucion_efectivo,
                })

            conciliaciones_pago = []
            for c in p.conciliaciones:
                conciliaciones_pago.append({
                    "id": c.id,
                    "negocio_id": c.negocio_id,
                    "pago_id": c.pago_id,
                    "venta_id": c.venta_id,
                    "usuario_id": c.usuario_id,
                    "fecha_conciliacion": c.fecha_conciliacion,
                    "motivo": c.motivo,
                    "referencia_documental": c.referencia_documental,
                    "estado": c.estado,
                    "notas": c.notas,
                })

            neto = p.monto - rev_sum
            total_revertido += rev_sum

            if p.estado == EstadoPago.CONFIRMED.value:
                if p.es_cobro_verificado:
                    total_cobrado_verificado += neto
                else:
                    total_cobrado_presuntivo += neto

            items.append({
                "pago": {
                    "id": p.id,
                    "negocio_id": p.negocio_id,
                    "venta_id": p.venta_id,
                    "monto": p.monto,
                    "metodo_pago": p.metodo_pago,
                    "estado": p.estado,
                    "origen": p.origen,
                    "esta_conciliado": p.esta_conciliado,
                    "es_cobro_verificado": p.es_cobro_verificado,
                    "usuario_id": p.usuario_id,
                    "fecha_pago": p.fecha_pago,
                    "referencia": p.referencia,
                    "total_revertido": rev_sum,
                    "saldo_reversible": p.saldo_reversible,
                },
                "reversiones": reversiones_pago,
                "conciliaciones": conciliaciones_pago,
                "neto_cobrado": neto,
                "es_historico_presuntivo": p.es_historico_presuntivo,
                "es_cobro_verificado": p.es_cobro_verificado,
            })

        # REGLA CANÓNICA FASE 2.2.2:
        # La cartera y saldo operativo consideran EXCLUSIVAMENTE cobros verificados netos.
        # Los cobros históricos presuntivos se informan por separado y NO reducen automáticamente la deuda.
        saldo_operativo_pendiente = max(Decimal("0.00"), venta.total - total_cobrado_verificado)
        importe_presuntivo_pendiente = total_cobrado_presuntivo

        return {
            "venta_id": venta.id,
            "numero_ticket": venta.numero,
            "total_venta": venta.total,
            "total_cobrado_verificado": total_cobrado_verificado,
            "total_cobrado_presuntivo": total_cobrado_presuntivo,
            "total_revertido": total_revertido,
            "saldo_operativo_pendiente": saldo_operativo_pendiente,
            "importe_presuntivo_pendiente": importe_presuntivo_pendiente,
            "saldo_pendiente": saldo_operativo_pendiente,
            "estado_operativo": venta.estado,
            "estado_financiero": venta.estado_financiero,
            "pagos": items,
        }

    # -----------------------------------------------------------------------
    # Métodos Internos de Cálculo
    # -----------------------------------------------------------------------
    @classmethod
    def _aplicar_efecto_pago_confirmado(
        cls,
        db: Session,
        negocio_id: str,
        venta: Venta,
        monto: Decimal,
    ) -> None:
        """Aplica el abono a la cuenta por cobrar y recalcula el estado financiero."""
        cxc = db.query(CuentaPorCobrar).filter(
            CuentaPorCobrar.venta_id == venta.id,
            CuentaPorCobrar.negocio_id == negocio_id
        ).with_for_update().first()

        if cxc:
            cxc.saldo_pendiente = max(Decimal("0.00"), cxc.saldo_pendiente - monto)
            if cxc.saldo_pendiente == Decimal("0.00"):
                cxc.estado = "PAGADA"
            cxc.updated_at = datetime.now(timezone.utc)
            db.flush()

        cls._recalcular_estado_financiero_venta(db, venta)

    @classmethod
    def _recalcular_estado_financiero_venta(cls, db: Session, venta: Venta) -> None:
        """
        Deriva canónicamente el estado financiero según cobros netos confirmados y su procedencia:
        - NOT_APPLICABLE: Venta cancelada o sin obligación.
        - PAID: Cobros netos VERIFICADOS cubren el 100% de la venta.
        - PAID_PRESUNTIVO: Venta histórica de contado cubierta al 100% por cobros HISTORICO_PRESUNTIVO no conciliados.
        - PARTIAL: Existen cobros confirmados netos mayores a 0 pero menores al total.
        - CREDIT: Venta a crédito con cartera activa y sin cobros confirmados netos.
        """
        if venta.estado == EstadoVenta.CANCELADA.value:
            venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value
            return

        pagos = db.query(PagoVenta).filter(
            PagoVenta.venta_id == venta.id,
            PagoVenta.negocio_id == venta.negocio_id,
            PagoVenta.estado == EstadoPago.CONFIRMED.value
        ).all()

        if not pagos:
            if venta.cuenta_cobrar or venta.metodo_pago == MetodoPago.CREDITO.value:
                venta.estado_financiero = EstadoFinanciero.CREDIT.value
            else:
                venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value
            db.flush()
            return

        # Reversiones agrupadas por pago_id para esta venta
        reversiones_por_pago = dict(
            db.query(
                ReversionPago.pago_id,
                func.coalesce(func.sum(ReversionPago.monto_revertido), Decimal("0.00"))
            ).filter(
                ReversionPago.venta_id == venta.id,
                ReversionPago.negocio_id == venta.negocio_id
            ).group_by(ReversionPago.pago_id).all()
        )

        # Pagos con conciliación aprobada
        conciliados_ids = {
            row[0] for row in db.query(ConciliacionPago.pago_id).filter(
                ConciliacionPago.venta_id == venta.id,
                ConciliacionPago.negocio_id == venta.negocio_id,
                ConciliacionPago.estado == EstadoConciliacion.APROBADA.value
            ).all()
        }

        total_verificado = Decimal("0.00")
        total_presuntivo = Decimal("0.00")

        for p in pagos:
            revertido = Decimal(str(reversiones_por_pago.get(p.id, Decimal("0.00"))))
            neto = max(Decimal("0.00"), Decimal(str(p.monto)) - revertido)
            if neto <= Decimal("0.00"):
                continue

            es_verificado = (p.origen == OrigenPago.OPERATIVO.value) or (p.id in conciliados_ids)
            if es_verificado:
                total_verificado += neto
            else:
                total_presuntivo += neto

        total_cobrado_neto = total_verificado + total_presuntivo

        if total_verificado >= venta.total:
            venta.estado_financiero = EstadoFinanciero.PAID.value
        elif total_cobrado_neto >= venta.total:
            if total_verificado == Decimal("0.00"):
                venta.estado_financiero = EstadoFinanciero.PAID_PRESUNTIVO.value
            else:
                venta.estado_financiero = EstadoFinanciero.PARTIAL.value
        elif total_cobrado_neto > Decimal("0.00"):
            venta.estado_financiero = EstadoFinanciero.PARTIAL.value
        elif venta.cuenta_cobrar or venta.metodo_pago == MetodoPago.CREDITO.value:
            venta.estado_financiero = EstadoFinanciero.CREDIT.value
        else:
            venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value

        db.flush()
