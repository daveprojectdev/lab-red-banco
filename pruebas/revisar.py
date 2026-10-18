"""Revisión de las configuraciones ANTES de levantar la red.

Lee configs/*/frr.conf como texto y busca los errores que en una red real
se pagan caro: direcciones repetidas, un enlace con un costo distinto en
cada punta, un vecino BGP que no existe o que espera otro número de AS.
No levanta nada: tarda milisegundos, y si algo falla el CI se detiene antes
de gastar un solo contenedor.

Uso:
  python3 pruebas/revisar.py          → informe legible; sale con 1 si algo falla
  (probar.py la importa y suma sus pruebas al resumen, grupo «configuracion»)
"""

from __future__ import annotations

import ipaddress
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

CONFIGS = Path(__file__).resolve().parent.parent / "configs"


@dataclass
class Interfaz:
    nombre: str
    red: ipaddress.IPv4Interface | None = None
    area: str | None = None
    costo: int | None = None
    tipo: str | None = None
    pasiva: bool = False


@dataclass
class Router:
    nombre: str
    interfaces: dict[str, Interfaz] = field(default_factory=dict)
    ospf_id: str | None = None
    bgp_as: int | None = None
    bgp_id: str | None = None
    vecinos: dict[str, int] = field(default_factory=dict)  # ip -> remote-as


def leer(ruta: Path) -> Router:
    r = Router(ruta.parent.name)
    actual: Interfaz | None = None
    bloque = None
    for cruda in ruta.read_text(encoding="utf-8").splitlines():
        linea = cruda.split("!")[0].rstrip()
        if not linea.strip():
            continue
        if not linea.startswith(" "):
            actual, bloque = None, linea.split()[0]
        if m := re.match(r"interface (\S+)", linea):
            actual = r.interfaces.setdefault(m[1], Interfaz(m[1]))
            continue
        if m := re.match(r"router bgp (\d+)", linea):
            r.bgp_as = int(m[1])
            continue
        texto = linea.strip()
        if actual is not None:
            if m := re.match(r"ip address (\S+)", texto):
                actual.red = ipaddress.IPv4Interface(m[1])
            elif m := re.match(r"ip ospf area (\S+)", texto):
                actual.area = m[1]
            elif m := re.match(r"ip ospf cost (\d+)", texto):
                actual.costo = int(m[1])
            elif m := re.match(r"ip ospf network (\S+)", texto):
                actual.tipo = m[1]
            elif texto == "ip ospf passive":
                actual.pasiva = True
        elif bloque == "router" and (m := re.match(r"ospf router-id (\S+)", texto)):
            r.ospf_id = m[1]
        elif bloque == "router" and (m := re.match(r"bgp router-id (\S+)", texto)):
            r.bgp_id = m[1]
        elif bloque == "router" and (m := re.match(r"neighbor (\S+) remote-as (\d+)", texto)):
            r.vecinos[m[1]] = int(m[2])
    return r


def cargar() -> dict[str, Router]:
    return {ruta.parent.name: leer(ruta) for ruta in sorted(CONFIGS.glob("*/frr.conf"))}


# ── Las revisiones: cada una devuelve una lista de problemas ──


def direcciones_unicas(routers):
    vistas: dict[str, str] = {}
    problemas = []
    for r in routers.values():
        for i in r.interfaces.values():
            if i.red is None:
                continue
            ip = str(i.red.ip)
            if ip in vistas:
                problemas.append(f"{ip} está en {vistas[ip]} y en {r.nombre} {i.nombre}")
            vistas[ip] = f"{r.nombre} {i.nombre}"
    return problemas


def identificadores_unicos(routers):
    problemas, vistos = [], {}
    for r in routers.values():
        for clase, rid in (("OSPF", r.ospf_id), ("BGP", r.bgp_id)):
            if rid is None:
                continue
            if (clase, rid) in vistos:
                problemas.append(f"router-id {clase} {rid} repetido en {vistos[clase, rid]} y {r.nombre}")
            vistos[clase, rid] = r.nombre
        if r.ospf_id and r.bgp_id and r.ospf_id != r.bgp_id:
            problemas.append(f"{r.nombre}: router-id OSPF {r.ospf_id} y BGP {r.bgp_id} no coinciden")
        lo = r.interfaces.get("lo")
        rid = r.ospf_id or r.bgp_id
        if rid and lo and lo.red and str(lo.red.ip) != rid:
            problemas.append(f"{r.nombre}: el router-id {rid} no es su loopback {lo.red.ip}")
    return problemas


def enlaces(routers):
    """Las puntas de cada /31, agrupadas por red."""
    por_red: dict[str, list[tuple[Router, Interfaz]]] = {}
    for r in routers.values():
        for i in r.interfaces.values():
            if i.red is not None and i.red.network.prefixlen == 31:
                por_red.setdefault(str(i.red.network), []).append((r, i))
    return por_red


