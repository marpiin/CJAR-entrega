# Imagen del sistema biométrico de reconocimiento facial (CJAR)
# Imagen base fijada por digest: un cambio en la etiqueta 3.11-slim no entra sin querer
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENCV_IO_MAX_IMAGE_PIXELS=40000000

# Librerías de sistema que necesitan OpenCV / onnxruntime. OJO: requirements.txt pide
# opencv-python-headless, pero insightface arrastra opencv-python (con GUI) y pip instala los dos
# en site-packages/cv2/: el cv2 que queda cargado es el de opencv-python (build con Qt/GUI, medido
# en esta imagen), NO el headless. Por eso hacen falta las librerías de X11/GL. No se cambian las
# dependencias; ver «Limitaciones conocidas» (i) en el README. Ya no hace falta compilar nada.
# apt-get upgrade trae los parches de seguridad de Debian publicados desde que se creó la imagen
# base (p. ej. zlib1g); el resto de paquetes quedan como los fija el digest.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
        libgl1 \
        libxcb1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin cjar

WORKDIR /app

# Versiones exactas (requirements.txt) y todo el árbol de dependencias fijado (requirements.lock).
# INSTALL_DEV=1 añade lo necesario para los tests web (httpx2).
COPY requirements.txt requirements.lock requirements-dev.txt ./
ARG INSTALL_DEV=0
# pip, setuptools y wheel de la imagen base se actualizan a versiones con los parches de seguridad publicados
RUN pip install --upgrade pip==26.3 setuptools==84.2.1 wheel==0.46.2 \
    && pip install -r requirements.txt -c requirements.lock \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install -r requirements-dev.txt; fi

# Los modelos se descargan en el build (el contenedor arranca sin internet) y se comprueba
# su SHA-256 antes de usarlos. Pertenecen a root: el usuario de la app no puede cambiarlos.
# /home/cjar se crea con modo 0700: sin el chmod 755 un uid distinto de 1000 (CJAR_UID) no puede
# entrar, InsightFace cree que falta el modelo e intenta descargarlo.
COPY docker/descargar_modelos.py /tmp/descargar_modelos.py
RUN HOME=/home/cjar python /tmp/descargar_modelos.py && rm /tmp/descargar_modelos.py \
    && chown -R root:root /home/cjar/.insightface && chmod -R a-w /home/cjar/.insightface \
    && chmod 755 /home/cjar

# El código es de root y de solo lectura para la app; solo database/ y fotos/ son escribibles.
# Se copia SOLO lo necesario, por nombre (no `COPY . .`): lo que se añada luego a la carpeta del
# proyecto (.env, claves...) no entra en la imagen aunque se olvide ignorarlo.
# docker/, Dockerfile, docker-compose.yml, .gitignore y .dockerignore solo los leen los tests de despliegue.
COPY src ./src
COPY web ./web
COPY tests ./tests
COPY docker ./docker
COPY main.py cargar_dataset.py Dockerfile docker-compose.yml .gitignore .dockerignore ./
RUN mkdir -p /app/database /app/fotos && chown cjar:cjar /app/database /app/fotos

# La base de datos vive en un volumen para que persista y no se meta en la imagen
VOLUME ["/app/database"]

USER cjar
ENV HOME=/home/cjar

# Por defecto arranca la interfaz web en http://localhost:8765
# Demo por terminal: docker compose run --rm cjar python main.py
# Tests:             docker compose run --rm cjar python -m unittest tests.test_system -v
# --limit-concurrency cuenta CONEXIONES abiertas (también las que no envían nada): con 32, 32 sockets sin
# clave dejaban toda la web en 503, administrador incluido. Con 1024 ese ataque es inofensivo (medido con 1000
# sockets, incluso enviando ~60 KiB de cabecera cada uno: la memoria no varía), pero NO es una solución:
# quien abra más de 1024 sockets sigue pudiendo, y un plazo de petición dentro de uvicorn no ayuda si el
# atacante reconecta. Lo único que lo resuelve es un proxy inverso con plazos de cabecera y de cuerpo
# (ver «Limitaciones conocidas» en el README). Los cuerpos en vuelo los acota PRESUPUESTO_* en web/app.py.
EXPOSE 8000
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", \
     "--limit-concurrency", "1024", "--timeout-keep-alive", "5"]
