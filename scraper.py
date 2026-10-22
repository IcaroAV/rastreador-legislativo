"""
Radar legislativo — scraper de proyectos de ley de Congreso Visible.

Busca proyectos de ley por palabras clave (transparencia, gobierno abierto,
participación...), recorre todas las páginas de resultados y guarda dos tablas:
  - data/proyectos.csv : un proyecto por fila
  - data/autores.csv   : un autor por fila (relación proyecto-congresista)

Fuente: Congreso Visible (Universidad de los Andes) — https://congresovisible.uniandes.edu.co
"""

import json
import math
import time
import unicodedata
from pathlib import Path
from urllib import robotparser

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
BASE = "https://congresovisible.uniandes.edu.co"
LISTA_URL = f"{BASE}/proyectos-de-ley/"
USER_AGENT = "rastreador-legislativo (https://github.com/IcaroAV/rastreador-legislativo)"

PALABRAS_CLAVE = [
    "transparencia", "acceso a la información", "gobierno abierto", "datos abiertos",
    "participación ciudadana", "rendición de cuentas", "anticorrupción",
    "contratación pública", "conflicto de intereses", "lobby", "cabildeo",
    "control social", "veedurías",
]

FILAS_POR_PAGINA = 50   # resultados por página (menos solicitudes al servidor)
PAUSA_SEGUNDOS = 2      # pausa entre solicitudes, por cortesía con el sitio
REINTENTOS = 3          # intentos si una solicitud falla

CARPETA_DATOS = Path("data")

