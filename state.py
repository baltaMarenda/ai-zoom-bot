"""
state.py
State machine de la conversación de Malena.
"""
from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    INTRO        = "intro"
    CALIFICACION = "calificacion"
    DEMO         = "demo"
    CIERRE       = "cierre"


@dataclass
class ConversationState:
    stage: Stage = Stage.INTRO

    # Datos del lead que Malena va recopilando
    lead_name:    str | None = None
    negocio:      str | None = None
    usa_sistema:  str | None = None   # "no", "sí + cuál", etc.
    contact_info: str | None = None

    # Para la demo: módulos ya mostrados (evita repetirse)
    modulos_vistos: list[str] = field(default_factory=list)

    def advance(self):
        """Avanza al siguiente estado."""
        order = [Stage.INTRO, Stage.CALIFICACION, Stage.DEMO, Stage.CIERRE]
        idx = order.index(self.stage)
        if idx < len(order) - 1:
            self.stage = order[idx + 1]

    def ready_for_demo(self) -> bool:
        """¿Ya tenemos lo mínimo para arrancar la demo?"""
        return bool(self.lead_name and self.negocio)

    def ready_for_cierre(self) -> bool:
        """El usuario no tiene más preguntas o pidió cerrar."""
        return self.stage == Stage.DEMO

    def summary(self) -> str:
        """Resumen del lead para logs o futura DB."""
        return (
            f"Nombre: {self.lead_name or '?'} | "
            f"Negocio: {self.negocio or '?'} | "
            f"Sistema actual: {self.usa_sistema or '?'} | "
            f"Contacto: {self.contact_info or '?'}"
        )