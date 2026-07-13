# Prompt para Claude Code — Integración "MIA Coach" con el bot Malena

> Copiá y pegá todo lo de abajo en Claude Code, dentro del proyecto del **campus**.
> Antes de arrancar, reemplazá los placeholders marcados con `<<...>>`.

---

## Contexto

En este proyecto (el **campus** de la empresa) ya existe una sección llamada **"MIA Coach"**.
Quiero modificar esa sección para que el usuario pueda **iniciar una reunión de capacitación con
un bot de IA (Malena)** eligiendo qué quiere que le expliquen:

1. **Módulo 1 — Configuración** (completo)
2. **Módulo 2 — Caja y Caja Mayor** (completo)
3. **Una sección específica** (ej. Balanza, Gastos, Clientes, Proveedores, etc.)

El bot es un servicio externo ya funcionando. Cuando se lo llama, crea a "Malena", la mete en la
reunión de Google Meet, y ella hace la capacitación en vivo enfocada en lo que el usuario eligió,
manejando voz + una demo del sistema MGW en pantalla. **Soporta varias reuniones en paralelo.**

## Objetivo concreto

En la sección **MIA Coach**:

- Mostrar al usuario las opciones: **Módulo 1**, **Módulo 2**, y **Sección específica** (con un
  selector de secciones — lista abajo).
- Al elegir una opción y confirmar:
  1. El **backend del campus** genera un link de Google Meet nuevo (como ya lo hace / o el método
     que corresponda en este proyecto).
  2. El backend hace un `POST` al bot (contrato abajo) con `meeting_url`, `module`/`field` y el
     `user_name` del usuario logueado.
  3. Según la respuesta, se lleva al usuario a la reunión (o se le muestra el link para unirse), y
     Malena se suma sola y arranca la capacitación enfocada en lo pedido.
- Manejar el caso de **cola de espera** (cuando no hay sistemas libres) y los errores.

> **Importante:** primero explorá este repo para encontrar la sección **MIA Coach** (frontend y
> backend), entender el stack, cómo se generan hoy los links de Meet, cómo se identifica al usuario
> logueado, y cómo se hacen las llamadas HTTP salientes. **Seguí los patrones que ya existen** en
> este proyecto (framework, estilos, manejo de estado, capa de servicios). No inventes una
> arquitectura nueva.

## Contrato del bot (API externa — NO se toca, se consume)

- **Base URL:** `<<BOT_API_BASE_URL>>` (producción: `https://ai-zoom-bot-production.up.railway.app`)
- **Guardar la base URL en una variable de entorno** del campus (ej. `MIA_COACH_BOT_URL`), no
  hardcodeada.

### Crear la reunión con el bot
`POST {BASE_URL}/bot/create` — `Content-Type: application/json`

Body:
```json
{
  "meeting_url": "https://meet.google.com/xxx-xxxx-xxx",
  "module": "modulo_2",
  "field": "",
  "user_name": "Juan Pérez"
}
```

Reglas del body:
- `meeting_url` (obligatorio): el link de Meet que genera el backend del campus.
- `module`: `"modulo_1"` | `"modulo_2"` | `""`.
- `field`: nombre de una sección puntual (ej. `"balanza"`) o `""`.
- **Elegí uno solo**: para un módulo completo mandá `module` con `field: ""`; para una sección
  puntual mandá `field` con `module: ""`. (Si mandás los dos, el bot prioriza `field`.)
- `user_name`: nombre del usuario logueado del campus (Malena lo saluda por su nombre).

Respuestas posibles:
- **Arrancó (hay sistema libre):**
  ```json
  { "status": "running", "sid": "ab12cd34", "bot_id": "...", "sistema": "dev2" }
  ```
- **En cola (todos los sistemas ocupados, pero la cola está habilitada):**
  ```json
  { "status": "waiting", "sid": "ab12cd34", "position": 2 }
  ```
  → Mostrar al usuario "estás en la posición N, la reunión arranca sola cuando se libera un cupo".
  El bot se suma automáticamente al Meet cuando le toca (no hay que reintentar el POST).