sesion = requests.Session()
sesion.headers.update({"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------------
# 1. Respetar robots.txt
# ---------------------------------------------------------------------------
def permitido_por_robots(url):
    """
    Descarga robots.txt con nuestra propia sesión (mismo User-Agent) y dice si
    podemos visitar la URL. Si el sitio no publica un robots.txt real (responde
    con una página HTML o con error 404), no hay restricciones declaradas.
    """
    r = sesion.get(f"{BASE}/robots.txt", timeout=30)
    es_robots_real = r.status_code == 200 and "<html" not in r.text.lower()
    print(f"robots.txt -> código {r.status_code}; "
          f"{'archivo encontrado' if es_robots_real else 'el sitio no publica robots.txt'}")
    if not es_robots_real:
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# 2. Descargar una página con pausas y reintentos
# ---------------------------------------------------------------------------
def descargar(url, params):
    for intento in range(1, REINTENTOS + 1):
        try:
            r = sesion.get(url, params=params, timeout=30)
            r.raise_for_status()          # lanza error si el código no es 200
            time.sleep(PAUSA_SEGUNDOS)
            return r.text
        except requests.RequestException as e:
            espera = PAUSA_SEGUNDOS * intento * 2
            print(f"   ! Error ({e}). Intento {intento}/{REINTENTOS}; reintento en {espera}s")
            time.sleep(espera)
    raise RuntimeError(f"No se pudo descargar {url} {params}")


# ---------------------------------------------------------------------------
# 3. Extraer los datos incrustados en el HTML
# ---------------------------------------------------------------------------
def extraer_datos(html):
    """
    El sitio está hecho con Next.js: el servidor incrusta todos los datos de la
    página en una etiqueta <script id="__NEXT_DATA__"> en formato JSON.
    BeautifulSoup encuentra esa etiqueta y json la convierte en diccionarios.
    """
    soup = BeautifulSoup(html, "html.parser")
    etiqueta = soup.find("script", id="__NEXT_DATA__")
    if etiqueta is None:
        raise ValueError("No se encontró __NEXT_DATA__: ¿cambió la estructura del sitio?")
    datos = json.loads(etiqueta.string)
    props = datos["props"]["pageProps"]
    return props["ListProyectos"], props["TotalProyectos"]


# ---------------------------------------------------------------------------
# 4. Recorrer todas las páginas de una palabra clave
# ---------------------------------------------------------------------------
def sin_tildes(texto):
    """'participación' -> 'participacion'. El buscador del sitio no devuelve
    resultados cuando la palabra lleva tilde, pero sin tilde sí los encuentra."""
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")
def buscar(palabra):
    proyectos = []
    pagina, total_paginas = 1, 1
    while pagina <= total_paginas:
        params = {"page": pagina, "rows": FILAS_POR_PAGINA, "search": sin_tildes(palabra)}
        lista, total = extraer_datos(descargar(LISTA_URL, params))
        total_paginas = math.ceil(total / FILAS_POR_PAGINA)
        print(f"   página {pagina}/{max(total_paginas, 1)}: {len(lista)} proyectos")
        if not lista:
            break
        proyectos.extend(lista)
        pagina += 1
    return proyectos


# ---------------------------------------------------------------------------
# 5. Convertir cada proyecto en filas limpias
# ---------------------------------------------------------------------------
def nombre(d):
    """Devuelve d['nombre'] si d existe; si no, None (evita errores con datos vacíos)."""
    return d.get("nombre") if isinstance(d, dict) else None


def fila_proyecto(p):
    return {
        "id": p["id"],
        "titulo": p.get("titulo"),
        "alias": p.get("alias"),
        "fecha_radicacion": p.get("fecha_radicacion"),
        "numero_camara": p.get("numero_camara"),
        "numero_senado": p.get("numero_senado"),
        "cuatrienio": nombre(p.get("cuatrienio")),
        "legislatura": nombre(p.get("legislatura")),
        "tipo": nombre(p.get("tipo_proyecto_ley")),
        "iniciativa": nombre(p.get("iniciativa")),
        "sinopsis": p.get("sinopsis"),
        "n_autores": len(p.get("proyecto_ley_autor_legislativos") or []),
    }


def filas_autores(p):
    filas = []
    for a in p.get("proyecto_ley_autor_legislativos") or []:
        c = a.get("congresista") or {}
        persona = c.get("persona") or {}
        # El partido y la corporación están en 'detalle', uno por cuatrienio:
        # tomamos el del mismo cuatrienio del proyecto.
        detalle = next(
            (d for d in c.get("detalle") or [] if d.get("cuatrienio_id") == p.get("cuatrienio_id")),
            {},
        )
        filas.append({
            "proyecto_id": p["id"],
            "congresista_id": c.get("id"),
            "nombre": f"{persona.get('nombres', '')} {persona.get('apellidos', '')}".strip(),
            "partido": nombre(detalle.get("partido")),
            "corporacion": nombre(detalle.get("corporacion")),
            "departamento": nombre(detalle.get("departamento")),
        })
    return filas


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------
def main():
    if not permitido_por_robots(LISTA_URL):
        print("robots.txt no permite visitar esta sección. Se detiene el scraper.")
        return

    encontrados = {}       # id -> proyecto (un diccionario evita duplicados)
    palabras_por_id = {}   # id -> palabras clave con las que apareció

    for palabra in PALABRAS_CLAVE:
        print(f"Buscando: '{palabra}'")
        for p in buscar(palabra):
            encontrados[p["id"]] = p
            palabras_por_id.setdefault(p["id"], []).append(palabra)

    proyectos = pd.DataFrame([fila_proyecto(p) for p in encontrados.values()])
    proyectos["palabras_clave"] = proyectos["id"].map(lambda i: "; ".join(palabras_por_id[i]))
    proyectos = proyectos.sort_values("fecha_radicacion", ascending=False)

    autores = pd.DataFrame([f for p in encontrados.values() for f in filas_autores(p)])

    CARPETA_DATOS.mkdir(exist_ok=True)
    proyectos.to_csv(CARPETA_DATOS / "proyectos.csv", index=False, encoding="utf-8")
    autores.to_csv(CARPETA_DATOS / "autores.csv", index=False, encoding="utf-8")

    print(f"\nListo: {len(proyectos)} proyectos únicos y {len(autores)} registros de autoría.")
    print(f"Guardados en {CARPETA_DATOS}/proyectos.csv y {CARPETA_DATOS}/autores.csv")


if __name__ == "__main__":
    main()