# Laboratorio de red de un banco, probado como código

La red de un banco con sede central, centro de datos y dos sucursales, levantada en
contenedores con [Containerlab](https://containerlab.dev) y routers
[FRRouting](https://frrouting.org). En cada cambio, GitHub Actions levanta la red
completa, comprueba que se comporta como dice el diseño y la apaga. Si algo no cuadra,
el cambio queda en rojo antes de llegar a la red.

Resultados en vivo: **[qa.davidameth.dev](https://qa.davidameth.dev)** · Autor:
[David Ameth Martínez Sánchez](https://davidameth.dev)

## La red

| Router | Rol | Loopback |
|---|---|---|
| `sede` | Sede central: usuarios y red de gestión | 10.255.0.1 |
| `cpd` | Centro de datos: servidores | 10.255.0.2 |
| `suc1` | Sucursal 1 | 10.255.0.11 |
| `suc2` | Sucursal 2 | 10.255.0.12 |

- **OSPF** en un área (0) entre los cuatro routers.
- **Núcleo:** `sede` ↔ `cpd`, costo 10.
- **Cada sucursal tiene dos enlaces:** el principal a la sede (costo 10) y el de respaldo
  al centro de datos (costo 100). Mientras el principal esté arriba, el respaldo no
  lleva tráfico.

### Enlaces punto a punto (/31)

| Enlace | Extremo A | Extremo B | Costo |
|---|---|---|---|
| Núcleo | sede eth1 10.254.0.0 | cpd eth1 10.254.0.1 | 10 |
| Principal suc1 | sede eth2 10.254.0.2 | suc1 eth1 10.254.0.3 | 10 |
| Respaldo suc1 | cpd eth2 10.254.0.4 | suc1 eth2 10.254.0.5 | 100 |
| Principal suc2 | sede eth3 10.254.0.6 | suc2 eth1 10.254.0.7 | 10 |
| Respaldo suc2 | cpd eth3 10.254.0.8 | suc2 eth2 10.254.0.9 | 100 |

### Redes locales

| Red | Router (puerta de enlace) | Equipo de prueba |
|---|---|---|
| Usuarios de la sede 10.0.1.0/24 | sede eth4 .1 | `pc-sede` .10 |
| Gestión 10.0.99.0/24 | sede eth5 .1 | `admin` .10 |
| Servidores 10.10.10.0/24 | cpd eth4 .1 | `srv` .10 |
| Sucursal 1 10.1.1.0/24 | suc1 eth3 .1 | `pc-suc1` .10 |
| Sucursal 2 10.1.2.0/24 | suc2 eth3 .1 | `pc-suc2` .10 |

## Las pruebas

[`pruebas/probar.py`](pruebas/probar.py), solo con la biblioteca estándar de Python. Lo
esperado está escrito a mano a partir del diseño y **no se lee de las configuraciones**:
si una configuración se equivoca, la prueba lo nota en vez de copiar el error.

- **ospf:** la red converge, y cada router ve a sus vecinos en estado Full por el enlace
  correcto (y a ninguno que no esté en el diseño).
- **rutas:** cada router aprende por OSPF todas las redes que no son suyas.
- **camino:** el tráfico usa el enlace principal; traceroute confirma los saltos.
- **alcance:** los equipos de cada red local se alcanzan entre sí.

## Qué sigue

- **Fase 2:** salida a internet por dos proveedores con BGP, una prueba de caída (se
  corta un enlace y se mide cuánto tarda en pasar al respaldo), reglas de acceso (las
  sucursales no llegan a la red de gestión) y Batfish, que revisa las configuraciones
  antes de levantar nada.
- **Fase 3:** la página de arquitectura en [davidameth.dev](https://davidameth.dev).

## Correrlo

Hace falta Linux con Docker (o WSL2):

```
sudo containerlab deploy -t topologia.clab.yml
python3 pruebas/probar.py > resumen.json
sudo containerlab destroy -t topologia.clab.yml --cleanup
```

## Licencia

Código bajo [MIT](LICENSE).
