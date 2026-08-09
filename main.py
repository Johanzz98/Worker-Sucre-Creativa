import argparse
import sys
from pathlib import Path

# FIX para Windows: Forzar UTF-8 en stdout/stderr
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from app.application import generate_mockup

def main():
    parser = argparse.ArgumentParser(
        description="Generador de mockups que preserva el look de Blender"
    )
    parser.add_argument(
        "--product", type=str, required=True,
        help="ID del producto (ej: mug, hoodie, tshirt)"
    )
    parser.add_argument(
        "--design", type=str, required=True,
        help="Ruta al diseño del usuario (PNG, con alpha opcional)"
    )
    parser.add_argument(
        "--render_passes", type=str, required=True,
        help="Carpeta que contiene los render passes de Blender (Beauty.png, ID.png)"
    )
    parser.add_argument(
        "--uvmap", type=str, default=None,
        help="Ruta al archivo UVMap.exr (opcional, para productos con UVMap compartido)"
    )
    parser.add_argument(
        "--output", type=str, required=True,
        help="Carpeta donde se guardará el mockup final"
    )
    parser.add_argument(
        "--output-name", type=str, default=None,
        help="Nombre específico para el archivo de salida (opcional)"
    )

    args = parser.parse_args()

    # Validar rutas
    design_path = Path(args.design)
    render_passes_dir = Path(args.render_passes)
    output_dir = Path(args.output)

    if not design_path.exists():
        print(f"ERROR: Diseño no encontrado: {design_path}")
        sys.exit(1)
        
    if not render_passes_dir.exists():
        print(f"ERROR: Render passes no encontrados: {render_passes_dir}")
        sys.exit(1)
    
    # Validar UVMap si se proporciona
    uvmap_path = None
    if args.uvmap:
        uvmap_path = Path(args.uvmap)
        if not uvmap_path.exists():
            print(f"ERROR: UVMap no encontrado: {uvmap_path}")
            sys.exit(1)
        
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    # Ejecutar generación
    try:
        result = generate_mockup.generate_mockup(
            product_id=args.product,
            design_path=str(design_path),
            render_passes_dir=str(render_passes_dir),
            uvmap_path=str(uvmap_path) if uvmap_path else None,  # ← NUEVO PARÁMETRO
            output_dir=str(output_dir),
            output_name=args.output_name
        )

        # Mostrar resultado
        if result.get("status") == "success":
            print(f"\nSUCCESS: Mockup generado correctamente: {result['output_file']}")
            print(f"Resolution: {result['resolution']}")
            sys.exit(0)
        else:
            print(f"\nERROR: {result.get('error')}")
            sys.exit(1)
            
    except Exception as e:
        print(f"\nERROR inesperado: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()