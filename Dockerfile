FROM python:3.11-slim

# OpenCV en Linux requiere estas librerías del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Modo "render por partes" FRENTE/BACK (opieional): instalar Blender en la
# imagen y setear MOCKUP_RENDER_MODE=blender + BLENDER_BIN + POLERA_FRONTBACK_BLEND.
# Sin Blender el worker usa el compositor (UVMap + repintado) como fallback.
# Ejemplo (descomentar y ajustar versión):
# RUN apt-get update && apt-get install -y --no-install-recommends wget xz-utils \
#     && wget https://download.blender.org/release/Blender4.3/blender-4.3.2-linux-x64.tar.xz -O /tmp/blender.tar.xz \
#     && mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
#     && ln -s /opt/blender/blender /usr/local/bin/blender && rm /tmp/blender.tar.xz \
#     && apt-get remove -y wget && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=7860
EXPOSE 7860

CMD ["python", "app.py"]
