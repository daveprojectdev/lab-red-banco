"""Pruebas del laboratorio: la red levantada con Containerlab se comporta
como dice el diseño.

Lo que se espera está escrito aquí, a mano, a partir del diseño (README.md y
docs/guia.md), y no se lee de las configuraciones: si una configuración se
equivoca, la prueba lo nota en vez de copiar el error.

Grupos, en el orden en que corren:
- configuracion: la revisión previa de pruebas/revisar.py (sin levantar nada).
- ospf: la red converge y cada router ve a sus vecinos por el enlace correcto.
- bgp: las sesiones con los proveedores y entre la sede y el centro de datos.
- rutas: cada router aprende las redes de los demás y la salida a internet.
- camino: el tráfico usa el enlace y el proveedor principales (traceroute).
- alcance: los equipos se alcanzan entre sí y llegan a internet.
- acceso: las sucursales e internet NO llegan a la red de gestión.
- caidas: se corta un enlace, se mide cuánto tráfico se pierde hasta que la
  red usa el respaldo, y se comprueba que vuelve sola al principal.

Imprime el resumen en el formato de qa.davidameth.dev (stdout, JSON), deja
el detalle legible en stderr y escribe escena.json con los caminos medidos.
Solo biblioteca estándar.

Uso:
  python3 pruebas/probar.py > resumen.json
  python3 pruebas/probar.py --sin-red > resumen.json   (solo la revisión previa)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from revisar import revisar  # noqa: E402

LAB = "banco"

# ── El diseño ──

BANCO = ["sede", "cpd", "suc1", "suc2"]
INTERNET = ["isp1", "isp2", "nube"]

LOOPBACK = {"sede": "10.255.0.1", "cpd": "10.255.0.2", "suc1": "10.255.0.11", "suc2": "10.255.0.12"}

# Las redes de cada router del banco: su loopback y sus redes locales.
REDES = {
    "sede": ["10.255.0.1/32", "10.0.1.0/24", "10.0.99.0/24"],
    "cpd": ["10.255.0.2/32", "10.10.10.0/24"],
    "suc1": ["10.255.0.11/32", "10.1.1.0/24"],
    "suc2": ["10.255.0.12/32", "10.1.2.0/24"],
}

# router -> {vecino OSPF: interfaz por la que debe verlo}
VECINOS = {
    "sede": {"cpd": "eth1", "suc1": "eth2", "suc2": "eth3"},
    "cpd": {"sede": "eth1", "suc1": "eth2", "suc2": "eth3"},
    "suc1": {"sede": "eth1", "cpd": "eth2"},
    "suc2": {"sede": "eth1", "cpd": "eth2"},
}

# router -> {ip del vecino BGP: quién es}
SESIONES_BGP = {
    "sede": {"100.64.1.0": "isp1", "10.255.0.2": "cpd (iBGP)"},
    "cpd": {"100.64.2.0": "isp2", "10.255.0.1": "sede (iBGP)"},
    "isp1": {"100.64.1.1": "el banco", "100.64.11.1": "la nube"},
    "isp2": {"100.64.2.1": "el banco", "100.64.12.1": "la nube"},
    "nube": {"100.64.11.0": "isp1", "100.64.12.0": "isp2"},
}

# (router, red, interfaz de salida esperada, por qué)
CAMINOS = [
    ("suc1", "10.10.10.0/24", "eth1", "la sucursal 1 llega a los servidores por la sede, no por el respaldo"),
    ("suc2", "10.10.10.0/24", "eth1", "la sucursal 2 llega a los servidores por la sede, no por el respaldo"),
    ("cpd", "10.1.1.0/24", "eth1", "el centro de datos responde a la sucursal 1 por la sede"),
    ("cpd", "10.1.2.0/24", "eth1", "el centro de datos responde a la sucursal 2 por la sede"),
    ("suc1", "10.1.2.0/24", "eth1", "entre sucursales se pasa por la sede"),
    ("suc1", "0.0.0.0/0", "eth1", "la sucursal 1 sale a internet por la sede"),
    ("suc2", "0.0.0.0/0", "eth1", "la sucursal 2 sale a internet por la sede"),
    ("cpd", "203.0.113.0/24", "eth1", "el centro de datos también sale a internet por la sede mientras isp1 esté arriba"),
    ("nube", "10.0.0.0/8", "eth1", "internet vuelve al banco por isp1"),
    ("isp2", "10.0.0.0/8", "eth2", "isp2 llega al banco por la nube e isp1: el camino directo está alargado a propósito"),
]

EQUIPOS = {
    "pc-sede": "10.0.1.10",
    "admin": "10.0.99.10",
    "srv": "10.10.10.10",
    "pc-suc1": "10.1.1.10",
    "pc-suc2": "10.1.2.10",
    "web": "203.0.113.10",
}
PINGS = [
    ("pc-suc1", "srv"),
    ("pc-suc2", "srv"),
    ("pc-sede", "srv"),
    ("pc-suc1", "pc-suc2"),
    ("pc-suc2", "pc-sede"),
    ("srv", "pc-suc1"),
    ("admin", "srv"),
    ("pc-suc1", "web"),
    ("pc-suc2", "web"),
    ("srv", "web"),
]

# Regla de acceso de la sede (configs/sede/acceso.sh)
BLOQUEOS = [("pc-suc1", "admin"), ("pc-suc2", "admin"), ("web", "admin")]
PERMITIDOS = [("pc-sede", "admin"), ("srv", "admin")]

# Traceroute: los saltos que debe dar el paquete según el diseño.
TRAZAS = [
    ("pc-suc1", "srv", ["10.1.1.1", "10.254.0.2", "10.254.0.1", "10.10.10.10"],
     "de la sucursal 1 a los servidores: suc1 → sede → cpd"),
    ("pc-suc2", "srv", ["10.1.2.1", "10.254.0.6", "10.254.0.1", "10.10.10.10"],
     "de la sucursal 2 a los servidores: suc2 → sede → cpd"),
    ("pc-suc1", "web", ["10.1.1.1", "10.254.0.2", "100.64.1.0", "100.64.11.1", "203.0.113.10"],
     "de la sucursal 1 a internet: suc1 → sede → isp1 → nube"),
    ("srv", "web", ["10.10.10.1", "10.254.0.0", "100.64.1.0", "100.64.11.1", "203.0.113.10"],
     "de los servidores a internet: cpd → sede → isp1 → nube"),
]

# Los cortes: (nombre, router, interfaz, desde, hasta, saltos con el respaldo,
#              pérdida máxima aceptable en segundos, ventana de medición)
CORTES = [
    ("principal de la sucursal 1 (sede–suc1)", "sede", "eth2", "pc-suc1", "srv",
     ["10.1.1.1", "10.254.0.4", "10.10.10.10"], 3, 15),
    ("salida a internet por isp1 (sede–isp1)", "sede", "eth6", "pc-suc1", "web",
     ["10.1.1.1", "10.254.0.2", "10.254.0.1", "100.64.2.0", "100.64.12.1", "203.0.113.10"], 15, 40),
]

# Para escena.json: qué nodo responde en cada dirección.
NODO_DE_IP = {
    "10.1.1.1": "suc1", "10.1.2.1": "suc2", "10.0.1.1": "sede", "10.0.99.1": "sede", "10.10.10.1": "cpd",
    "10.254.0.0": "sede", "10.254.0.1": "cpd", "10.254.0.2": "sede", "10.254.0.3": "suc1",
    "10.254.0.4": "cpd", "10.254.0.5": "suc1", "10.254.0.6": "sede", "10.254.0.7": "suc2",
    "10.254.0.8": "cpd", "10.254.0.9": "suc2",
    "100.64.1.0": "isp1", "100.64.1.1": "sede", "100.64.2.0": "isp2", "100.64.2.1": "cpd",
    "100.64.11.0": "isp1", "100.64.11.1": "nube", "100.64.12.0": "isp2", "100.64.12.1": "nube",
    **{ip: nodo for nodo, ip in EQUIPOS.items()},
}

CONVERGENCIA_MAX_S = 120
ESTABLE_S = 6  # la tabla de rutas tiene que quedarse quieta este tiempo
INTERVALO_PING = 0.1  # en los cortes, un ping cada 100 ms

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


def sesiones_bgp(router: str) -> dict[str, str]:
    """ip del vecino -> estado de la sesión."""
    datos = vtysh(router, "show bgp summary json")
    pares = datos.get("ipv4Unicast", {}).get("peers", {})
    return {ip: p.get("state", "") for ip, p in pares.items()}


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


def bgp_arriba() -> bool:
    try:
        return all(
            sesiones_bgp(r).get(ip) == "Established"
            for r, pares in SESIONES_BGP.items() for ip in pares
        )
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return False


def salidas(tabla: dict, red: str) -> list[str]:
    """Por qué interfaces sale la ruta elegida a `red` (sin los saltos
    recursivos de BGP, que no tienen interfaz)."""
    return sorted({
        h["interfaceName"]
        for e in tabla.get(red, []) if e.get("selected")
        for h in e.get("nexthops", []) if h.get("active") and h.get("interfaceName")
    })


def foto_de_rutas() -> dict | None:
    """Por dónde sale cada ruta elegida de cada router, o None si a algún
    router del banco le falta una red del diseño o la salida a internet."""
    foto = {}
    try:
        for router in BANCO + INTERNET:
            tabla = vtysh(router, "show ip route json")
            foto[router] = {red: salidas(tabla, red) for red in tabla}
            if router in BANCO:
                ajenas = [red for otro, redes in REDES.items() if otro != router for red in redes]
                if any(not foto[router].get(red) for red in ajenas + ["0.0.0.0/0"]):
                    return None
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    return foto


def esperar_convergencia() -> tuple[bool, float]:
    """Convergida = vecinos OSPF en Full, sesiones BGP establecidas, todas las
    rutas del diseño y la tabla de rutas quieta durante ESTABLE_S. Solo con
    Full no basta: el primer día las pruebas corrieron con los vecinos en Full
    y el centro de datos todavía mandaba el tráfico por el respaldo; las rutas
    buenas entraron segundos después. Devuelve (convergió, segundos hasta el
    último cambio)."""
    t0 = time.monotonic()
    anterior, ultimo_cambio = None, t0
    while time.monotonic() - t0 < CONVERGENCIA_MAX_S:
        foto = foto_de_rutas() if todos_full() and bgp_arriba() else None
        ahora = time.monotonic()
        if foto != anterior:
            anterior, ultimo_cambio = foto, ahora
        elif foto is not None and ahora - ultimo_cambio >= ESTABLE_S:
            return True, ultimo_cambio - t0
        time.sleep(1)
    return False, time.monotonic() - t0


def traza(desde: str, hasta: str) -> list[str]:
    r = ejecutar(desde, "traceroute", "-n", "-w", "1", "-q", "1", "-m", "10", EQUIPOS[hasta])
    saltos = []
    for linea in r.stdout.splitlines()[1:]:
        partes = linea.split()
        if len(partes) >= 2:
            saltos.append(partes[1])
    return saltos


def ping_ok(desde: str, hasta: str) -> tuple[bool, str]:
    r = ejecutar(desde, "ping", "-c", "3", "-W", "1", EQUIPOS[hasta])
    lineas = [x for x in r.stdout.strip().splitlines() if x.strip()]
    return r.returncode == 0, (lineas[-1] if lineas else r.stderr.strip() or "sin salida")


# ── Registro de resultados ──

pruebas: list[dict] = []
escena: dict = {"flujos": [], "cortes": [], "bloqueos": []}


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


def nodos(saltos: list[str], desde: str) -> list[str]:
    return [desde] + [NODO_DE_IP.get(ip, ip) for ip in saltos]


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


def prueba_sesiones(router: str):
    def f():
        vistas = sesiones_bgp(router)
        problemas = [f"{quien} ({ip}): {vistas.get(ip) or 'no configurada'}"
                     for ip, quien in SESIONES_BGP[router].items() if vistas.get(ip) != "Established"]
        sobran = set(vistas) - set(SESIONES_BGP[router])
        if sobran:
            problemas.append(f"sesiones que no están en el diseño: {', '.join(sorted(sobran))}")
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
                if not any(e.get("selected") and e.get("protocol") == "ospf" for e in tabla.get(red, [])):
                    faltan.append(red)
        return f"faltan rutas OSPF a {', '.join(faltan)}" if faltan else ""
    return f


def prueba_sin_transito():
    """Un proveedor nunca debe aprender las rutas del otro a través del banco."""
    problemas = []
    for isp in ("isp1", "isp2"):
        datos = vtysh(isp, "show bgp ipv4 unicast 203.0.113.0/24 json")
        for camino in datos.get("paths", []):
            texto = (camino.get("aspath") or {}).get("string", "")
            if "65100" in texto.split():
                problemas.append(f"{isp} aprende 203.0.113.0/24 por el banco (AS {texto})")
    return "; ".join(problemas)


def prueba_camino(router: str, red: str, interfaz: str):
    def f():
        tabla = vtysh(router, f"show ip route {red} json")
        por = salidas(tabla, red)
        if not por:
            return f"{router} no tiene ruta a {red}"
        if por != [interfaz]:
            return f"sale por {', '.join(por)}, se esperaba solo {interfaz}"
        return ""
    return f


def prueba_traza(desde: str, hasta: str, esperados: list[str]):
    def f():
        vistos = traza(desde, hasta)
        escena["flujos"].append({"de": desde, "a": hasta, "saltos": nodos(vistos, desde), "ips": vistos})
        if vistos != esperados:
            return f"saltos {' → '.join(vistos) or '(ninguno)'}; se esperaba {' → '.join(esperados)}"
        return ""
    return f


def prueba_ping(desde: str, hasta: str):
    def f():
        ok, ultima = ping_ok(desde, hasta)
        return "" if ok else f"sin respuesta de {EQUIPOS[hasta]} ({ultima})"
    return f


def prueba_bloqueo(desde: str, hasta: str, debe_pasar: bool):
    def f():
        ok, ultima = ping_ok(desde, hasta)
        escena["bloqueos"].append({"de": desde, "a": hasta, "paso": ok, "esperado": debe_pasar, "respuesta": ultima})
        if ok and not debe_pasar:
            return f"{desde} llegó a {hasta}: la regla de acceso no lo frenó"
        if not ok and debe_pasar:
            return f"{desde} no llega a {hasta} ({ultima})"
        return ""
    return f


def medir_corte(nombre, router, interfaz, desde, hasta, saltos_respaldo, max_s, ventana_s):
    """Pings cada 100 ms mientras se corta el enlace; los que se pierden dan
    cuánto tardó la red en pasarse al respaldo."""
    registro = {"enlace": nombre, "router": router, "interfaz": interfaz, "de": desde, "a": hasta}
    escena["cortes"].append(registro)
    registro["antes"] = nodos(traza(desde, hasta), desde)
    cantidad = int(ventana_s / INTERVALO_PING)
    ping = subprocess.Popen(
        ["docker", "exec", f"clab-{LAB}-{desde}", "ping", "-i", str(INTERVALO_PING), "-c", str(cantidad),
         "-W", "1", EQUIPOS[hasta]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(2)
    ejecutar(router, "ip", "link", "set", interfaz, "down")
    salida, _ = ping.communicate(timeout=ventana_s + 30)
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", salida)
    enviados, recibidos = (int(m[1]), int(m[2])) if m else (cantidad, 0)
    perdida_s = round((enviados - recibidos) * INTERVALO_PING, 1)
    registro["perdida_s"] = perdida_s
    registro["perdidos"] = enviados - recibidos

    # Ya sin el enlace: el tráfico tiene que ir por el respaldo.
    time.sleep(3)
    con_respaldo = traza(desde, hasta)
    registro["despues"] = nodos(con_respaldo, desde)

    def corte():
        if not m:
            return f"no se pudo leer el resultado del ping: {salida.strip().splitlines()[-1:]}"
        if recibidos == 0 or perdida_s > max_s:
            return f"se perdieron {perdida_s} s de tráfico; lo aceptable es {max_s} s"
        return ""
    anotar("caidas", f"si se corta el {nombre}, {desde} sigue llegando a {hasta} "
                     f"(se perdieron {perdida_s} s; lo aceptable es {max_s} s)", corte)
    anotar("caidas", f"sin el {nombre}, el tráfico va por el respaldo: "
                     f"{' → '.join(nodos(saltos_respaldo, desde))}",
           lambda: "" if con_respaldo == saltos_respaldo else
           f"saltos {' → '.join(con_respaldo) or '(ninguno)'}; se esperaba {' → '.join(saltos_respaldo)}")

    # Se repone el enlace: la red tiene que volver sola al camino principal.
    ejecutar(router, "ip", "link", "set", interfaz, "up")
    t0 = time.monotonic()
    convergio, _ = esperar_convergencia()
    registro["vuelta_s"] = round(time.monotonic() - t0)
    de_vuelta = traza(desde, hasta)
    registro["repuesto"] = nodos(de_vuelta, desde)
    principal = registro["antes"]
    anotar("caidas", f"al reponer el {nombre}, la red vuelve sola al camino principal",
           lambda: "" if convergio and nodos(de_vuelta, desde) == principal else
           f"{'no convergió; ' if not convergio else ''}saltos {' → '.join(registro['repuesto'])}; "
           f"se esperaba {' → '.join(principal)}")


def resumen(comienzo: float) -> dict:
    fallidas = [p for p in pruebas if p["estado"] == "falla"]
    servidor = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    corrida = os.environ.get("GITHUB_RUN_ID")
    return {
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


def main() -> int:
    comienzo = time.monotonic()

    for p in revisar():
        pruebas.append(p)
        print(f"  {'✗' if p['error'] else '✓'} [configuracion] {p['titulo']}"
              + (f"\n      {p['error']}" if p["error"] else ""), file=sys.stderr)

    if "--sin-red" not in sys.argv:
        print("Esperando a que la red converja…", file=sys.stderr)
        convergio, segundos = esperar_convergencia()
        convergencia = round(segundos)
        escena["convergencia_s"] = convergencia
        print(f"  {'convergió' if convergio else 'NO convergió'} en {convergencia} s", file=sys.stderr)
        anotar("ospf", f"la red converge en menos de {CONVERGENCIA_MAX_S} s (tardó {convergencia} s)",
               lambda: "" if convergio else
               f"tras {convergencia} s faltan vecinos, sesiones BGP o rutas, o la tabla no se queda quieta")

        for router in VECINOS:
            anotar("ospf", f"{router}: sus {len(VECINOS[router])} vecinos en Full, cada uno por su enlace",
                   prueba_vecinos(router))
        for router in SESIONES_BGP:
            anotar("bgp", f"{router}: sesiones BGP con {' y '.join(SESIONES_BGP[router].values())}",
                   prueba_sesiones(router))
        anotar("bgp", "el banco no hace de tránsito: ningún proveedor aprende las rutas del otro por el banco",
               prueba_sin_transito)
        for router in REDES:
            anotar("rutas", f"{router} aprende por OSPF las redes de los demás", prueba_rutas(router))
        for router, red, interfaz, porque in CAMINOS:
            anotar("camino", porque, prueba_camino(router, red, interfaz))
        for desde, hasta, saltos, porque in TRAZAS:
            anotar("camino", f"traceroute {porque}", prueba_traza(desde, hasta, saltos))
        for desde, hasta in PINGS:
            anotar("alcance", f"{desde} llega a {hasta} ({EQUIPOS[hasta]})", prueba_ping(desde, hasta))
        for desde, hasta in BLOQUEOS:
            anotar("acceso", f"{desde} NO llega a la red de gestión ({EQUIPOS[hasta]})",
                   prueba_bloqueo(desde, hasta, False))
        for desde, hasta in PERMITIDOS:
            anotar("acceso", f"{desde} sí llega a la red de gestión ({EQUIPOS[hasta]})",
                   prueba_bloqueo(desde, hasta, True))
        for corte in CORTES:
            medir_corte(*corte)

    datos = resumen(comienzo)
    escena["fecha"] = datos["fecha"]
    escena["commit"] = datos["commit"]
    escena["resultado"] = datos["resultado"]
    Path("escena.json").write_text(json.dumps(escena, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(datos, ensure_ascii=False, indent=1))
    print(f"\n{datos['pasadas']} pasadas, {datos['fallidas']} fallidas", file=sys.stderr)
    return 1 if datos["fallidas"] else 0


if __name__ == "__main__":
    sys.exit(main())
