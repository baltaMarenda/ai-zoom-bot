"""
credentials_pool.py
Pool de credenciales MGW para multi-tenant.

Cada llamada concurrente necesita un sistema MGW distinto (no se puede tener dos
bots sobre el mismo login). Este módulo administra EXCLUSIVAMENTE las credenciales:
acquire() entrega el primer slot libre en orden, release() lo devuelve. No sabe nada
de sesiones, bots ni Recall — eso es responsabilidad del SessionManager.
"""
import asyncio
from dataclasses import dataclass, field


@dataclass
class CredentialSlot:
    """Un sistema MGW del pool. `busy` indica si está en uso por alguna sesión."""
    index: int
    empresa: str
    usuario: str
    password: str
    alias: str
    busy: bool = False
    holder_sid: str | None = None  # qué sesión lo tiene tomado (para debugging)


class CredentialPool:
    """
    Administra el pool de credenciales MGW. Thread-safe vía asyncio.Lock.

    Uso:
        pool = CredentialPool(config.MGW_CREDENTIALS)
        slot = await pool.acquire(sid)   # None si no hay libres
        ...
        await pool.release(slot)
    """

    def __init__(self, credentials: list[dict]):
        self._slots: list[CredentialSlot] = [
            CredentialSlot(
                index=i,
                empresa=c["empresa"],
                usuario=c["usuario"],
                password=c["password"],
                alias=c.get("alias") or c["empresa"],
            )
            for i, c in enumerate(credentials)
        ]
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._slots)

    async def acquire(self, sid: str | None = None) -> CredentialSlot | None:
        """Toma el primer slot libre en orden. Devuelve None si están todos ocupados."""
        async with self._lock:
            for slot in self._slots:
                if not slot.busy:
                    slot.busy = True
                    slot.holder_sid = sid
                    return slot
            return None

    async def release(self, slot: CredentialSlot | None) -> None:
        """Devuelve un slot al pool. Idempotente: liberar dos veces no rompe."""
        if slot is None:
            return
        async with self._lock:
            slot.busy = False
            slot.holder_sid = None

    def free_count(self) -> int:
        return sum(1 for s in self._slots if not s.busy)

    def status(self) -> list[dict]:
        """Estado de cada slot, para GET /pool/status."""
        return [
            {
                "index": s.index,
                "alias": s.alias,
                "empresa": s.empresa,
                "usuario": s.usuario,
                "busy": s.busy,
                "holder_sid": s.holder_sid,
            }
            for s in self._slots
        ]
