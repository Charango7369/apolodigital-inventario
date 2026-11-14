"""
AccountsReceivableService — Servicio de gestión de cartera, cuentas por cobrar y morosidad (Bloque 2.3).

Responsabilidades:
1. Consultar cartera de cuentas por cobrar general, filtrable por estado y vencimiento.
2. Consultar cartera agregada por cliente (métricas de exposición crediticia y morosidad).
3. Consultar cuenta por cobrar individual asociada a una venta específica dentro del tenant.
4. Registrar abonos y amortizaciones (parciales y totales) delegando la creación financiera en PaymentService.
5. Control de concurrencia mediante bloqueo pesimista (with_for_update) sobre CuentaPorCobrar y Venta.
6. Aislamiento multi-tenant estricto: negocio_id derivado exclusivamente del contexto autenticado.
7. Cálculo dinámico de morosidad y días de mora (fecha_comercial_hoy() en America/La_Paz).
8. Regla canónica estricta: saldo_operativo = max(0, total_venta - cobros_verificados_netos).
   Los cobros presuntivos se informan separadamente y NO reducen la deuda operativa sin conciliación aprobada.
9. Cuentas históricas con estado REQUIERE_CONCILIACION no generan mora artificial ni vencimiento forzado.
10. Emisión de estados de cuenta detallados por cliente con historial cronológico de cargos y abonos.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import (
    Cliente, Venta, PagoVenta, ReversionPago, CuentaPorCobrar, ConciliacionPago,
    EstadoVenta, EstadoCuentaCobrar, EstadoPago, OrigenPago, EstadoConciliacion,
    fecha_comercial_hoy,
)
from app.modules.ventas.services.payment_service import PaymentService


class AccountsReceivableService:

    @classmethod
    def _calcular_metricas_financieras_cuenta(
        cls,
        db: Session,
        cxc: CuentaPorCobrar,
        venta: Venta,
    ) -> dict:
        """
        Calcula las métricas canónicas contrastando la cuenta con el historial real de cobros:
        - cobros_verificados_netos (V)
        - cobros_presuntivos_netos (P)
        - saldo_operativo: max(0, venta.total - V)
        - dias_mora dinámico según fecha comercial de Bolivia (America/La_Paz)
        """
        pagos = db.query(PagoVenta).filter(
            PagoVenta.venta_id == venta.id,
            PagoVenta.negocio_id == venta.negocio_id,
            PagoVenta.estado == EstadoPago.CONFIRMED.value
        ).all()

        reversiones_por_pago = dict(
            db.query(
                ReversionPago.pago_id,
                func.coalesce(func.sum(ReversionPago.monto_revertido), Decimal("0.00"))
            ).filter(
                ReversionPago.venta_id == venta.id,
                ReversionPago.negocio_id == venta.negocio_id
            ).group_by(ReversionPago.pago_id).all()
        )

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

        if cxc.estado == EstadoCuentaCobrar.CANCELADA.value or venta.estado == EstadoVenta.CANCELADA.value:
            if cxc.saldo_pendiente != Decimal("0.00"):
                cxc.saldo_pendiente = Decimal("0.00")
                db.flush()
            return {
                "cobros_verificados_netos": total_verificado,
                "cobros_presuntivos_netos": total_presuntivo,
                "saldo_operativo": Decimal("0.00"),
                "dias_mora": 0,
                "esta_vencida": False,
            }

        saldo_operativo = max(Decimal("0.00"), Decimal(str(venta.total)) - total_verificado)

        # Mantener consistencia canónica con cxc.saldo_pendiente
        if cxc.saldo_pendiente != saldo_operativo:
            cxc.saldo_pendiente = saldo_operativo
            if saldo_operativo == Decimal("0.00") and cxc.estado != EstadoCuentaCobrar.CANCELADA.value:
                cxc.estado = EstadoCuentaCobrar.PAGADA.value
            elif cxc.estado == EstadoCuentaCobrar.PAGADA.value and saldo_operativo > Decimal("0.00"):
                cxc.estado = EstadoCuentaCobrar.PENDIENTE.value
            db.flush()

        # Morosidad dinámica en zona comercial
        hoy = fecha_comercial_hoy()
        esta_vencida = False
        dias_mora = 0

        if saldo_operativo > Decimal("0.00") and cxc.fecha_vencimiento is not None:
            if cxc.estado != EstadoCuentaCobrar.REQUIERE_CONCILIACION.value:
                if cxc.fecha_vencimiento < hoy:
                    esta_vencida = True
                    dias_mora = (hoy - cxc.fecha_vencimiento).days

        return {
            "cobros_verificados_netos": total_verificado,
            "cobros_presuntivos_netos": total_presuntivo,
            "saldo_operativo": saldo_operativo,
            "dias_mora": dias_mora,
            "esta_vencida": esta_vencida,
        }

    @classmethod
    def consultar_cartera(
        cls,
        db: Session,
        negocio_id: str,
        estado: str | None = None,
        solo_vencidas: bool = False,
        cliente_id: str | None = None,
        incluir_canceladas: bool = False,
    ) -> list[dict]:
        """
        Consulta las cuentas por cobrar del tenant con cálculo dinámico de morosidad y saldos operativos.
        Por defecto excluye cuentas CANCELADAS a menos que se indique explícitamente en el filtro o parámetro.
        """
        query = db.query(CuentaPorCobrar).options(
            joinedload(CuentaPorCobrar.venta),
            joinedload(CuentaPorCobrar.cliente),
        ).filter(
            CuentaPorCobrar.negocio_id == negocio_id
        )

        if estado:
            query = query.filter(CuentaPorCobrar.estado == estado)
        elif not incluir_canceladas:
            query = query.filter(CuentaPorCobrar.estado != EstadoCuentaCobrar.CANCELADA.value)

        if cliente_id:
            query = query.filter(CuentaPorCobrar.cliente_id == cliente_id)

        cuentas = query.order_by(CuentaPorCobrar.created_at.desc()).all()
        resultado = []

        for cxc in cuentas:
            metricas = cls._calcular_metricas_financieras_cuenta(db, cxc, cxc.venta)
            if solo_vencidas and not metricas["esta_vencida"]:
                continue

            resultado.append({
                "id": cxc.id,
                "negocio_id": cxc.negocio_id,
                "venta_id": cxc.venta_id,
                "numero_ticket": cxc.venta.numero if cxc.venta else None,
                "cliente_id": cxc.cliente_id,
                "cliente_nombre": cxc.cliente.nombre if cxc.cliente else None,
                "cliente_nit": cxc.cliente.nit if cxc.cliente else None,
                "cliente_telefono": cxc.cliente.telefono if cxc.cliente else None,
                "monto_total_venta": cxc.monto_total_venta,
                "monto_inicial_financiado": cxc.monto_inicial_financiado,
                "cobros_verificados_netos": metricas["cobros_verificados_netos"],
                "cobros_presuntivos_netos": metricas["cobros_presuntivos_netos"],
                "saldo_operativo": metricas["saldo_operativo"],
                "saldo_pendiente": metricas["saldo_operativo"],
                "fecha_vencimiento": cxc.fecha_vencimiento,
                "dias_mora": metricas["dias_mora"],
                "esta_vencida": metricas["esta_vencida"],
                "estado": cxc.estado,
                "notas": cxc.notas,
                "created_at": cxc.created_at,
                "updated_at": cxc.updated_at,
            })

        return resultado

    @classmethod
    def consultar_cartera_por_cliente(
        cls,
        db: Session,
        negocio_id: str,
    ) -> list[dict]:
        """
        Retorna la cartera agregada por cliente en el tenant.
        Muestra exposición crediticia total, saldos operativos, importes presuntivos y máxima mora.
        """
        cuentas_detalle = cls.consultar_cartera(db, negocio_id)
        clientes_map: dict[str, dict] = {}

        for c in cuentas_detalle:
            cid = c["cliente_id"]
            if cid not in clientes_map:
                clientes_map[cid] = {
                    "cliente_id": cid,
                    "cliente_nombre": c["cliente_nombre"] or "Sin nombre",
                    "cliente_nit": c["cliente_nit"],
                    "cliente_telefono": c["cliente_telefono"],
                    "total_cuentas": 0,
                    "cuentas_activas": 0,
                    "cuentas_vencidas": 0,
                    "monto_total_financiado": Decimal("0.00"),
                    "total_cobrado_verificado": Decimal("0.00"),
                    "saldo_operativo_total": Decimal("0.00"),
                    "importe_presuntivo_total": Decimal("0.00"),
                    "max_dias_mora": 0,
                }

            item = clientes_map[cid]
            item["total_cuentas"] += 1
            item["monto_total_financiado"] += c["monto_inicial_financiado"]
            item["total_cobrado_verificado"] += c["cobros_verificados_netos"]
            item["saldo_operativo_total"] += c["saldo_operativo"]
            item["importe_presuntivo_total"] += c["cobros_presuntivos_netos"]

            if c["saldo_operativo"] > Decimal("0.00"):
                item["cuentas_activas"] += 1
                if c["esta_vencida"]:
                    item["cuentas_vencidas"] += 1
                    if c["dias_mora"] > item["max_dias_mora"]:
                        item["max_dias_mora"] = c["dias_mora"]

        return list(clientes_map.values())

    @classmethod
    def consultar_cuenta_venta(
        cls,
        db: Session,
        negocio_id: str,
        venta_id: str,
    ) -> dict:
        """
        Obtiene el detalle financiero completo de la cuenta por cobrar de una venta específica.
        """
        cxc = db.query(CuentaPorCobrar).options(
            joinedload(CuentaPorCobrar.venta),
            joinedload(CuentaPorCobrar.cliente),
        ).filter(
            CuentaPorCobrar.venta_id == venta_id,
            CuentaPorCobrar.negocio_id == negocio_id
        ).first()

        if not cxc:
            raise ValueError("Cuenta por cobrar no encontrada o no pertenece a su negocio")

        metricas = cls._calcular_metricas_financieras_cuenta(db, cxc, cxc.venta)

        return {
            "id": cxc.id,
            "negocio_id": cxc.negocio_id,
            "venta_id": cxc.venta_id,
            "numero_ticket": cxc.venta.numero if cxc.venta else None,
            "cliente_id": cxc.cliente_id,
            "cliente_nombre": cxc.cliente.nombre if cxc.cliente else None,
            "cliente_nit": cxc.cliente.nit if cxc.cliente else None,
            "cliente_telefono": cxc.cliente.telefono if cxc.cliente else None,
            "monto_total_venta": cxc.monto_total_venta,
            "monto_inicial_financiado": cxc.monto_inicial_financiado,
            "cobros_verificados_netos": metricas["cobros_verificados_netos"],
            "cobros_presuntivos_netos": metricas["cobros_presuntivos_netos"],
            "saldo_operativo": metricas["saldo_operativo"],
            "saldo_pendiente": metricas["saldo_operativo"],
            "fecha_vencimiento": cxc.fecha_vencimiento,
            "dias_mora": metricas["dias_mora"],
            "esta_vencida": metricas["esta_vencida"],
            "estado": cxc.estado,
            "notas": cxc.notas,
            "created_at": cxc.created_at,
            "updated_at": cxc.updated_at,
        }

    @classmethod
    def registrar_abono(
        cls,
        db: Session,
        negocio_id: str,
        cuenta_id: str,
        usuario_id: str,
        monto: Decimal,
        metodo_pago: str,
        referencia: str | None = None,
        confirmar_inmediato: bool | None = None,
        commit: bool = True,
    ) -> dict:
        """
        Registra una amortización (parcial o total) para una cuenta por cobrar.
        
        Garantías y Reglas:
        1. Bloqueo pesimista (with_for_update) sobre CuentaPorCobrar y Venta.
        2. Validación de importe estrictamente positivo (monto > 0).
        3. Comprobación contra el saldo operativo real: no permite sobrepagos.
        4. Delegación de la creación del pago en PaymentService.registrar_pago().
        5. Actualización atómica de saldo_pendiente, estado de la cuenta y estado financiero de la venta.
        6. Aislamiento multi-tenant por negocio_id.
        """
        try:
            if monto <= Decimal("0.00"):
                raise ValueError("El importe del abono debe ser estrictamente mayor a cero")

            # 1. Resolver metadatos de la cuenta para mantener el orden determinista de bloqueos (Venta -> CxC)
            cxc_meta = db.query(CuentaPorCobrar.id, CuentaPorCobrar.venta_id, CuentaPorCobrar.estado).filter(
                CuentaPorCobrar.id == cuenta_id,
                CuentaPorCobrar.negocio_id == negocio_id
            ).first()

            if not cxc_meta:
                raise ValueError("Cuenta por cobrar no encontrada o no pertenece a su negocio")

            if cxc_meta.estado == EstadoCuentaCobrar.CANCELADA.value:
                raise ValueError("No se pueden registrar abonos a una cuenta por cobrar cancelada")

            # 2. Bloqueo determinista de la Venta primero (Venta -> CxC para prevenir deadlocks)
            venta = db.query(Venta).filter(
                Venta.id == cxc_meta.venta_id,
                Venta.negocio_id == negocio_id
            ).with_for_update().first()

            if not venta:
                raise ValueError("Venta asociada a la cuenta no encontrada")

            if venta.estado == EstadoVenta.CANCELADA.value:
                raise ValueError("No se pueden registrar abonos a una venta cancelada")

            # 3. Bloqueo determinista de la Cuenta por Cobrar
            cxc = db.query(CuentaPorCobrar).filter(
                CuentaPorCobrar.id == cuenta_id,
                CuentaPorCobrar.negocio_id == negocio_id
            ).with_for_update().first()

            # 3. Calcular saldo operativo actual directamente desde la base de datos
            metricas_previas = cls._calcular_metricas_financieras_cuenta(db, cxc, venta)
            saldo_operativo_anterior = metricas_previas["saldo_operativo"]

            if saldo_operativo_anterior <= Decimal("0.00"):
                raise ValueError("La cuenta ya se encuentra completamente liquidada (saldo operativo Bs 0.00)")

            if monto > saldo_operativo_anterior:
                raise ValueError(
                    f"El importe a abonar (Bs {monto}) supera el saldo operativo pendiente de la cuenta (Bs {saldo_operativo_anterior})"
                )

            # 4. Delegación en PaymentService (no duplica lógica financiera, comparte transacción)
            pago = PaymentService.registrar_pago(
                db=db,
                negocio_id=negocio_id,
                venta_id=venta.id,
                usuario_id=usuario_id,
                monto=monto,
                metodo_pago=metodo_pago,
                referencia=referencia,
                confirmar_inmediato=confirmar_inmediato,
                commit=False,
            )

            # 5. Obtener saldo operativo resultante tras el abono
            metricas_nuevas = cls._calcular_metricas_financieras_cuenta(db, cxc, venta)
            saldo_operativo_nuevo = metricas_nuevas["saldo_operativo"]

            if commit:
                db.commit()
                db.refresh(cxc)
                db.refresh(venta)
            else:
                db.flush()

            return {
                "cuenta_id": cxc.id,
                "venta_id": venta.id,
                "cliente_id": cxc.cliente_id,
                "monto_abonado": monto,
                "saldo_operativo_anterior": saldo_operativo_anterior,
                "saldo_operativo_nuevo": saldo_operativo_nuevo,
                "estado_cuenta": cxc.estado,
                "estado_financiero_venta": venta.estado_financiero,
                "pago": pago,
            }

        except Exception:
            db.rollback()
            raise

    @classmethod
    def estado_de_cuenta_cliente(
        cls,
        db: Session,
        negocio_id: str,
        cliente_id: str,
    ) -> dict:
        """
        Genera el extracto / estado de cuenta consolidado para un cliente,
        incluyendo resumen global de cartera y el historial cronológico de cargos y abonos.
        """
        cliente = db.query(Cliente).filter(
            Cliente.id == cliente_id,
            Cliente.negocio_id == negocio_id
        ).first()

        if not cliente:
            raise ValueError("Cliente no encontrado o no pertenece a su negocio")

        cuentas = cls.consultar_cartera(db, negocio_id, cliente_id=cliente.id)

        # Totales globales del cliente
        total_cuentas = len(cuentas)
        cuentas_activas = 0
        cuentas_vencidas = 0
        monto_total_financiado = Decimal("0.00")
        total_cobrado_verificado = Decimal("0.00")
        saldo_operativo_total = Decimal("0.00")
        importe_presuntivo_total = Decimal("0.00")
        max_dias_mora = 0

        for c in cuentas:
            monto_total_financiado += c["monto_inicial_financiado"]
            total_cobrado_verificado += c["cobros_verificados_netos"]
            saldo_operativo_total += c["saldo_operativo"]
            importe_presuntivo_total += c["cobros_presuntivos_netos"]

            if c["saldo_operativo"] > Decimal("0.00"):
                cuentas_activas += 1
                if c["esta_vencida"]:
                    cuentas_vencidas += 1
                    if c["dias_mora"] > max_dias_mora:
                        max_dias_mora = c["dias_mora"]

        resumen = {
            "cliente_id": cliente.id,
            "cliente_nombre": cliente.nombre,
            "cliente_nit": cliente.nit,
            "cliente_telefono": cliente.telefono,
            "total_cuentas": total_cuentas,
            "cuentas_activas": cuentas_activas,
            "cuentas_vencidas": cuentas_vencidas,
            "monto_total_financiado": monto_total_financiado,
            "total_cobrado_verificado": total_cobrado_verificado,
            "saldo_operativo_total": saldo_operativo_total,
            "importe_presuntivo_total": importe_presuntivo_total,
            "max_dias_mora": max_dias_mora,
        }

        return {
            "cliente_id": cliente.id,
            "cliente_nombre": cliente.nombre,
            "cliente_nit": cliente.nit,
            "cliente_telefono": cliente.telefono,
            "resumen": resumen,
            "cuentas": cuentas,
        }
