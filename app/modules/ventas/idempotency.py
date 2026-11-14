"""
Módulo de gestión de idempotencia para operaciones financieras (Fase 2.5.1).

Arquitectura de Frontera Transaccional Única (Single Atomic Transaction):
1. Adquisición / Verificación de la clave idempotente en la base de datos.
2. Validación determinista del hash canónico SHA-256 de la solicitud.
3. Ejecución de la operación de negocio con `commit=False` (db.flush() interno).
4. Persistencia del resultado serializado y estado 'SUCCESS' en RegistroIdempotencia.
5. Un único `db.commit()` que confirma atómicamente la operación de negocio y la clave.

Garantías de Consistencia:
- Si ocurre una caída o interrupción antes del COMMIT: PostgreSQL/SQLite aborta y
  revierte tanto la operación financiera como el registro de idempotencia. Un reintento
  futuro encontrará la clave libre y ejecutará la operación sin duplicaciones ni bloqueos fantasma.
- Si ocurre una caída después del COMMIT: El reintento encontrará el registro 'SUCCESS'
  y devolverá la respuesta almacenada con la cabecera `X-Idempotent-Replay: true`.
- Ámbito de unicidad extendido: `(negocio_id, metodo, endpoint, idempotency_key)`.
  Evita colisiones entre distintos métodos o diferentes recursos que usen la misma clave.
"""

from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Any, Callable

from fastapi import HTTPException, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.ventas.models import RegistroIdempotencia


def canonical_request_hash(data: Any, query_params: dict | None = None) -> str:
    """
    Calcula un hash SHA-256 determinista a partir del cuerpo y parámetros normalizados.
    Garantiza ordenamiento de claves lexicográfico para evitar discrepancias de serialización.
    """
    payload_dict: dict[str, Any] = {}
    if data is not None:
        if hasattr(data, "model_dump"):
            payload_dict["body"] = jsonable_encoder(data.model_dump(mode="json"))
        elif isinstance(data, dict):
            payload_dict["body"] = jsonable_encoder(data)
        elif isinstance(data, (str, bytes)):
            payload_dict["body"] = str(data)
        else:
            payload_dict["body"] = jsonable_encoder(data)

    if query_params:
        payload_dict["query"] = jsonable_encoder(query_params)

    raw_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()


def _serialize_response_payload(resultado: Any) -> str:
    """Serializa la respuesta de negocio para caché de replay preservando tipos estándar."""
    if hasattr(resultado, "model_dump_json"):
        return resultado.model_dump_json()
    if hasattr(resultado, "model_dump"):
        return json.dumps(resultado.model_dump(mode="json"), default=str)
    if isinstance(resultado, dict):
        return json.dumps(jsonable_encoder(resultado), default=str)
    # Si es un modelo SQLAlchemy, extraer columnas y relaciones si están disponibles
    if hasattr(resultado, "__table__"):
        data = {c.name: getattr(resultado, c.name) for c in resultado.__table__.columns}
        for rel in ("detalles", "pagos", "cuenta_cobrar"):
            if hasattr(resultado, rel):
                val = getattr(resultado, rel)
                if val is not None:
                    if isinstance(val, list):
                        data[rel] = [
                            {c.name: getattr(item, c.name) for c in item.__table__.columns}
                            if hasattr(item, "__table__") else item
                            for item in val
                        ]
                    elif hasattr(val, "__table__"):
                        data[rel] = {c.name: getattr(val, c.name) for c in val.__table__.columns}
                else:
                    data[rel] = [] if rel in ("detalles", "pagos") else None
        return json.dumps(jsonable_encoder(data), default=str)
    return json.dumps(jsonable_encoder(resultado), default=str)


