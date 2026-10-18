# Laboratorio de red de un banco, probado como código

La red de un banco con sede central, centro de datos, dos sucursales y salida a
internet por dos proveedores, levantada en contenedores con
[Containerlab](https://containerlab.dev) y routers [FRRouting](https://frrouting.org).
En cada cambio, GitHub Actions revisa las configuraciones, levanta la red completa,
comprueba que se comporta como dice el diseño (cortando enlaces incluso) y la apaga. Si
algo no cuadra, el cambio queda en rojo antes de llegar a la red.

Resultados en vivo: **[qa.davidameth.dev](https://qa.davidameth.dev)** · Autor:
[David Ameth Martínez Sánchez](https://davidameth.dev) · La explicación paso a paso,
con las preguntas de entrevista: **[docs/guia.md](docs/guia.md)**

## La red

| Router | Rol | Loopback | AS |
|---|---|---|---|
| `sede` | Sede central: usuarios, red de gestión y salida principal a internet | 10.255.0.1 | 65100 |
| `cpd` | Centro de datos: servidores y salida de respaldo a internet | 10.255.0.2 | 65100 |
| `suc1` | Sucursal 1 | 10.255.0.11 | — |
| `suc2` | Sucursal 2 | 10.255.0.12 | — |
| `isp1` | Proveedor principal | 100.100.0.1 | 64501 |
| `isp2` | Proveedor de respaldo | 100.100.0.2 | 64502 |
| `nube` | Internet, con un servidor `web` | 100.100.0.100 | 64500 |

- **Dentro del banco, OSPF** en un área (0). Núcleo `sede` ↔ `cpd`, costo 10. Cada
  sucursal tiene un enlace principal a la sede (costo 10) y uno de respaldo al centro de
  datos (costo 100).
- **Hacia internet, BGP.** La sede habla con isp1 y el centro de datos con isp2; entre
  ellos, iBGP por los loopbacks. El banco sale por isp1 (lo que llega de isp2 tiene
  preferencia local 50) y pide que internet vuelva por isp1 (hacia isp2 el camino va
  alargado con *AS prepend*). A un proveedor solo se le anuncia 10.0.0.0/8: el banco
  nunca hace de tránsito entre los dos.
- **Regla de acceso:** a la red de gestión (10.0.99.0/24) no entran las sucursales ni
  internet ([`configs/sede/acceso.sh`](configs/sede/acceso.sh)).

### Enlaces punto a punto (/31)

| Enlace | Extremo A | Extremo B | Costo OSPF |
|---|---|---|---|
| Núcleo | sede eth1 10.254.0.0 | cpd eth1 10.254.0.1 | 10 |
| Principal suc1 | sede eth2 10.254.0.2 | suc1 eth1 10.254.0.3 | 10 |
| Respaldo suc1 | cpd eth2 10.254.0.4 | suc1 eth2 10.254.0.5 | 100 |
| Principal suc2 | sede eth3 10.254.0.6 | suc2 eth1 10.254.0.7 | 10 |
| Respaldo suc2 | cpd eth3 10.254.0.8 | suc2 eth2 10.254.0.9 | 100 |
| Sede–isp1 | isp1 eth1 100.64.1.0 | sede eth6 100.64.1.1 | — (BGP) |
| Cpd–isp2 | isp2 eth1 100.64.2.0 | cpd eth5 100.64.2.1 | — (BGP) |
| isp1–nube | isp1 eth2 100.64.11.0 | nube eth1 100.64.11.1 | — (BGP) |
| isp2–nube | isp2 eth2 100.64.12.0 | nube eth2 100.64.12.1 | — (BGP) |

### Redes locales

| Red | Router (puerta de enlace) | Equipo de prueba |
|---|---|---|
| Usuarios de la sede 10.0.1.0/24 | sede eth4 .1 | `pc-sede` .10 |
| Gestión 10.0.99.0/24 | sede eth5 .1 | `admin` .10 |
| Servidores 10.10.10.0/24 | cpd eth4 .1 | `srv` .10 |
| Sucursal 1 10.1.1.0/24 | suc1 eth3 .1 | `pc-suc1` .10 |
| Sucursal 2 10.1.2.0/24 | suc2 eth3 .1 | `pc-suc2` .10 |
| Internet 203.0.113.0/24 | nube eth3 .1 | `web` .10 |

## Las pruebas

Solo con la biblioteca estándar de Python. Lo esperado está escrito a mano a partir del
diseño y **no se lee de las configuraciones**: si una configuración se equivoca, la
prueba lo nota en vez de copiar el error.

1. **Antes de levantar nada,** [`pruebas/revisar.py`](pruebas/revisar.py) lee las
   configuraciones: direcciones e identificadores únicos, cada enlace con sus dos puntas
   y el mismo costo en las dos, redes locales pasivas, vecinos BGP que existen y se
   responden. Si algo falla, el CI se detiene en segundos sin gastar un contenedor
   ([ejemplo](https://github.com/daveprojectdev/lab-red-banco/pull/1)).
2. **Con la red levantada,** [`pruebas/probar.py`](pruebas/probar.py):
   - **ospf** y **bgp:** la red converge, cada vecino está donde debe y no sobra ninguno.
   - **rutas** y **camino:** cada router aprende lo que debe, el tráfico usa el enlace y
     el proveedor principales, y traceroute lo confirma salto por salto.
   - **alcance:** los equipos se alcanzan entre sí y llegan a internet.
   - **acceso:** las sucursales e internet no llegan a la red de gestión.
   - **caidas:** se corta un enlace con pings cada 100 ms, se mide cuánto tráfico se
     pierde hasta que la red usa el respaldo, y se comprueba que vuelve sola al
     principal al reponerlo.

Cada corrida deja también `escena.json` (en los artefactos de la corrida) con los caminos
medidos antes y después de cada corte.

## Correrlo

Hace falta Linux con Docker (o WSL2):

```
python3 pruebas/revisar.py
sudo containerlab deploy -t topologia.clab.yml
python3 pruebas/probar.py > resumen.json
sudo containerlab destroy -t topologia.clab.yml --cleanup
```

## Licencia

Código bajo [MIT](LICENSE).
