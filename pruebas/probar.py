"""Pruebas del laboratorio: la red levantada con Containerlab se comporta
como dice el diseño.

Lo que se espera está escrito aquí, a mano, a partir del diseño (README.md),
y no se lee de las configuraciones: si una configuración se equivoca, la
prueba lo nota en vez de copiar el error.

Grupos (fase 1):
- ospf: cada router tiene sus vecinos en estado Full, por la interfaz correcta.
- rutas: cada router aprende por OSPF todas las redes que no son suyas.
- camino: el tráfico usa el enlace principal; el de respaldo queda quieto.
- alcance: los equipos de cada red local se alcanzan entre sí (ping).

Imprime el resumen en el formato de qa.davidameth.dev (stdout, JSON) y deja
el detalle legible en stderr. Solo biblioteca estándar.

Uso: python3 pruebas/probar.py > resumen.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

LAB = "banco"

# ── El diseño ──

LOOPBACK = {"sede": "10.255.0.1", "cpd": "10.255.0.2", "suc1": "10.255.0.11", "suc2": "10.255.0.12"}

# Las redes de cada router: su loopback y sus redes locales.
REDES = {
    "sede": ["10.255.0.1/32", "10.0.1.0/24", "10.0.99.0/24"],
    "cpd": ["10.255.0.2/32", "10.10.10.0/24"],
    "suc1": ["10.255.0.11/32", "10.1.1.0/24"],
    "suc2": ["10.255.0.12/32", "10.1.2.0/24"],
}

# router -> {vecino: interfaz por la que debe verlo}
VECINOS = {
    "sede": {"cpd": "eth1", "suc1": "eth2", "suc2": "eth3"},
    "cpd": {"sede": "eth1", "suc1": "eth2", "suc2": "eth3"},
    "suc1": {"sede": "eth1", "cpd": "eth2"},
    "suc2": {"sede": "eth1", "cpd": "eth2"},
}

# (router, red, interfaz de salida esperada, por qué)
CAMINOS = [
    ("suc1", "10.10.10.0/24", "eth1", "la sucursal 1 llega a los servidores por la sede, no por el respaldo"),
    ("suc2", "10.10.10.0/24", "eth1", "la sucursal 2 llega a los servidores por la sede, no por el respaldo"),
    ("cpd", "10.1.1.0/24", "eth1", "el centro de datos responde a la sucursal 1 por la sede"),
    ("cpd", "10.1.2.0/24", "eth1", "el centro de datos responde a la sucursal 2 por la sede"),
    ("suc1", "10.1.2.0/24", "eth1", "entre sucursales se pasa por la sede"),
]

# (desde, hasta, ip)
EQUIPOS = {
    "pc-sede": "10.0.1.10",
    "admin": "10.0.99.10",
    "srv": "10.10.10.10",
    "pc-suc1": "10.1.1.10",
    "pc-suc2": "10.1.2.10",
}
PINGS = [
    ("pc-suc1", "srv"),
    ("pc-suc2", "srv"),
    ("pc-sede", "srv"),
    ("pc-suc1", "pc-suc2"),
    ("pc-suc2", "pc-sede"),
    ("srv", "pc-suc1"),
    ("admin", "srv"),
]

# Traceroute: los saltos que debe dar el paquete (la IP por la que entra a
# cada router), según el diseño.
TRAZAS = [
    ("pc-suc1", "srv", ["10.1.1.1", "10.254.0.2", "10.254.0.1", "10.10.10.10"],
     "de la sucursal 1 a los servidores: suc1 → sede → cpd"),
    ("pc-suc2", "srv", ["10.1.2.1", "10.254.0.6", "10.254.0.1", "10.10.10.10"],
     "de la sucursal 2 a los servidores: suc2 → sede → cpd"),
]

CONVERGENCIA_MAX_S = 120
ESTABLE_S = 6  # la tabla de rutas tiene que quedarse quieta este tiempo

# ── Herramientas ──


def ejecutar(nodo: str, *orden: str, tiempo: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", f"clab-{LAB}-{nodo}", *orden],
        capture_output=True, text=True, timeout=tiempo,
    )


def vtysh(nodo: str, orden: str) -> dict:
    r = ejecutar(nodo, "vtysh", "-c", orden)
    if r.returncode != 0:
        raise RuntimeError(f"vtysh en {nodo} falló: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout or "{}")


def vecinos_ospf(router: str) -> dict[str, tuple[str, str]]:
    """router-id del vecino -> (estado, interfaz). Admite los dos formatos de
    FRR: `nbrState`/`ifaceName` (10.x) y `state`/`interfaceName` (anteriores)."""
    datos = vtysh(router, "show ip ospf neighbor json")
    salida = {}
    for rid, lista in datos.get("neighbors", {}).items():
        n = lista[0] if isinstance(lista, list) else lista
        estado = n.get("nbrState") or n.get("state") or ""
        interfaz = (n.get("ifaceName") or n.get("interfaceName") or "").split(":")[0]
        salida[rid] = (estado.split("/")[0], interfaz)
    return salida


def todos_full() -> bool:
    try:
        for router, esperados in VECINOS.items():
            vistos = vecinos_ospf(router)
            for vecino in esperados:
                if vistos.get(LOOPBACK[vecino], ("", ""))[0] != "Full":
                    return False
        return True
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return False


def foto_de_rutas() -> dict | None:
    """Las rutas OSPF elegidas de cada router y por dónde salen, o None si a
    alguno le falta una red del diseño."""
    foto = {}
    try:
        for router in REDES:
            tabla = vtysh(router, "show ip route ospf json")
            elegidas = {
                red: sorted(h.get("interfaceName", "") for e in entradas if e.get("selected")
                            for h in e.get("nexthops", []) if h.get("active"))
                for red, entradas in tabla.items()
            }
            ajenas = [red for otro, redes in REDES.items() if otro != router for red in redes]
            if any(not elegidas.get(red) for red in ajenas):
                return None
            foto[router] = elegidas
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    return foto


def esperar_convergencia() -> tuple[bool, float]:
    """Convergida = vecinos en Full, todas las rutas del diseño y la tabla de
    rutas quieta durante ESTABLE_S. Solo con Full no basta: el primer día las
    pruebas corrieron con los vecinos en Full y el centro de datos todavía
    mandaba el tráfico por el respaldo; las rutas buenas entraron segundos
    después. Devuelve (convergió, segundos hasta el último cambio)."""
    t0 = time.monotonic()
    anterior, ultimo_cambio = None, t0
    while time.monotonic() - t0 < CONVERGENCIA_MAX_S:
        foto = foto_de_rutas() if todos_full() else None
        ahora = time.monotonic()
        if foto != anterior:
            anterior, ultimo_cambio = foto, ahora
        elif foto is not None and ahora - ultimo_cambio >= ESTABLE_S:
            return True, ultimo_cambio - t0
        time.sleep(1)
    return False, time.monotonic() - t0


# ── Registro de resultados ──

pruebas: list[dict] = []


def anotar(grupo: str, titulo: str, funcion) -> None:
    inicio = time.monotonic()
    try:
        error = funcion()
    except Exception as e:  # una prueba rota es una prueba que falla
        error = f"{type(e).__name__}: {e}"
    ms = round((time.monotonic() - inicio) * 1000)
    estado = "falla" if error else "pasa"
    pruebas.append({"archivo": grupo, "titulo": titulo, "proyecto": None, "estado": estado, "ms": ms, "error": error or None})
    marca = "✗" if error else "✓"
    print(f"  {marca} [{grupo}] {titulo}" + (f"\n      {error}" if error else ""), file=sys.stderr)


# ── Pruebas ──


def prueba_vecinos(router: str):
    def f():
        vistos = vecinos_ospf(router)
        problemas = []
        for vecino, interfaz in VECINOS[router].items():
            estado, por = vistos.get(LOOPBACK[vecino], ("ausente", "-"))
            if estado != "Full":
                problemas.append(f"{vecino}: {estado}")
            elif por != interfaz:
                problemas.append(f"{vecino} por {por}, se esperaba {interfaz}")
        sobran = set(vistos) - {LOOPBACK[v] for v in VECINOS[router]}
        if sobran:
            problemas.append(f"vecinos que no están en el diseño: {', '.join(sorted(sobran))}")
        return "; ".join(problemas)
    return f


def prueba_rutas(router: str):
    def f():
        tabla = vtysh(router, "show ip route ospf json")
        faltan = []
        for otro, redes in REDES.items():
            if otro == router:
                continue
            for red in redes:
                entradas = tabla.get(red, [])
                if not any(e.get("selected") and e.get("protocol") == "ospf" for e in entradas):
                    faltan.append(red)
        return f"faltan rutas OSPF a {', '.join(faltan)}" if faltan else ""
    return f


def prueba_camino(router: str, red: str, interfaz: str):
    def f():
        tabla = vtysh(router, f"show ip route {red} json")
        elegidas = [e for e in tabla.get(red, []) if e.get("selected")]
        if not elegidas:
            return f"{router} no tiene ruta a {red}"
        salidas = sorted({h.get("interfaceName") for e in elegidas for h in e.get("nexthops", []) if h.get("active")})
        if salidas != [interfaz]:
            return f"sale por {', '.join(map(str, salidas)) or 'ninguna'}, se esperaba solo {interfaz}"
        return ""
    return f


def prueba_ping(desde: str, hasta: str):
    def f():
        r = ejecutar(desde, "ping", "-c", "3", "-W", "1", "-q", EQUIPOS[hasta])
        if r.returncode != 0:
            ultima = (r.stdout.strip().splitlines() or ["sin respuesta"])[-1]
            return f"sin respuesta de {EQUIPOS[hasta]} ({ultima})"
        return ""
    return f


def prueba_traza(desde: str, hasta: str, saltos: list[str]):
    def f():
        r = ejecutar(desde, "traceroute", "-n", "-w", "1", "-q", "1", "-m", "8", EQUIPOS[hasta])
        vistos = []
        for linea in r.stdout.splitlines()[1:]:
            partes = linea.split()
            if len(partes) >= 2:
                vistos.append(partes[1])
        if vistos != saltos:
            return f"saltos {' → '.join(vistos) or '(ninguno)'}; se esperaba {' → '.join(saltos)}"
        return ""
    return f


def main() -> int:
    comienzo = time.monotonic()

    print("Esperando a que OSPF converja…", file=sys.stderr)
    convergio, segundos = esperar_convergencia()
    convergencia = round(segundos)
    print(f"  {'convergió' if convergio else 'NO convergió'} en {convergencia} s", file=sys.stderr)
    anotar("ospf", f"OSPF converge en menos de {CONVERGENCIA_MAX_S} s (tardó {convergencia} s)",
           lambda: "" if convergio else
           f"tras {convergencia} s faltan vecinos en Full o rutas del diseño, o la tabla no se queda quieta")

    for router in VECINOS:
        n = len(VECINOS[router])
        anotar("ospf", f"{router}: sus {n} vecinos en Full, cada uno por su enlace", prueba_vecinos(router))
    for router in REDES:
        anotar("rutas", f"{router} aprende por OSPF las redes de los demás", prueba_rutas(router))
    for router, red, interfaz, porque in CAMINOS:
        anotar("camino", porque, prueba_camino(router, red, interfaz))
    for desde, hasta, saltos, porque in TRAZAS:
        anotar("camino", f"traceroute {porque}", prueba_traza(desde, hasta, saltos))
    for desde, hasta in PINGS:
        anotar("alcance", f"{desde} llega a {hasta} ({EQUIPOS[hasta]})", prueba_ping(desde, hasta))

    fallidas = [p for p in pruebas if p["estado"] == "falla"]
    servidor = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    corrida = os.environ.get("GITHUB_RUN_ID")
    resumen = {
        "suite": "lab-red",
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resultado": "falla" if fallidas else "pasa",
        "pasadas": len(pruebas) - len(fallidas),
        "fallidas": len(fallidas),
        "inestables": 0,
        "omitidas": 0,
        "duracion_s": round(time.monotonic() - comienzo),
        "cobertura": None,
        "commit": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "corrida": f"{servidor}/{repo}/actions/runs/{corrida}" if repo and corrida else None,
        "fallos": [f"[{p['archivo']}] {p['titulo']}: {p['error']}" for p in fallidas],
        "detalle": True,
        "pruebas": pruebas,
    }
    print(json.dumps(resumen, ensure_ascii=False, indent=1))
    print(f"\n{resumen['pasadas']} pasadas, {resumen['fallidas']} fallidas", file=sys.stderr)
    return 1 if fallidas else 0


if __name__ == "__main__":
    sys.exit(main())