def ejecutar_con_idempotencia(
    db: Session,
    negocio_id: str,
    usuario_id: str,
    metodo: str,
    endpoint: str,
    idempotency_key: str | None,
    request_data: Any,
    action: Callable[[bool], Any],
    status_code: int = 200,
    expires_in_days: int = 7,
    query_params: dict | None = None,
) -> Any:
    """
    Ejecuta una acción financiera bajo una única frontera transaccional atómica.
    
    Parámetros:
    - action: Callable que recibe `commit: bool`. Cuando se ejecuta con idempotencia,
      se invoca con `commit=False` para sincronizar los cambios sin cerrar la transacción.
    - metodo: Método HTTP (ej. "POST", "PUT").
    - endpoint: Ruta concreta del recurso (ej. "/api/v1/ventas/123/pagos").
    """
    # Si no se envía clave de idempotencia, ejecutar con confirmación directa
    if not idempotency_key or not idempotency_key.strip():
        return action(commit=True)

    key_limpia = idempotency_key.strip()
    req_hash = canonical_request_hash(request_data, query_params)

    # 1. Comprobar si la clave ya existe (replay transparente o colisión de payload)
    record = db.query(RegistroIdempotencia).filter(
        RegistroIdempotencia.negocio_id == negocio_id,
        RegistroIdempotencia.metodo == metodo,
        RegistroIdempotencia.endpoint == endpoint,
        RegistroIdempotencia.idempotency_key == key_limpia,
    ).first()

    if record:
        # Detectar alteración de cuerpo con la misma llave
        if record.request_hash != req_hash:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key reused with different request payload"
            )

        if record.status == "PROCESSING":
            raise HTTPException(
                status_code=409,
                detail="A request with this idempotency key is currently being processed"
            )

        # Replay de respuesta previa exitosa
        if record.status in ("SUCCESS", "COMPLETED"):
            return Response(
                content=record.response_json or "{}",
                status_code=record.response_code or status_code,
                media_type="application/json",
                headers={"X-Idempotent-Replay": "true"}
            )

        if record.status == "FAILED":
            detail = "Previous request with this idempotency key failed"
            if record.response_json:
                try:
                    parsed = json.loads(record.response_json)
                    detail = parsed.get("detail", detail)
                except Exception:
                    pass
            raise HTTPException(status_code=record.response_code or 400, detail=detail)

    # 2. RESERVA ATÓMICA DE LA CLAVE PREVIA A LA LÓGICA FINANCIERA (Bloque B)
    # Insertar el registro con status='PROCESSING' y ejecutar db.flush() para adquirir
    # la reserva exclusiva en base de datos ANTES de cualquier cálculo o mutación financiera.
    ahora = datetime.now(timezone.utc)
    registro_reserva = RegistroIdempotencia(
        negocio_id=negocio_id,
        metodo=metodo,
        endpoint=endpoint,
        idempotency_key=key_limpia,
        usuario_id=usuario_id,
        request_hash=req_hash,
        status="PROCESSING",
        response_code=None,
        response_json=None,
        created_at=ahora,
        updated_at=ahora,
        expires_at=ahora + timedelta(days=expires_in_days),
    )
    db.add(registro_reserva)

    try:
        db.flush()
    except IntegrityError:
        # Colisión concurrente interceptada en la reserva atómica inicial
        db.rollback()

        # Consultar el registro consolidado por la transacción concurrente ganadora
        record_carrera = db.query(RegistroIdempotencia).filter(
            RegistroIdempotencia.negocio_id == negocio_id,
            RegistroIdempotencia.metodo == metodo,
            RegistroIdempotencia.endpoint == endpoint,
            RegistroIdempotencia.idempotency_key == key_limpia,
        ).first()

        if record_carrera:
            if record_carrera.request_hash != req_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key reused with different request payload"
                )
            if record_carrera.status in ("SUCCESS", "COMPLETED"):
                return Response(
                    content=record_carrera.response_json or "{}",
                    status_code=record_carrera.response_code or status_code,
                    media_type="application/json",
                    headers={"X-Idempotent-Replay": "true"}
                )
            if record_carrera.status == "PROCESSING":
                raise HTTPException(
                    status_code=409,
                    detail="A request with this idempotency key is currently being processed"
                )
            if record_carrera.status == "FAILED":
                detail = "Previous request with this idempotency key failed"
                if record_carrera.response_json:
                    try:
                        parsed = json.loads(record_carrera.response_json)
                        detail = parsed.get("detail", detail)
                    except Exception:
                        pass
                raise HTTPException(status_code=record_carrera.response_code or 400, detail=detail)
        raise HTTPException(
            status_code=409,
            detail="A request with this idempotency key was executed concurrently"
        )

    # 3. EJECUTAR LA OPERACIÓN FINANCIERA (Única frontera transaccional atómica)
    try:
        # 3.1 Invocar acción de negocio sin commit interno (commit=False)
        resultado = action(commit=False)

        # 3.2 Serializar respuesta para almacenamiento seguro
        json_str = _serialize_response_payload(resultado)

        # 3.3 Promover registro a SUCCESS en la misma transacción
        registro_reserva.status = "SUCCESS"
        registro_reserva.response_code = status_code
        registro_reserva.response_json = json_str
        registro_reserva.updated_at = datetime.now(timezone.utc)
        db.flush()

        # 3.4 COMMIT ÚNICO: Confirma al unísono la operación financiera y la llave
        db.commit()

        return Response(
            content=json_str,
            status_code=status_code,
            media_type="application/json"
        )

    except HTTPException:
        db.rollback()
        raise

    except ValueError as ve:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(ve))

    except Exception as ex:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(ex))
