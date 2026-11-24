"""
Configuración de pytest: agrega la raíz del proyecto al sys.path para que `import app`
funcione tanto en local, en GitHub Actions, como dentro del contenedor (/app).
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