def enlaces_con_dos_puntas(routers):
    problemas = []
    for red, puntas in enlaces(routers).items():
        nombres = [f"{r.nombre} {i.nombre}" for r, i in puntas]
        if len(puntas) != 2:
            problemas.append(f"{red} tiene {len(puntas)} punta(s): {', '.join(nombres)}")
        elif puntas[0][0].nombre == puntas[1][0].nombre:
            problemas.append(f"{red} une al router {puntas[0][0].nombre} consigo mismo")
    return problemas


def ospf_igual_en_las_dos_puntas(routers):
    problemas = []
    for red, puntas in enlaces(routers).items():
        if len(puntas) != 2:
            continue
        (ra, a), (rb, b) = puntas
        if (a.area is None) != (b.area is None):
            con, sin = (ra, rb) if a.area else (rb, ra)
            problemas.append(f"{red}: OSPF en {con.nombre} pero no en {sin.nombre}")
            continue
        if a.area is None:
            continue  # enlace sin OSPF (hacia un proveedor)
        lado = f"{ra.nombre} {a.nombre} ↔ {rb.nombre} {b.nombre}"
        if a.area != b.area:
            problemas.append(f"{lado}: área {a.area} contra {b.area}")
        if a.costo != b.costo:
            problemas.append(f"{lado}: costo {a.costo} contra {b.costo} (el tráfico iría por un lado y volvería por otro)")
        if a.tipo != b.tipo:
            problemas.append(f"{lado}: tipo de red {a.tipo} contra {b.tipo}")
    return problemas


def redes_locales_pasivas(routers):
    """Una red de usuarios no debe hablar OSPF: cualquiera conectado ahí podría
    inyectar rutas."""
    return [
        f"{r.nombre} {i.nombre} ({i.red.network}) está en OSPF sin ser pasiva"
        for r in routers.values()
        for i in r.interfaces.values()
        if i.red is not None and i.red.network.prefixlen < 31 and i.area is not None and not i.pasiva
    ]


def vecinos_bgp_reciprocos(routers):
    duenos: dict[str, Router] = {}
    for r in routers.values():
        for i in r.interfaces.values():
            if i.red is not None:
                duenos[str(i.red.ip)] = r
    problemas = []
    for r in routers.values():
        for ip, remoto in r.vecinos.items():
            otro = duenos.get(ip)
            if otro is None:
                problemas.append(f"{r.nombre}: el vecino {ip} no es de ningún router")
                continue
            if otro.bgp_as != remoto:
                problemas.append(f"{r.nombre}: espera a {ip} ({otro.nombre}) con AS {remoto}, pero {otro.nombre} es AS {otro.bgp_as}")
            mias = {str(i.red.ip) for i in r.interfaces.values() if i.red is not None}
            de_vuelta = [v for v, a in otro.vecinos.items() if v in mias and a == r.bgp_as]
            if not de_vuelta:
                problemas.append(f"{r.nombre} ↔ {otro.nombre}: {otro.nombre} no tiene a {r.nombre} como vecino con AS {r.bgp_as}")
    return problemas


REVISIONES = [
    ("Ninguna dirección IP está repetida", direcciones_unicas),
    ("Cada router tiene un router-id único, igual a su loopback", identificadores_unicos),
    ("Cada enlace punto a punto tiene exactamente dos puntas, en routers distintos", enlaces_con_dos_puntas),
    ("OSPF igual en las dos puntas de cada enlace: área, costo y tipo", ospf_igual_en_las_dos_puntas),
    ("Las redes locales están en OSPF como pasivas", redes_locales_pasivas),
    ("Cada vecino BGP existe, tiene el AS esperado y responde de vuelta", vecinos_bgp_reciprocos),
]


def revisar() -> list[dict]:
    """Las revisiones en el formato de prueba de qa.davidameth.dev."""
    routers = cargar()
    salida = []
    for titulo, funcion in REVISIONES:
        inicio = time.monotonic()
        try:
            problemas = funcion(routers)
        except Exception as e:  # una revisión rota es una revisión que falla
            problemas = [f"{type(e).__name__}: {e}"]
        salida.append({
            "archivo": "configuracion",
            "titulo": titulo,
            "proyecto": None,
            "estado": "falla" if problemas else "pasa",
            "ms": round((time.monotonic() - inicio) * 1000),
            "error": "; ".join(problemas) or None,
        })
    return salida


def main() -> int:
    resultados = revisar()
    for p in resultados:
        print(f"  {'✗' if p['error'] else '✓'} {p['titulo']}" + (f"\n      {p['error']}" if p["error"] else ""))
    fallidas = sum(p["estado"] == "falla" for p in resultados)
    print(f"\n{len(resultados) - fallidas} de {len(resultados)} revisiones bien")
    return 1 if fallidas else 0


if __name__ == "__main__":
    sys.exit(main())
