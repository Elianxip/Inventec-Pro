INVENTEC PRO — GUÍA DE INSTALACIÓN
===================================

IMPORTANTE
- Esta versión incluye base de datos SQLite, roles, asignaciones, historial, reportes y seguridad básica.
- Si ya tenías un archivo inventec.db, el programa intenta conservar los equipos y adaptar las columnas.
- Aun así, haz una copia de seguridad de inventec.db antes de reemplazar archivos.
- Es una aplicación local de desarrollo. Antes de publicarla en internet se requiere configuración adicional de producción.

1) EXTRAER
Extrae el ZIP en una carpeta nueva, por ejemplo: Escritorio\Inventec_Pro

2) ABRIR
Abre esa carpeta en Visual Studio Code.

3) INSTALAR DEPENDENCIAS
En VS Code, abre Terminal > Nueva terminal y escribe:
    py -m pip install -r requirements.txt

4) INICIAR
En la terminal escribe:
    py app.py

5) ABRIR EN EL NAVEGADOR
Visita:
    http://127.0.0.1:5000

6) CREAR EL ADMINISTRADOR
La primera vez, el sistema te llevará a /setup para crear el primer usuario administrador.
Usa una contraseña única de al menos 10 caracteres y guárdala en un lugar seguro.
No hay contraseña de demostración.

ROLES
- admin: administra usuarios, configuración, equipos y categorías.
- tecnico: registra y edita equipos, asigna/devoluciones y ve reportes.
- consulta: solo consulta inventario y reportes.

COPIA DE SEGURIDAD
Cierra la aplicación con Ctrl+C en la terminal. Copia el archivo inventec.db a otra carpeta o unidad.
No compartas el archivo de base de datos si contiene información privada.

SOLUCIÓN DE PROBLEMAS
- Si el puerto está ocupado, cierra la terminal donde esté corriendo otra versión de Inventec.
- Si una página muestra error de seguridad/CSRF, recarga la página e intenta de nuevo.
- Si usas un entorno virtual, actívalo antes de instalar los requisitos y ejecutar la app.
