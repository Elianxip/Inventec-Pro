# Registro de cambios

## Fase 1 — Conexión Render ↔ Supabase (2026-11-01)
Respaldo del original: `respaldo/app.py.original`.

**app.py**
- Conexión PostgreSQL por parámetros (`host`, `user`, `password`…) en vez de una URL: soporta contraseñas con `@ # / % espacios`.
- Nuevas variables opcionales `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`.
- Validación previa: usuario sin `.referencia` con el pooler, marcador `[YOUR-PASSWORD]`, corchetes, URL mal formada, host directo IPv6.
- Errores traducidos a diagnósticos accionables en los logs (sin secretos).
- `init_db()` ya no se ejecuta al importar: arranque diferido con reintento cada 5 s. Si la BD cae, la app responde 503 en vez de no arrancar.
- Rutas `/healthz` (sin BD) y `/healthz/db`; `render.yaml` usa `/healthz`.
- Manejadores de error 503/500 y registro de excepciones.
- RLS activado en todas las tablas (Supabase las expone por su API pública).
- Bloqueo con `pg_advisory_xact_lock` para arranque simultáneo de workers.
- Búsqueda con `ILIKE` en PostgreSQL (antes distinguía mayúsculas).
- Corrección de "open redirect" en `/login?next=`.
- No se puede asignar un equipo que ya está asignado.

**templates/error.html**: las páginas de error salían en blanco sin sesión iniciada.

## Fase 2 — Backend (2026-11-01)
- Usuarios: editar nombre y rol, restablecer contraseña (admin), cambiar la propia en «Mi cuenta» (`/account`); protección del último administrador y contra auto-degradación.
- Sesiones: cada petición revalida al usuario en la BD; desactivar o cambiar de rol surte efecto al instante.
- Conflictos: columna `version` en `equipment`; si dos personas editan a la vez, la segunda recibe aviso y ve los datos actuales (no se pisa nada).
- Asignaciones: operación atómica (sin dobles asignaciones); ya no se puede marcar «Asignado» desde el formulario; cambiar el estado de un equipo asignado cierra su asignación activa.
- Categorías: edición (nombre y descripción).
- `/setup`: si defines `INVENTEC_SETUP_TOKEN` en Render, exige ese código para crear el primer administrador.
- CSV: neutraliza inyección de fórmulas de Excel.
- Ajustes: caché de 30 s para reducir consultas a Supabase.
- Migración automática: las bases existentes reciben la columna `version` sin perder datos.

## Fase 3 — Diseño y experiencia (2026-11-01)
- Inventario paginado (25 por página) con filtros conservados; antes cortaba en 200 registros sin aviso.
- Panel con gráfico de estado (dona, CSS puro, sin librerías externas) y barras por categoría.
- Revisado en escritorio (1366 px) y móvil (390 px): sin desbordes horizontales.
