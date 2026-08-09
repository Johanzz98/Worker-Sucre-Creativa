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

✔️ ¿Qué sigue?

Si quieres te dejo también los archivos vacíos generados:

main.py con la estructura

render_manager.py básico

blender_service.py para lanzar Blender

pillow_postprocess.py listo para PNG