- **Rechazado (cola deshabilitada o llena):** HTTP `503`
  ```json
  { "status": "rejected", "error": "..." }
  ```
- **Error interno:** HTTP `500`.

### Endpoints de estado (opcionales, para UI/soporte)
- `GET {BASE_URL}/sessions/{sid}` → estado de una sesión: `{ sid, state, bot_id, sistema, focus, ... }`
  (`state` ∈ `waiting` | `running` | `closing`). Útil para mostrar "esperando…" → "en curso".
- `DELETE {BASE_URL}/sessions/{sid}` → cancelar/cerrar una reunión.
- `GET {BASE_URL}/pool/status` → cuántos sistemas libres/ocupados y tamaño de la cola.

## Valores válidos para "Sección específica" (`field`)

Mandá exactamente estos strings en `field` (el bot los reconoce por nombre). Sugerencia de
etiquetas para el selector:

| Etiqueta en la UI | `field` a enviar |
|---|---|
| Balanza | `balanza` |
| Gastos | `gastos` |
| Clientes | `clientes` |
| Proveedores | `proveedores` |
| Usuarios / permisos | `usuarios` |
| Listas de precios | `listas de precios` |
| Grupos de productos | `grupos` |
| Productos | `productos` |
| Precios | `precios` |
| Historial de precios | `historial de precios` |
| Combos | `combos` |
| Bancos / cheques | `bancos` |
| Formas de pago | `formas de pago` |
| Descuentos | `descuentos` |
| Terminales / posnet | `terminales` |
| Impuestos | `impuestos` |
| Apertura de caja | `apertura de caja` |
| Venta en caja | `caja` |
| Lista de ventas | `lista de ventas` |
| Retiros de caja | `retiros de caja` |
| Cierre de caja | `cierre de caja` |
| Caja mayor / tesorería | `caja mayor` |

(Para "Módulo 1" mandá `module:"modulo_1"`, para "Módulo 2" `module:"modulo_2"`, ambos con `field:""`.)

## Requisitos de UX

- La opción **Sección específica** habilita el selector con las secciones de arriba; Módulo 1 y
  Módulo 2 no necesitan selector.
- `user_name` sale del usuario logueado del campus (no pedírselo por formulario).
- Botón de confirmar deshabilitado mientras se crea la reunión (evitar doble POST).
- Al recibir `running`: llevar al usuario al Meet (redirect o abrir en pestaña nueva) o mostrar el
  link con un botón "Unirme a la reunión".
- Al recibir `waiting`: mostrar mensaje de cola con la `position`; opcionalmente poll a
  `GET /sessions/{sid}` hasta que `state` pase a `running` para avisar/redirigir.
- Al recibir `503`/`500`: mensaje claro ("no hay cupos disponibles, probá en unos minutos") y
  permitir reintentar.
- Guardar el `sid` devuelto (en la sesión del usuario / estado de la vista) por si se quiere
  consultar estado o cancelar.

## Qué NO hacer

- No modificar el servicio del bot (es externo).
- No hardcodear la URL del bot ni credenciales.
- No cambiar cómo el campus genera los links de Meet más de lo necesario para pasar el `meeting_url`.

## Entregable

- La sección **MIA Coach** con las 3 opciones funcionando end-to-end: elegir → crear Meet → llamar
  al bot → unir al usuario, con manejo de `running` / `waiting` / error.
- La base URL del bot como variable de entorno.
- Un resumen de qué archivos tocaste y cómo probarlo en este proyecto.

---

## Notas para vos (dev del campus), fuera del prompt

- El `sid` identifica la sesión del bot; el campus no necesita conocer los `sid` para el flujo básico
  (solo si querés mostrar estado o cancelar).
- Cada reunión concurrente usa un "sistema" MGW distinto del lado del bot (pool de credenciales).
  Si hay más pedidos que sistemas, entran en cola y arrancan solos — por eso `waiting` no es un error.
