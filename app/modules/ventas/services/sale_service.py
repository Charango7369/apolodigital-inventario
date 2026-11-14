"""
SaleService — Servicio de ventas del módulo POS con consistencia financiera (Fase 2.1).

Responsabilidades:
1. Crear ventas con consistencia transaccional y financiera completa.
2. Soportar ventas de contado, multi-pago (mixto), crédito puro y crédito con abono inicial.
3. Descontar existencias mediante el mecanismo FEFO existente sin romper la atomicidad.
4. Derivar automáticamente el estado financiero (PAID, PARTIAL, CREDIT, NOT_APPLICABLE).
5. Prevenir sobrepagos silenciosos y exigir cliente registrado para cualquier saldo financiado.
6. Manejar la frontera transaccional con rollback automático ante cualquier falla.
"""

from datetime import datetime, timezone, date
from decimal import Decimal
import uuid

from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, CuentaPorCobrar,
    EstadoVenta, EstadoFinanciero, MetodoPago, OrigenPago,
)
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate,
)
from app.modules.inventario.models import (
    Variante, Producto, Almacen,
)
from app.modules.inventario.service import (
    get_almacen_principal, get_or_create_stock, crear_movimiento,
)
from app.modules.inventario.schemas import MovimientoCreate


class SaleService:
    @staticmethod
    def get_siguiente_numero_venta(db: Session, negocio_id: str) -> int:
        from sqlalchemy import func
        result = db.query(func.max(Venta.numero)).filter(
            Venta.negocio_id == negocio_id
        ).scalar()
        return (result or 0) + 1

    @classmethod
    def crear_venta_financiera(
        cls,
        db: Session,
        negocio_id: str,
        usuario_id: str,
        data: VentaCreate,
        commit: bool = True,
    ) -> Venta:
        """
        Crea una venta con todas sus líneas, movimientos de inventario FEFO,
        registros de pago y cuenta por cobrar en una única frontera transaccional atómica.
        Si commit=False, realiza flush() dejando la confirmación al gestor transaccional externo.
        """
        try:
            # -------------------------------------------------------------------
            # 1. Resolver almacén
            # -------------------------------------------------------------------
            if data.almacen_id:
                almacen = db.query(Almacen).filter(
                    Almacen.id == data.almacen_id,
                    Almacen.negocio_id == negocio_id
                ).first()
                if not almacen:
                    raise ValueError("Almacén no encontrado o no pertenece a su negocio")
            else:
                almacen = get_almacen_principal(db, negocio_id)
                if not almacen:
                    raise ValueError("No hay almacén principal configurado")

            # -------------------------------------------------------------------
            # 2. Validar cliente (IDOR prevention)
            # -------------------------------------------------------------------
            cliente: Cliente | None = None
            cliente_nombre = data.cliente_nombre

            if data.cliente_id:
                cliente = db.query(Cliente).filter(
                    Cliente.id == data.cliente_id,
                    Cliente.negocio_id == negocio_id,
                    Cliente.activo == True
                ).first()
                if not cliente:
                    raise ValueError("Cliente no encontrado, inactivo o no pertenece a su negocio")
                if not cliente_nombre:
                    cliente_nombre = cliente.nombre

            # -------------------------------------------------------------------
            # 3. Validar líneas de detalle y calcular importes con Decimal
            # -------------------------------------------------------------------
            if not data.detalles:
                raise ValueError("La venta debe contener al menos un producto")

            lineas_procesadas = []
            subtotal = Decimal("0.00")

            for linea in data.detalles:
                if linea.cantidad <= Decimal("0"):
                    raise ValueError(f"La cantidad debe ser mayor a cero para la variante {linea.variante_id}")

                variante = db.query(Variante).join(Producto).filter(
                    Variante.id == linea.variante_id,
                    Producto.negocio_id == negocio_id,
                    Variante.activa == True,
                    Producto.activo == True
                ).first()

                if not variante:
                    raise ValueError(f"Variante {linea.variante_id} no encontrada o inactiva")

                precio = linea.precio_unitario if linea.precio_unitario is not None else variante.precio_venta
                if precio < Decimal("0"):
                    raise ValueError("El precio unitario no puede ser negativo")

                descuento_linea = linea.descuento_linea or Decimal("0.00")
                if descuento_linea < Decimal("0"):
                    raise ValueError("El descuento de línea no puede ser negativo")

                subtotal_linea = (precio * linea.cantidad) - descuento_linea
                if subtotal_linea < Decimal("0"):
                    raise ValueError(f"El subtotal de la línea para {variante.producto.nombre} no puede ser negativo")

                subtotal += subtotal_linea
                lineas_procesadas.append((linea, variante, precio, subtotal_linea, descuento_linea))

            descuento_global = data.descuento or Decimal("0.00")
            if descuento_global < Decimal("0"):
                raise ValueError("El descuento global no puede ser negativo")

            total = subtotal - descuento_global
            if total < Decimal("0"):
                raise ValueError("El total de la venta no puede ser negativo tras aplicar descuentos")

            # -------------------------------------------------------------------
            # 4. Resolver pagos y saldo financiado
            # -------------------------------------------------------------------
            pagos_a_crear: list[PagoCreate] = []
            saldo_financiado = Decimal("0.00")
            cambio = Decimal("0.00")

            if data.pagos:
                # Flujo multi-pago explícito (Fase 2)
                suma_declarada = Decimal("0.00")
                for p in data.pagos:
                    if p.monto <= Decimal("0"):
                        raise ValueError("Los importes de pago deben ser estrictamente mayores a cero")
                    suma_declarada += p.monto
                    pagos_a_crear.append(p)

                if suma_declarada > total:
                    raise ValueError(
                        f"La suma de los pagos declarados (Bs {suma_declarada}) no puede superar "
                        f"el total de la venta (Bs {total})"
                    )

                saldo_financiado = total - suma_declarada

                if saldo_financiado > Decimal("0"):
                    if not data.es_credito and data.metodo_pago != MetodoPago.CREDITO.value:
                        raise ValueError(
                            f"Existe un saldo pendiente de Bs {saldo_financiado} pero la venta "
                            "no fue indicada a crédito"
                        )
                    if not cliente:
                        raise ValueError("Para otorgar crédito es obligatorio seleccionar un cliente registrado activo")

            else:
                # Flujo tradicional POS (Compatibilidad con frontend y clientes simples)
                if data.es_credito or data.metodo_pago == MetodoPago.CREDITO.value:
                    if not cliente:
                        raise ValueError("Para otorgar crédito es obligatorio seleccionar un cliente registrado activo")

                    if data.monto_recibido and data.monto_recibido > Decimal("0"):
                        if data.monto_recibido >= total:
                            raise ValueError(
                                f"El abono inicial (Bs {data.monto_recibido}) no puede ser igual "
                                f"o superior al total de una venta a crédito (Bs {total})"
                            )
                        # Abono inicial confirmado en efectivo
                        pagos_a_crear.append(
                            PagoCreate(
                                monto=data.monto_recibido,
                                metodo_pago="EFECTIVO",
                                estado="CONFIRMED",
                                referencia="ABONO_INICIAL_POS"
                            )
                        )
                        saldo_financiado = total - data.monto_recibido
                    else:
                        # Crédito 100% financiado
                        saldo_financiado = total
                else:
                    # Venta de contado pura (EFECTIVO, QR, TARJETA, TRANSFERENCIA)
                    metodo = data.metodo_pago if data.metodo_pago in ("EFECTIVO", "QR", "TARJETA", "TRANSFERENCIA") else "EFECTIVO"
                    pagos_a_crear.append(
                        PagoCreate(
                            monto=total,
                            metodo_pago=metodo,
                            estado="CONFIRMED",
                            referencia="PAGO_CONTADO_POS"
                        )
                    )
                    saldo_financiado = Decimal("0.00")

                    if data.monto_recibido and data.monto_recibido > total:
                        cambio = data.monto_recibido - total

            # -------------------------------------------------------------------
            # 5. Instanciar Cabecera de Venta
            # -------------------------------------------------------------------
            venta_id = str(uuid.uuid4())
            numero = cls.get_siguiente_numero_venta(db, negocio_id)
            ahora = datetime.now(timezone.utc)

            # Resolver método de pago general para vista previa
            if len(pagos_a_crear) > 1:
                metodo_cabecera = MetodoPago.MIXTO.value
            elif len(pagos_a_crear) == 1:
                metodo_cabecera = pagos_a_crear[0].metodo_pago
            elif data.es_credito or saldo_financiado > Decimal("0"):
                metodo_cabecera = MetodoPago.CREDITO.value
            else:
                metodo_cabecera = data.metodo_pago

            venta = Venta(
                id=venta_id,
                negocio_id=negocio_id,
                almacen_id=almacen.id,
                numero=numero,
                cliente_id=cliente.id if cliente else None,
                cliente_nombre=cliente_nombre,
                cliente_nit=data.cliente_nit,
                subtotal=subtotal,
                descuento=descuento_global,
                total=total,
                metodo_pago=metodo_cabecera,
                monto_recibido=data.monto_recibido,
                cambio=cambio if cambio > Decimal("0") else None,
                estado=EstadoVenta.PENDIENTE.value,
                estado_financiero=EstadoFinanciero.NOT_APPLICABLE.value,
                usuario_id=usuario_id,
                notas=data.notas,
                created_at=ahora,
            )
            db.add(venta)
            db.flush()

            # -------------------------------------------------------------------
            # 6. Crear detalles de venta
            # -------------------------------------------------------------------
            for linea, variante, precio, subtotal_linea, desc_linea in lineas_procesadas:
                detalle = DetalleVenta(
                    id=str(uuid.uuid4()),
                    venta_id=venta.id,
                    variante_id=variante.id,
                    producto_nombre=variante.producto.nombre,
                    variante_sku=variante.sku,
                    cantidad=linea.cantidad,
                    precio_unitario=precio,
                    descuento_linea=desc_linea,
                    subtotal=subtotal_linea,
                    costo_unitario=variante.precio_costo,
                )
                db.add(detalle)

            db.flush()

            # -------------------------------------------------------------------
            # 7. Ejecutar descuento de stock FEFO si completar=True
            # -------------------------------------------------------------------
            if data.completar:
                for linea, variante, _, _, _ in lineas_procesadas:
                    if variante.producto.es_servicio:
                        continue  # Servicios no descuentan stock

                    # Pre-chequeo de stock
                    stock = get_or_create_stock(db, variante.id, almacen.id)
                    if stock.cantidad_actual < linea.cantidad:
                        raise ValueError(
                            f"Stock insuficiente para {variante.producto.nombre}. "
                            f"Disponible: {stock.cantidad_actual}, Solicitado: {linea.cantidad}"
                        )

                    # Descuento FEFO (crear_movimiento no hace commit)
                    mov_data = MovimientoCreate(
                        variante_id=variante.id,
                        almacen_id=almacen.id,
                        tipo="SALIDA_VENTA",
                        cantidad=linea.cantidad,
                        referencia_id=str(venta.id),
                        motivo=f"Venta #{venta.numero}",
                    )
                    crear_movimiento(db, negocio_id, mov_data, usuario_id)

                venta.estado = EstadoVenta.COMPLETADA.value
                venta.completed_at = ahora

            # -------------------------------------------------------------------
            # 8. Persistir pagos_venta
            # -------------------------------------------------------------------
            total_confirmado = Decimal("0.00")
            for p in pagos_a_crear:
                pago_id = str(uuid.uuid4())
                estado_pago = getattr(p, "estado", None)
                if not estado_pago:
                    estado_pago = "CONFIRMED" if p.metodo_pago in ("EFECTIVO", "TARJETA") else "PENDING"

                pago_entity = PagoVenta(
                    id=pago_id,
                    negocio_id=negocio_id,
                    venta_id=venta.id,
                    monto=p.monto,
                    metodo_pago=p.metodo_pago,
                    estado=estado_pago,
                    origen=OrigenPago.OPERATIVO.value,
                    usuario_id=usuario_id,
                    fecha_pago=ahora,
                    referencia=p.referencia,
                )
                db.add(pago_entity)
                if estado_pago == "CONFIRMED":
                    total_confirmado += p.monto

            db.flush()

            # -------------------------------------------------------------------
            # 9. Persistir cuentas_por_cobrar si hay saldo financiado
            # -------------------------------------------------------------------
            if saldo_financiado > Decimal("0"):
                cxc_id = str(uuid.uuid4())
                cxc_entity = CuentaPorCobrar(
                    id=cxc_id,
                    negocio_id=negocio_id,
                    venta_id=venta.id,
                    cliente_id=cliente.id,  # Validado como no nulo previamente
                    monto_total_venta=total,
                    monto_inicial_financiado=saldo_financiado,
                    saldo_pendiente=saldo_financiado,
                    fecha_vencimiento=data.fecha_vencimiento,
                    estado="PENDIENTE",
                    notas=data.notas,
                    created_at=ahora,
                    updated_at=ahora,
                )
                db.add(cxc_entity)
                db.flush()

            # -------------------------------------------------------------------
            # 10. Derivar estado financiero canónico
            # -------------------------------------------------------------------
            if venta.estado == EstadoVenta.CANCELADA.value:
                venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value
            elif total_confirmado >= total:
                venta.estado_financiero = EstadoFinanciero.PAID.value
            elif total_confirmado > Decimal("0"):
                venta.estado_financiero = EstadoFinanciero.PARTIAL.value
            elif saldo_financiado > Decimal("0") or data.es_credito:
                venta.estado_financiero = EstadoFinanciero.CREDIT.value
            else:
                venta.estado_financiero = EstadoFinanciero.NOT_APPLICABLE.value

            # -------------------------------------------------------------------
            # 11. Cierre de transacción atómica
            # -------------------------------------------------------------------
            if commit:
                db.commit()
            else:
                db.flush()

            # Carga completa para respuesta
            return db.query(Venta).options(
                joinedload(Venta.detalles),
                joinedload(Venta.cliente),
                joinedload(Venta.pagos),
                joinedload(Venta.cuenta_cobrar),
            ).filter(
                Venta.id == venta.id,
                Venta.negocio_id == negocio_id
            ).first()

        except Exception:
            if commit:
                db.rollback()
            raise
