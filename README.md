# Inventec Pro

Sistema web de gestión de inventario con Flask. Puede ejecutarse localmente con SQLite o desplegarse en Render con PostgreSQL (Supabase).

## Ejecutar localmente en Windows

1. Instala Python 3.11 o 3.12.
2. Abre una terminal dentro de esta carpeta.
3. Ejecuta:

   ```powershell
   py -m venv .venv
   .venv\\Scripts\\Activate.ps1
   pip install -r requirements.txt
   py app.py
   ```
4. Abre `http://127.0.0.1:5000/setup` y crea el primer administrador.

Sin `DATABASE_URL`, la aplicación usa `inventec.db` local.

## Despliegue en Render + Supabase

1. Crea una base PostgreSQL en Supabase.
2. Copia la cadena de conexión PostgreSQL (no la clave publishable de Supabase).
3. En Render crea un Web Service desde el repositorio GitHub.
4. Usa `pip install -r requirements.txt` como Build Command y el comando del Procfile como Start Command.
5. Configura la base de datos con `DB_HOST`, `DB_USER`, `DB_PASSWORD` (y `DB_PORT`, `DB_NAME`) **o** con `DATABASE_URL`, y `INVENTEC_SECRET_KEY` con una clave aleatoria larga. No subas esos valores a GitHub.
6. Configura `INVENTEC_HTTPS=1`.
7. Cuando el servicio esté listo, abre `/setup` y crea el administrador.

La app crea sus tablas al iniciar. La clave `sb_publishable_...` no sustituye la cadena PostgreSQL del servidor.

## Seguridad y datos

- Las contraseñas se almacenan con hash de Werkzeug.
- Se usa protección CSRF en formularios POST.
- Los permisos se validan en el servidor según el rol.
- En producción usa solo HTTPS y mantén las credenciales en las variables privadas de Render.
- Haz copias de seguridad periódicas de PostgreSQL desde Supabase.

## Nota de alojamiento gratuito

Los servicios gratuitos pueden suspenderse por inactividad y tardar en despertar. Revisa las condiciones actuales del proveedor antes de usarlo como sistema crítico de negocio.

## Diagnóstico de la conexión

- `/healthz` indica que el servidor web responde (lo usa Render).
- `/healthz/db` indica `ok` o `unavailable` según haya conexión con la base de datos.
- Los motivos concretos (usuario inválido, contraseña rechazada, red) aparecen en **Render > Logs**, con la línea `DIAGNÓSTICO:`. Las contraseñas nunca se registran.
- Con Session Pooler el usuario es `postgres.<referencia>`, no `postgres`.
