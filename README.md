📄 README.md (final)
# 🏺 Python Render Service — Mug Mockup Engine  
Microservicio Python para generar mockups realistas de tazas (mugs) usando Blender para el render y Pillow para la optimización final del PNG.

---

## 🚀 Overview
Este servicio es parte del pipeline de mockups tipo Printful:

1. **Node.js** recibe el diseño del usuario.
2. Node llama a este microservicio Python con:
   - ID del producto (por ahora: `mug_white_11oz`)
   - Ruta del diseño
   - Ruta de salida

3. **Python ejecuta Blender** en modo background usando `render_composite.py`, el cual:
   - Combina AOVs (Diffuse, Glossy, AO y más)
   - Aplica UV Warp para colocar el diseño curva­do sobre la taza
   - Genera un PNG final totalmente realista

4. **Pillow** realiza postprocesos ligeros:
   - Compresión PNG
   - Recorte transparente
   - Limpieza final

5. El PNG se devuelve a Node.js y se entrega al frontend.

---

## 🧱 Arquitectura



python-render-service/
│
├── main.py
│
├── blender/
│ ├── renders/
│ │ └── mug_white_11oz/
│ │ ├── front.exr
│ │ ├── front_mask.png
│ │ ├── front_UV.png
│ │ ├── angle.exr
│ │ └── ...
│ └── scripts/
│ └── render_composite.py
│
├── app/
│ ├── services/
│ │ ├── blender_service.py
│ │ ├── pillow_postprocess.py
│ │ └── render_manager.py
│ │
│ ├── adapters/
│ │ └── node_adapter.py
│ │
│ ├── controllers/
│ │ └── render_controller.py
│ │
│ ├── dtos/
│ │ └── request_dto.py
│ │
│ └── exceptions/
│ └── errors.py
│
├── config/
│ └── products/
│ └── mug_white_11oz.json
│
└── output/
└── results/


---

## 🔧 Requisitos

### Python


Pillow==10.3.0
numpy==1.26.0


### Blender  
Versión recomendada: **3.6 LTS o superior**  
Debe estar disponible como comando:



blender --background


---

## ▶️ Running a Render (manual)



python main.py
--product mug_white_11oz
--design /input/user_design.png
--output /output/result/


---

## 📦 Configuración de productos (JSON)
Cada producto se define como un JSON en `/config/products/`.

Ejemplo `mug_white_11oz.json`:



{
"name": "Mug Blanco 11oz",
"variations": ["front", "angle_45"],
"resolution": 2500,
"blender_scene": "blender/renders/mug_white_11oz/"
}


---

## 🌟 Estado actual
✔ Mug realista funcionando  
✔ EXR + UV + AOV listos  
✔ Postproceso Pillow mínimo  
✔ Arquitectura enterprise lista para expandir

---

## 🔮 Futuro
- Agregar camisas y hoodies
- Optimizar colas en Node.js + Redis
- Cache CDN (Cloudflare / BunnyCDN)
- Ofrecer API pública

---

# License
MIT

---

## 🎬 Modo "render por partes" FRENTE/BACK (Blender)

Para que los mockups de polera se vean **idénticos al preview GLB**: el worker
hornea los diseños como textura (atlas plan A) y re-renderiza los 4 ángulos
con Blender headless. Sin esto, el compositor repinta el diseño plano sobre el
beauty y se nota sin sombreado ni el mapeo exacto del editor.

### Variables de entorno

| Variable | Valor | Efecto |
|---|---|---|
| `MOCKUP_RENDER_MODE` | `blender` | Activa el re-render con textura (default `compositor`) |
| `BLENDER_BIN` | ruta al ejecutable | Deja el `blender` del PATH si se omite |
| `POLERA_FRONTBACK_BLEND` | ruta al `.blend` de producción | Debe ser el blend re-UV (poleraNormaliMultiple). Si falta busca `data/blends/poleraNormaliMultiple.blend` |
| `POLERA_RENDER_SIZE` | `2048` | Resolución cuadrada de los renders |
| `POLERA_RENDER_TIMEOUT` | `600` | Segundos máx del render por pedido |

Si falta Blender o el `.blend`, el worker **cae automáticamente al compositor**
sin romper el job.

### Archivos

- `app/application/render_frontback.py` — atlas (numpy) + wrapper subprocess.
- `blender/scripts/render_frontback_designs.py` — script bpy: textura en
  `Polera_Frente`/`Polera_Atras`, `Polera_Restante` blanco, orbita la cámara
  y renderiza `centro/derecha/izquierda/atras` (CYCLES).

### En Docker

La imagen base `python:3.11-slim` NO trae Blender. Para activar el modo
re-render hay que instalar Blender en la imagen (p. ej. descargar el tarball
oficial de blender.org en un stage y dejarlo en el PATH) y montar el `.blend`.

✔️ ¿Qué sigue?

Si quieres te dejo también los archivos vacíos generados:

main.py con la estructura

render_manager.py básico

blender_service.py para lanzar Blender

pillow_postprocess.py listo para PNG