# CJAR: sistema biométrico de reconocimiento facial

Sistema de reconocimiento facial en Python (InsightFace `buffalo_s`: detector + ArcFace), con los 9 métodos del enunciado, una interfaz web y 50 personas ya inscritas.

- **Instalación:** [sección 1](#1-instalación-con-docker) (con Docker) o [sección 5](#5-sin-docker) (sin Docker)
- **Vulnerabilidades y fortalezas que conocemos:** [`INFORME.md`](INFORME.md)
- **Cómo funciona por dentro:** [sección 6](#6-cómo-funciona) y [`docs/workflow.html`](docs/workflow.html) (se abre en el navegador)

## 1. Instalación con Docker

### Qué hace falta

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows o Mac) o Docker Engine con Compose (Linux), **abierto y en marcha**.
- Git.
- Internet la primera vez, unos 3 GB de disco y 4 GB de memoria para Docker.

No hace falta instalar Python ni descargar modelos: todo va dentro de la imagen.

### Pasos

```bash
git clone https://github.com/marpiin/CJAR-entrega.git
cd CJAR-entrega
docker compose up -d --build
```

La primera vez tarda entre 1 y 10 minutos, según la conexión. Después abre **http://localhost:8765** (puede tardar unos segundos más en responder).

### Comprobar que funciona

```bash
docker compose run --rm cjar python admin_bd.py verificar
```

Tiene que terminar con `Íntegra: 50 personas`.

### Parar, volver a arrancar y desinstalar

```bash
docker compose stop                  # parar
docker compose start                 # volver a arrancar
docker compose down --rmi local      # quitar el contenedor y la imagen (la base de datos se queda en database/)
```

### La clave de administración

Verificar, identificar y comparar no piden nada. Para **inscribir, borrar y ver la lista de personas** en la web hace falta una clave. Se genera sola al arrancar y sale en el log; búscala en la línea «Clave de administración de CJAR»:

```bash
docker compose logs cjar
```

Cópiala en el campo «Clave de administración» de la web. **Cambia cada vez que arranca el contenedor.** Si prefieres una fija, crea un fichero `.env` junto al `docker-compose.yml` con esta línea (16 caracteres ASCII o más, al menos 8 distintos) y repite `docker compose up -d`:

```
CJAR_ADMIN_TOKEN=pon-aqui-una-clave-larga-y-aleatoria
```

Desde Python no hace falta ninguna clave.

### Si algo falla

| Qué pasa | Solución |
|---|---|
| `Cannot connect to the Docker daemon` o `error during connect ... pipe` | Docker Desktop no está abierto: ábrelo y espera a que termine de arrancar |
| `port is already allocated` (8765 ocupado) | En `docker-compose.yml` cambia el primer número de `127.0.0.1:8765:8000`, por ejemplo `127.0.0.1:9000:8000`, y abre http://localhost:9000 |
| `container name "/cjar-biometria" is already in use` | `docker rm -f cjar-biometria` |
| La página no carga justo después de arrancar | Espera unos segundos; si sigue igual, mira `docker compose logs cjar` |
| La página no carga desde otro ordenador o con otro nombre | Es a propósito: solo responde en `localhost` del equipo donde corre |
| `503 No se puede leer la base de datos` o todo da `False`/`None` | El CSV se ha modificado a mano (también al guardarlo con Excel) o falta: ver [Restaurar la base de datos](#restaurar-la-base-de-datos) |
| Inscribir da `False` y el log dice que `database` no es escribible (Linux) | El contenedor usa el usuario 1000. Arranca con el tuyo: `CJAR_UID=$(id -u) CJAR_GID=$(id -g) docker compose up -d` (no uses 0, sería root), o `sudo chown -R 1000:1000 database fotos` |
| `SHA-256 ... no coincide` al construir | La descarga de los modelos ha llegado mal: repite `docker compose build` |
| Al usar Python salen líneas `Applied providers`, `find model` o `set det-size` | Son mensajes normales de InsightFace |

## 2. Uso desde la web

| Pestaña | Qué hace | ¿Clave? |
|---|---|---|
| **Inscribir** | Registra a una persona con un id y de 1 a 10 fotos (subidas, arrastradas o de la cámara) | Sí |
| **Verificar 1:1** | ¿Es esta foto de la persona indicada? Solo sí o no | No |
| **Identificar 1:N** | ¿De quién es esta foto? El id, o nadie | No |
| **Comparar** | Similitud entre dos fotos, sin usar la BD | No |
| **Base de datos** | Lista a las personas y permite borrarlas | Sí |

Arriba se elige el flujo **Completo** o **Sin detector (`_sd`)**; verificar e identificar usan siempre el completo. Con la clave puesta se muestra además **en qué zonas de la cara se apoya el parecido** (análisis por oclusión, añade unos segundos). Verificar e identificar no dan la similitud ni un ranking: serviría para ir retocando una imagen hasta parecerse a alguien.

Al inscribir se pide confirmación si el id ya existe o si la persona se parece a otra ya inscrita.

## 3. Uso desde Python (los 9 métodos)

1. Copia tus fotos a la carpeta `fotos/` (dentro del contenedor se ve como `fotos/`).
2. Abre una consola de Python dentro del contenedor:

```bash
docker compose run --rm cjar python
```

```python
from src.biometric_system import BiometricSystem
s = BiometricSystem()                                   # usa database/database.csv

# Flujo completo
s.identificar("fotos/una_foto.jpg")                     # 'keanu_reeves' o None
s.verificar("keanu_reeves", "fotos/una_foto.jpg")       # True / False
s.inscribir("ana", "fotos/ana1.jpg")                    # o una lista: ["fotos/ana1.jpg", "fotos/ana2.jpg"]

# Sin detector (_sd): se pasa la cara ya recortada y alineada de 112x112
cara = s.detector.detect_and_align("fotos/una_foto.jpg")
s.verificar_sd("keanu_reeves", cara)
s.identificar_sd(cara)

# Sin detector ni extractor (_t): se pasa la plantilla de 512 valores
t = s.extractor.extract(cara)
s.verificar_t("keanu_reeves", t)
s.identificar_t(t)

s.db.get_all().keys()                                   # personas inscritas
s.db.delete_template("ana")                             # borrar a alguien
```

Para lanzar un script propio, déjalo en `fotos/` y ejecuta `docker compose run --rm -e PYTHONPATH=/app cjar python fotos/mi_script.py`.

| Flujo | Inscribir | Verificar (1:1) | Identificar (1:N) |
|---|---|---|---|
| Completo: foto → detector → extractor → comparador | `inscribir(id, foto)` | `verificar(id, foto)` | `identificar(foto)` |
| Sin detector | `inscribir_sd(id, foto)` | `verificar_sd(id, foto)` | `identificar_sd(foto)` |
| Sin detector ni extractor | `inscribir_t(id, template)` | `verificar_t(id, template)` | `identificar_t(template)` |

- `inscribir*` y `verificar*` devuelven `True`/`False`; `identificar*`, el id o `None`. **Ninguno lanza excepciones**: ante un dato corrupto o un ataque devuelven `False` o `None`, y el motivo queda en el log (`cjar`).
- **foto:** ruta, bytes o array de NumPy en BGR o BGRA (uint8, uint16, entero 0-255, o float 0-1 o 0-255). Máximo 30 MB y 40 megapíxeles; sin animaciones ni transparencia.
- **template:** 512 números (array, lista o texto separado por `;`) **de este sistema**, es decir, de `s.extractor.extract(cara)` o `s.db.get_template(id)`. Uno calculado con otro ArcFace no sirve, porque el extractor aplica una rotación secreta.
- **id:** hasta 128 caracteres, sin caracteres de control ni barras y sin empezar por `=`, `+`, `-`, `@` ni `~`.

### Cuándo `inscribir*` devuelve `False`

Es a propósito en estos casos:

| Caso | Cómo hacerlo igualmente |
|---|---|
| El id ya existe (si no, cualquiera podría quedarse con la identidad de otro) | `reemplazar=True` |
| La cara se parece a la de otra persona inscrita (≥ 0,40) o el id se confunde con otro (`Adam_Pearson`, letras cirílicas) | `permitir_parecido=True`, por ejemplo para un gemelo |
| Varias fotos que no son de la misma persona, más de 20 fotos, foto de grupo, cara de menos de 64 px o de mala calidad | Usar otras fotos |

**Gemelos.** `s.registrar_contraste("ana", "fotos/su_gemela.jpg")` no inscribe a nadie: a partir de ahí, una foto que se parezca más (o casi igual) a la gemela no se acepta como `ana`. La BD trae dos contrastes, los gemelos de `james_phelps_(gemelo_weasley)` y de `mary-kate_olsen`; se quitan con `docker compose run --rm cjar python admin_bd.py quitar-contrastes ID`.

## 4. Base de datos

`database/database.csv` tiene **50 personas**: las **49** de `Dataset.tar.gz` (trae 49 fotos, no las 50 del enunciado) y la elegida por el equipo, **`salman_rushdie`** (media de 3 fotos). El id es el nombre del fichero sin el año: `christian_bale_2012.jpg` → `christian_bale`. Las fotos no están en el repositorio.

- **Formato:** `id,template`, con cada plantilla cifrada (AES-256-GCM ligado al id), y una última fila `__sello__` que firma el fichero entero (HMAC-SHA256). Las filas `~id~n` son contrastes.
- **Clave:** `clave/bd.key`. Va en el repositorio **solo para que funcione nada más clonarlo**; en un despliegue real se guardaría aparte. También vale la variable `CJAR_CLAVE_BD` o `CJAR_CLAVE_BD_FICHERO`.
- **Si alguien edita el CSV**, el sello no coincide: el sistema lo rechaza entero, lo dice en el log y no reconoce ni inscribe a nadie hasta restaurarlo.

### Restaurar la base de datos

Vuelve a la base de datos original de 50 personas (se pierde lo que hayas inscrito):

```bash
git checkout database/database.csv
docker compose restart
```

El `restart` es necesario: con el sistema en marcha, una copia anterior se toma por un ataque y se rechaza.

### Mantenimiento

```bash
docker compose run --rm cjar python admin_bd.py verificar        # ¿está íntegra?
docker compose run --rm cjar python admin_bd.py --help           # importar, exportar, cambiar la clave...
```

Para regenerarla desde las fotos (en `fotos/Dataset/` y `fotos/salman_rushdie/`):

```bash
docker compose run --rm cjar python cargar_dataset.py --extra salman_rushdie fotos/salman_rushdie/salman_rushdie_1.jpg fotos/salman_rushdie/salman_rushdie_2.jpg fotos/salman_rushdie/salman_rushdie_3.jpg
```

## 5. Sin Docker

Con **Python 3.11**. Se ha probado en Windows 11; recomendamos Docker, porque es donde están las protecciones del contenedor.

Windows (PowerShell):

```powershell
git clone https://github.com/marpiin/CJAR-entrega.git
cd CJAR-entrega
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -c requirements.lock
.venv\Scripts\python admin_bd.py verificar
.venv\Scripts\python -m uvicorn web.app:app --port 8765 --no-server-header
```

Linux o Mac:

```bash
git clone https://github.com/marpiin/CJAR-entrega.git
cd CJAR-entrega
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c requirements.lock
.venv/bin/python admin_bd.py verificar
.venv/bin/python -m uvicorn web.app:app --port 8765 --no-server-header
```

La primera ejecución descarga los modelos en `~/.insightface`. La clave de administración sale en esa misma ventana. Si Windows responde «Una directiva de Control de aplicaciones bloqueó este archivo», es el Control inteligente de aplicaciones: repite el comando o usa Docker.

## 6. Cómo funciona

![Esquema del sistema](docs/esquema.png)

| Módulo | Archivo | Qué hace |
|---|---|---|
| Clase principal | `src/biometric_system.py` | Los 9 métodos y las reglas de inscripción |
| Detector | `src/detector.py` | Localiza la cara principal y la alinea a 112x112 con sus 5 puntos faciales |
| Extractor | `src/extractor.py` | Convierte la cara en una plantilla de 512 números, comprueba la calidad y detecta ruido adversario |
| Comparador | `src/matcher.py` | Similitud coseno: umbral `0.48` y margen `0.05` frente a las demás personas |
| BD | `src/database.py` | Lee y escribe el CSV; falla cerrado si no es auténtico |
| Cifrado | `src/cripto.py` | Rotación secreta de las plantillas, cifrado por fila y sello |
| Utilidades | `src/utils.py` | Valida fotos, plantillas e ids |
| Explicador | `src/explicador.py` | En qué zonas de la cara se apoya el parecido |
| Web | `web/` | FastAPI y página HTML |

El contenedor corre sin root, con la raíz de solo lectura, sin capacidades y con límites de CPU, memoria y procesos. La imagen base, las dependencias y los modelos están fijados por SHA-256. `database/`, `fotos/` y el código son carpetas de tu ordenador montadas en el contenedor: si cambias algo en `src/` o `web/`, basta con `docker compose restart`; si cambias las dependencias, hay que regenerar `requirements-hash.txt` (`uv pip compile --generate-hashes`).

Cada fallo deja un aviso en `docker compose logs cjar`, sin las fotos ni las plantillas, pero puede incluir ids y rutas de ficheros: revísalo antes de compartirlo.

## 7. Tests

```bash
docker build --build-arg INSTALL_DEV=1 -t cjar-biometria:test .
docker run --rm --network none cjar-biometria:test python -m unittest tests.test_system tests.test_bd tests.test_imagen tests.test_web tests.test_avisos tests.test_despliegue
```

Son 114 y tienen que acabar en `OK` (los avisos que salen por pantalla son los ataques que se están probando). No tocan tus datos. `docker compose run --rm cjar python main.py` lanza una demo corta.
