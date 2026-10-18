# Guía del laboratorio, paso a paso

Esta guía explica el laboratorio de punta a punta, sin dar nada por sabido: qué es cada
pieza, por qué está configurada así, qué prueba cada prueba y qué se encontró al
construirlo. Al final hay preguntas de entrevista con su respuesta.

Las cifras salen de las corridas reales del CI (octubre de 2026). Cada corrida nueva
las vuelve a medir en [qa.davidameth.dev](https://qa.davidameth.dev).

---

## 1. En una frase

Es la red de un banco, levantada en contenedores, a la que se le hacen pruebas
automáticas en cada cambio, igual que al código de una aplicación: si un cambio rompe
el diseño, se ve en rojo antes de aplicarlo.

A esto se le llama **red como código**: la red se describe en archivos de texto
(topología y configuraciones), los archivos viven en git y cada cambio pasa por pruebas.

## 2. La red, dibujada en palabras

```
                    internet (nube, con el servidor «web»)
                     /                         \
                  isp1                         isp2
             (principal)                    (respaldo)
                   |                             |
                 sede  ─────── núcleo ───────  cpd
        (usuarios, gestión)               (servidores)
             |       \                     /      |
     principal        principal     respaldo      respaldo
             |           \           /            |
           suc1            \       /            suc2
                       (cada sucursal tiene un enlace
                        principal a la sede y uno de
                        respaldo al centro de datos)
```

- **sede:** la oficina central. Tiene la red de los usuarios de la sede, la **red de
  gestión** (desde donde se administra la red) y la salida principal a internet.
- **cpd:** el centro de procesamiento de datos, donde están los servidores. También tiene
  una salida a internet, de respaldo.
- **suc1 y suc2:** dos sucursales. Cada una tiene **dos caminos**: el principal va a la
  sede y el de respaldo va directo al centro de datos.
- **isp1, isp2 y nube:** representan internet. isp1 e isp2 son los dos proveedores del
  banco, y la nube es «el resto de internet», con un servidor web al que hay que llegar.

Cada red local tiene un **equipo de prueba** (un contenedor Linux) que hace de computadora
de esa red: `pc-sede`, `admin`, `srv`, `pc-suc1`, `pc-suc2` y `web`.

## 3. Las piezas

| Pieza | Qué es | Para qué está aquí |
|---|---|---|
| **Containerlab** | Una herramienta que levanta redes completas con contenedores de Docker. | Crea los 13 equipos y los conecta con cables virtuales, según `topologia.clab.yml`. |
| **FRRouting (FRR)** | Un software de routing de código abierto, el mismo que usan muchos equipos reales. | Cada router es un contenedor con FRR. Su configuración está en `configs/<router>/frr.conf`. |
| **network-multitool** | Un Linux pequeño con `ping`, `traceroute` y otras herramientas. | Los equipos de prueba. |
| **Python** | Solo la biblioteca estándar, sin instalar nada. | `pruebas/revisar.py` y `pruebas/probar.py`. |
| **GitHub Actions** | El CI de GitHub. | Corre todo en cada cambio y cada lunes. El repo es público, así que no cuesta nada. |
| **qa.davidameth.dev** | La página donde se publica cada resultado. | Que se vea, en vivo, que el lab pasa. |

## 4. Los conceptos, uno por uno

### Dentro del banco: OSPF

**OSPF** es un protocolo con el que los routers de una misma organización se cuentan qué
redes tienen, y cada uno calcula solo el mejor camino a cada red. Si un enlace cae,
vuelven a calcular sin que nadie toque nada.

- **Vecino y estado Full.** Dos routers conectados se presentan y se pasan su mapa de la
  red. Cuando terminan de sincronizarlo, la relación queda en estado **Full**.
- **Costo.** Cada enlace tiene un costo y OSPF elige el camino cuya suma es menor. Aquí,
  los enlaces normales cuestan 10 y los de respaldo 100. Por eso, desde la sucursal 1, ir
  a los servidores por la sede cuesta 10 + 10 + 10 = 30 y por el respaldo 100 + 10 = 110:
  gana la sede. **El costo se configura en las dos puntas** del enlace, porque cada router
  usa el de su lado para salir.
- **Área 0.** OSPF puede dividir una red grande en áreas. Esta es chica y usa una sola.
- **Loopback.** Una dirección del propio router que no depende de ningún cable (por
  ejemplo, la de la sede es 10.255.0.1). Sirve para identificarlo (*router-id*) y para que
  otros lleguen a él por cualquier camino.
- **Red pasiva.** Las redes de usuarios están en OSPF como **pasivas**: el router las
  anuncia, pero no habla OSPF hacia ellas. Si no, cualquiera conectado en una sucursal
  podría inyectar rutas falsas.
- **Enlaces /31.** Un enlace entre dos routers solo necesita dos direcciones, y un /31 es
  justo eso. Ahorra direcciones.

### Hacia internet: BGP

**BGP** es el protocolo con el que se hablan las organizaciones distintas en internet.
Cada organización es un **sistema autónomo (AS)** con un número propio: el banco es el
65100, isp1 el 64501, isp2 el 64502 y la nube el 64500.

- **eBGP e iBGP.** eBGP es la sesión con otra organización (la sede con isp1). iBGP es
  entre routers del mismo banco: la sede y el centro de datos se cuentan por iBGP lo que
  cada uno aprende de su proveedor. La iBGP va entre los **loopbacks**, para que no dependa
  de un solo cable.
- **Ruta por defecto (0.0.0.0/0).** «Lo que no sepas dónde está, mándalo por aquí.» Los
  proveedores se la dan al banco, y la sede y el centro de datos la reparten por OSPF a las
  sucursales: la de la sede con métrica 10 y la del centro de datos con 100, así que las
  sucursales salen por la sede.
- **Preferencia local (*local preference*).** Decide **por dónde sale** el banco. Lo que
  llega de isp2 vale 50 y lo de isp1, 100. Gana el número más alto: el banco sale por isp1
  mientras exista.
- **AS prepend.** Decide **por dónde vuelve** el tráfico desde internet. Hacia isp2, el
  banco repite su número de AS tres veces más (65100 65100 65100 65100). BGP prefiere el
  camino con menos AS, así que internet vuelve por isp1. Las pruebas lo verifican: hasta
  isp2 llega al banco dando la vuelta por la nube e isp1.
- **No hacer de tránsito.** A un proveedor solo se le anuncia el bloque del banco
  (10.0.0.0/8), con un filtro (`route-map SOLO-BANCO`). Sin ese filtro, la sede le pasaría
  a isp1 lo que el centro de datos aprende de isp2, y el tráfico de internet entre los dos
  proveedores podría pasar por el banco. Es un error clásico y caro.
- **blackhole 10.0.0.0/8.** Una ruta «a ninguna parte» para todo el bloque del banco. BGP
  solo anuncia una red si existe en la tabla, y esta ruta la crea. Las redes reales tienen
  rutas más específicas, que ganan; lo que no tenga ruta se descarta en vez de dar vueltas.

### La regla de acceso

En la sede, `configs/sede/acceso.sh` usa **iptables** (el cortafuegos de Linux) para que a
la red de gestión no entren las sucursales ni internet. Si a alguien le roban una sesión
en una sucursal, desde ahí no puede llegar a los equipos que administran la red. La sede y
el centro de datos sí entran.

## 5. Cómo viaja un paquete (medido)

Estos caminos los mide `traceroute` en cada corrida:

| Desde → hasta | Camino | Por qué |
|---|---|---|
| Sucursal 1 → servidores | suc1 → sede → cpd | El principal cuesta 30; el respaldo, 110. |
| Sucursal 1 → internet | suc1 → sede → isp1 → nube | La sede reparte la ruta por defecto con métrica 10. |
| Servidores → internet | cpd → sede → isp1 → nube | El centro de datos también sale por isp1 (preferencia 100 contra 50). |

**Y cuando algo se corta** (pings cada 100 ms mientras se corta el enlace):

| Se corta | El camino pasa a ser | Tráfico perdido | Vuelve solo al principal |
|---|---|---|---|
| Principal de la sucursal 1 | suc1 → cpd | 0 paquetes | sí, en unos 19 s |
| Salida por isp1 | suc1 → sede → cpd → isp2 → nube | 0,2 s (2 paquetes) | sí, en unos 9 s |

El tiempo de vuelta incluye los 6 s que la prueba espera para confirmar que la tabla de
rutas se quedó quieta.

## 6. Qué prueba cada grupo, y por qué

Las pruebas no leen las configuraciones para saber qué esperar: **lo esperado está escrito
a mano a partir del diseño.** Si una configuración se equivoca, la prueba lo nota en vez
de copiar el error. Es el mismo principio que usarías en QA de software: el oráculo no
puede salir del código que estás probando.

| Grupo | Qué comprueba | Qué error atraparía |
|---|---|---|
| **configuracion** | Antes de levantar nada: IP y router-id únicos, enlaces con dos puntas y el mismo costo en las dos, redes locales pasivas, vecinos BGP que existen y se responden. | Un costo distinto en cada punta, una IP repetida, un número de AS mal escrito. Tarda milisegundos. |
| **ospf** | La red converge, y cada router ve a sus vecinos en Full por el enlace correcto, sin vecinos de más. | Un cable mal conectado, un área equivocada. |
| **bgp** | Todas las sesiones establecidas, y el banco no hace de tránsito. | Un filtro olvidado, una sesión caída. |
| **rutas** | Cada router aprende las redes de los demás. | Una red que nadie anuncia. |
| **camino** | El tráfico sale por el enlace y el proveedor principales, confirmado con traceroute. | Un costo mal puesto: **la red funciona, pero por el camino equivocado.** |
| **alcance** | Ping entre todas las redes y a internet. | Algo que de verdad no llega. |
| **acceso** | Las sucursales e internet no llegan a gestión; la sede y el centro de datos sí. | Una regla de cortafuegos que no se aplicó. |
| **caidas** | Se corta un enlace, se mide la pérdida, se comprueba el respaldo y la vuelta. | Un respaldo que no funciona, o que no devuelve el tráfico. |

## 7. Lo que se encontró construyéndolo

Estas son buenas historias para una entrevista, porque son reales.

1. **«Full» no significa que la red convergió.** La primera corrida dio 5 fallos: las
   pruebas empezaban en cuanto los vecinos quedaban en Full, pero el centro de datos
   todavía mandaba el tráfico por el respaldo. Las rutas buenas entraron unos segundos
   después. La corrección fue esperar a que estén todas las rutas y que la tabla se quede
   quieta 6 s. Lección: una prueba tiene que esperar el estado correcto, no un indicador
   que se le parece.
2. **Los pings pasaban con el camino mal.** Con el costo del respaldo de la sucursal 2 en
   10 (a propósito), todos los pings seguían bien, pero las pruebas de camino fallaron: el
   tráfico iba por donde no debía. Una prueba superficial lo habría dejado pasar.
3. **La sesión iBGP se quedaba en «Active».** Arrancaba antes de que OSPF diera ruta a los
   loopbacks, y FRR espera 120 s para reintentar. Mientras tanto, el centro de datos salía a
   internet por isp2. Dos pruebas de camino lo atraparon. La corrección fue reintentar cada
   5 s (`timers connect 5`).
4. **Un error frenado antes de desplegar.** El
   [pull request de demostración](https://github.com/daveprojectdev/lab-red-banco/pull/1)
   trae ese mismo costo mal puesto. La revisión previa lo detecta leyendo los archivos y el
   CI se detiene en 5 segundos, sin levantar la red.

## 8. Lo que el laboratorio simplifica (dilo tú antes de que te lo pregunten)

- **No hay NAT.** Un banco real usa direcciones privadas por dentro y las traduce a
  públicas al salir. Aquí el banco anuncia 10.0.0.0/8 directamente para que se vea BGP sin
  otra capa encima.
- **Los cortes se detectan al instante.** En contenedores, apagar una interfaz avisa al
  otro lado de inmediato, y por eso la pérdida es casi cero. En una red real, si el corte
  ocurre más allá de un switch o dentro del proveedor, el router no se entera hasta que
  vence el temporizador (40 s en OSPF por defecto). En producción eso se resuelve con
  **BFD**, un protocolo que detecta la caída en milisegundos. Es el siguiente paso natural.
- **Una sola área de OSPF, solo IPv4,** y temporizadores de BGP cortos (3 y 9 s) para que
  las pruebas no tarden.
- **Batfish no está todavía.** Batfish analiza configuraciones sin levantar la red, pero
  lee FRR solo dentro del formato de Cumulus Linux y su imagen pesa unos 2 GB. Para este
  diseño, la revisión propia (`revisar.py`) cubre los errores que importan en
  milisegundos. Sumar Batfish queda como mejora.

## 9. Preguntas de entrevista

**¿Qué es este proyecto?**
La red de un banco en contenedores, con pruebas automáticas en cada cambio. Lo mismo que
hago en QA con las API del core bancario, aplicado a una red: un diseño esperado, pruebas
que lo comparan con lo real y un CI que frena lo que no cuadra.

**¿Por qué OSPF dentro y BGP fuera?**
OSPF es para una sola organización: calcula rápido el mejor camino con los costos. BGP es
para hablar con otras organizaciones: no busca el camino más corto, sino aplicar políticas
(por dónde salgo, por dónde quiero que vuelvan, qué anuncio).

**¿Cómo decides por dónde sale el banco y por dónde vuelve el tráfico?**
La salida, con preferencia local: lo de isp1 vale 100 y lo de isp2, 50. La vuelta, con AS
prepend: hacia isp2 el camino se alarga para que internet prefiera isp1.

**¿Qué pasa si se cae isp1?**
La sesión BGP con isp1 cae, la sede deja de tener esa salida y el tráfico pasa por el
centro de datos hacia isp2. Medido: se pierden 0,2 s de tráfico. Cuando isp1 vuelve, el
tráfico regresa solo.

**¿Por qué tus pruebas no leen las configuraciones?**
Porque entonces solo comprobarían que la red hace lo que dice la configuración, aunque la
configuración esté mal. Lo esperado sale del diseño. Es el problema del oráculo en QA.

**¿Qué es «convergencia» y cómo la mides?**
El momento en que todos los routers tienen las rutas finales y dejan de cambiarlas. La mido
así: vecinos en Full, sesiones BGP arriba, todas las rutas del diseño presentes y la tabla
quieta 6 s. Aquí tarda unos 20 s desde que arranca la red.

**¿Qué es el tránsito y por qué lo evitas?**
Que el tráfico entre dos proveedores pase por tu red. Te consume ancho de banda y no te
pagan por él. Lo evito anunciando solo mi bloque, y una prueba revisa que ningún proveedor
aprenda las rutas del otro a través del banco.

**¿Qué mejorarías?**
BFD para detectar caídas reales en milisegundos, NAT en la salida, una segunda área de
OSPF para las sucursales y Batfish en la revisión previa.

## 10. Para leer los archivos, en este orden

1. `topologia.clab.yml`: qué equipos hay y cómo se conectan.
2. `configs/suc1/frr.conf`: el router más simple (solo OSPF).
3. `configs/sede/frr.conf`: OSPF, BGP, la ruta por defecto y el filtro de tránsito.
4. `configs/cpd/frr.conf`: lo mismo, más la preferencia local y el AS prepend.
5. `configs/sede/acceso.sh`: la regla de acceso.
6. `pruebas/revisar.py` y `pruebas/probar.py`: arriba de cada archivo está lo esperado
   (el diseño); abajo, cómo se comprueba.
7. `.github/workflows/lab.yml`: el orden en que el CI hace todo.
