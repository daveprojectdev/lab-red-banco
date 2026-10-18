# Arquitectura Técnica — Laboratorio de red de un banco, probado como código

**Autor:** David Ameth Martínez Sánchez · QA y desarrollo · Panamá
**Naturaleza:** Proyecto propio, un solo mantenedor, código abierto (MIT)
**Versión:** 1.0 — 18 de octubre de 2026 · estado: en verde en GitHub Actions; corre en
cada push a `main` y cada lunes, y publica cada corrida en qa.davidameth.dev. No hay red
física: todo vive en contenedores que se crean y se destruyen en cada corrida.

> Este documento organiza el laboratorio por dominios de arquitectura, no por cronología de
> desarrollo. Los fallos que aparecieron al construirlo no van como lista de errores: se
> agrupan en la Sección 5 por el supuesto equivocado que los produjo. Las decisiones de
> diseño y las alternativas descartadas viven en los Registros de Decisiones
> Arquitectónicas (Apéndice C). La explicación paso a paso, con preguntas de entrevista,
> está en `docs/guia.md`; la cronología commit por commit, en el historial de
> `daveprojectdev/lab-red-banco`. Las cifras son de la corrida `acc3c32` del 18 de octubre
> de 2026.

---

## Resumen ejecutivo

El laboratorio es la red de un banco (sede, centro de datos, dos sucursales, dos
proveedores de internet, una red de cajeros automáticos como servicio externo y un hacker
en internet) levantada en 17 contenedores con Containerlab y FRRouting. En cada cambio,
GitHub Actions revisa las configuraciones, levanta la red, le pasa 75 pruebas y la apaga.

Mirado por encima parece un ejercicio de certificación: unos routers con OSPF y BGP que se
hacen ping. No lo es. Lo que el laboratorio resuelve son tres problemas que no se ven desde
fuera.

El primero es de **oráculo**. Una prueba que lee la configuración para saber qué esperar
solo comprueba que la red hace lo que dice su configuración, aunque la configuración esté
mal. Aquí lo esperado está escrito a mano a partir del diseño, en la cabecera de
`pruebas/probar.py`, y no se deriva de ningún archivo de `configs/` (ADR-003).

El segundo es de **una red que funciona por el camino equivocado**. Un costo mal puesto no
rompe ningún ping: el tráfico llega, pero por el enlace de respaldo. Por eso 14 de las 75
pruebas no miran si el tráfico llega sino por dónde va, interfaz por interfaz y con
`traceroute` salto por salto (Sección 4.2).

El tercero es de **medir la caída en vez de suponerla**. Dos pruebas cortan un enlace con
pings cada 100 ms en curso y cuentan los que se pierden hasta que la red usa el respaldo:
0 paquetes al cortar el principal de la sucursal 1 y 2 paquetes (0,2 s) al cortar la
salida por isp1. Después reponen el enlace y comprueban que el tráfico vuelve solo al
principal (Sección 5.4).

---

## Índice

1. Arquitectura Core
2. Enrutamiento
3. Seguridad
4. Estrategia de Pruebas
5. Patrones de Resiliencia
6. CI y Publicación
7. Deuda Técnica y Huecos Conocidos

**Apéndices**
A. Cronología condensada
B. Referencia rápida
C. Registros de Decisiones Arquitectónicas

---

## 1. Arquitectura Core

### 1.1 La topología

```
                 internet (nube, AS 64500: «web» y «hacker»)
                  /                                \
            isp1 (AS 64501)                   isp2 (AS 64502)
             principal                          respaldo
                  |                                  |
                sede  ───────── núcleo, 10 ─────────  cpd ──── socio (AS 65200)
        usuarios, gestión                     servidores,    red de cajeros,
                  |      \                   / autorizador   línea dedicada
         principal, 10    principal, 10   respaldo, 100
                  |           \           /          |
                suc1            \       /       respaldo, 100
                                  suc2 ──────────────┘
```

| Router | Papel | Loopback | AS |
|---|---|---|---|
| `sede` | Usuarios, red de gestión y salida principal a internet | 10.255.0.1 | 65100 |
| `cpd` | Servidores, autorizador y salida de respaldo | 10.255.0.2 | 65100 |
| `suc1`, `suc2` | Sucursales, cada una con un enlace principal y uno de respaldo | 10.255.0.11 y .12 | solo OSPF |
| `isp1`, `isp2` | Los dos proveedores | 100.100.0.1 y .2 | 64501, 64502 |
| `nube` | El resto de internet, con un servidor `web` y un `hacker` | 100.100.0.100 | 64500 |
| `socio` | La red de cajeros automáticos, por línea dedicada al centro de datos | 172.31.255.1 | 65200 |

Volumen: 8 routers, 9 equipos de prueba (uno por red local), 19 enlaces y 9 redes locales.
Los enlaces entre routers son /31: dos direcciones por enlace, ni una más.

### 1.2 Los archivos

| Archivo | Qué contiene |
|---|---|
| `topologia.clab.yml` | Los 17 nodos, sus imágenes y los 19 cables virtuales |
| `configs/<router>/frr.conf` | OSPF, BGP, rutas estáticas y filtros de cada router |
| `configs/sede/acceso.sh`, `configs/cpd/acceso.sh` | Las reglas de iptables: gestión, borde y socio |
| `pruebas/revisar.py` | La revisión estática: lee los archivos sin levantar nada |
| `pruebas/probar.py` | Las pruebas con la red levantada; arriba, el diseño esperado |
| `.github/workflows/lab.yml` | El orden en que el CI hace todo |

**Solo la biblioteca estándar de Python.** Las pruebas no instalan nada: hablan con los
routers por `docker exec ... vtysh` y leen la salida en JSON que FRR ya sabe dar. Un
`requirements.txt` vacío es una dependencia menos que se puede romper en un runner nuevo.

### 1.3 Los equipos de prueba

Cada red local tiene un contenedor Linux que hace de computadora de esa red: `pc-sede`,
`admin` (gestión), `srv` (servidores, con HTTPS público), `autorizador`, `pc-suc1`,
`pc-suc2`, `web`, `hacker` y `cajeros`. Son los que hacen ping, abren puertos y lanzan los
ataques. Sin ellos, las pruebas solo verían las tablas de los routers, que pueden estar
bien mientras un cortafuegos tira el tráfico real.

---

## 2. Enrutamiento

El banco usa dos protocolos con dos trabajos distintos. Dentro, OSPF calcula el mejor
camino con costos. Hacia fuera, BGP no busca el camino más corto: aplica políticas sobre
por dónde se sale, por dónde se vuelve y qué se anuncia.

### 2.1 Dentro: OSPF, un área, costos asimétricos a propósito

Núcleo `sede`–`cpd` con costo 10; cada sucursal, principal a la sede con costo 10 y
respaldo al centro de datos con costo 100. Desde la sucursal 1, llegar a los servidores por
la sede suma 10 + 10 + 10 = 30; por el respaldo, 100 + 10 = 110. Gana la sede, y una prueba
de camino lo comprueba en cada corrida.

**El costo se configura en las dos puntas.** Cada router usa el costo de su lado para
salir. Con uno distinto en cada punta, la ida y la vuelta toman caminos diferentes y nadie
lo nota mirando un ping. `revisar.py` compara las dos puntas de cada enlace antes de
levantar la red.

Las redes de usuarios están en OSPF como **pasivas**: el router las anuncia, pero no habla
OSPF hacia ellas. Sin eso, un equipo conectado en una sucursal podría inyectar rutas.

### 2.2 Hacia fuera: BGP con dos proveedores

| Decisión | Mecanismo | Valor | Cómo se comprueba |
|---|---|---|---|
| Por dónde sale el banco | Preferencia local | isp1 100, isp2 50 | El centro de datos también sale por la sede mientras isp1 esté arriba |
| Por dónde vuelve internet | AS prepend hacia isp2 | 65100 × 4 | isp2 llega al banco dando la vuelta por la nube e isp1 |
| Qué se anuncia | `route-map` de salida | solo 10.0.0.0/8 | Ningún proveedor aprende las rutas del otro a través del banco |
| Ruta por defecto interna | `default-information originate` | sede métrica 10, cpd 100 | Las sucursales salen por la sede |

La sesión iBGP entre la sede y el centro de datos va entre loopbacks, para que no dependa
de un solo cable. Una `ip route 10.0.0.0/8 blackhole` en los dos routers de borde hace que
BGP tenga qué anunciar y que lo que no tenga ruta más específica se descarte en vez de dar
vueltas.

**El banco no hace de tránsito.** Sin el filtro de salida, la sede le pasaría a isp1 lo
que el centro de datos aprende de isp2, y el tráfico entre los dos proveedores podría
cruzar el banco. Es un error clásico y caro, y tiene su propia prueba.

### 2.3 El socio: lo que aprende

El centro de datos le enseña a la red de cajeros, por BGP, solo la red del autorizador
(10.10.20.0/24). El socio no tiene ruta hacia las sucursales, la gestión ni internet: no
sabe que existen. Y su red (192.168.50.0/24) nunca sale hacia los proveedores, porque el
filtro de salida solo deja pasar el bloque del banco. Es la primera de dos capas; la
segunda está en la Sección 3.3.

---

## 3. Seguridad

Las reglas viven en dos archivos de iptables, uno por router de borde, y cada una tiene al
menos una prueba que intenta romperla desde el lado del atacante.

### 3.1 La red de gestión

A 10.0.99.0/24 no entran las sucursales ni internet; la sede y el centro de datos sí. Si a
alguien le roban una sesión en una sucursal, desde ahí no llega a los equipos que
administran la red. Medido en la última corrida: 3 de 3 orígenes prohibidos sin respuesta
(100 % de pérdida) y 2 de 2 permitidos con respuesta.

### 3.2 El borde con internet

Dos defensas, en la sede y en el centro de datos:

- **Anti-suplantación**, en la tabla `raw`, antes de cualquier otra decisión: todo paquete
  que llegue de un proveedor diciendo venir de 10.0.0.0/8, 172.16.0.0/12 o 192.168.0.0/16
  se tira.
- **Cortafuegos con estado**: de internet solo entra la respuesta a una conexión que el
  banco abrió, o una conexión nueva al HTTPS de `srv`. Lo demás se descarta en silencio,
  así que quien escanea no recibe ni un rechazo.

| Intento del hacker | Esperado | Resultado medido |
|---|---|---|
| Entrar a la red de gestión | frenado | 100 % de pérdida |
| Llegar a un equipo de la sucursal 1 | frenado | 100 % de pérdida |
| Hacer ping a los servidores | frenado | 100 % de pérdida |
| Abrir el puerto 80 de los servidores | frenado | cerrado o filtrado |
| Abrir el 443 de los servidores | abierto, es el servicio público | abierto |
| Abrir el autorizador | frenado | cerrado o filtrado |
| Disfrazarse de la sucursal 1 (origen 10.1.1.99) | frenado | 3 paquetes contados en el borde, 0 en el servidor |

El disfraz se comprueba en los dos extremos: en el servidor, que no llegó nada; en el
borde, que el contador de la regla `anti-suplantacion` subió. Mirar solo el servidor no
distingue entre «el filtro lo frenó» y «el paquete se perdió por otra razón».

### 3.3 El socio: lo que se le deja pasar

En la línea dedicada, una cadena `SOCIO` solo permite abrir HTTPS a 10.10.20.10. Ni el
socio abre otra cosa, ni el banco abre conexiones hacia él. Medido: autorizador por 443
abierto; autorizador por 80, servidores por 443, gestión y sucursal 1, sin acceso; y una
sucursal tampoco llega a la red de cajeros.

**Las dos capas son independientes a propósito.** Con las reglas de iptables quitadas a
propósito, 7 pruebas fallaron, pero el socio siguió sin llegar a la gestión ni a las
sucursales: lo frenó la capa de BGP, porque no tiene ruta (ADR-005).

---

## 4. Estrategia de Pruebas

### 4.1 Dos etapas: leer antes de levantar

| Etapa | Archivo | Qué hace | Cuánto tarda |
|---|---|---|---|
| Revisión estática | `pruebas/revisar.py` | Lee las configuraciones: direcciones y router-id únicos, cada enlace con dos puntas en routers distintos, el mismo área, costo y tipo de OSPF en las dos puntas, redes locales pasivas, vecinos BGP que existen y se responden, todo vecino eBGP del banco con filtro de salida | milisegundos |
| Red levantada | `pruebas/probar.py` | Las 68 pruebas restantes, contra la red en marcha | el resto de los 195 s de la corrida |

Si la revisión estática falla, el CI no levanta la red: el error que se ve leyendo un
archivo no merece un minuto de contenedores. El pull request de demostración
(`daveprojectdev/lab-red-banco#1`) trae un costo mal puesto y el CI se detiene en 5
segundos.

### 4.2 Los diez grupos

| Grupo | Pruebas | Qué comprueba | Qué error atraparía |
|---|---|---|---|
| configuracion | 7 | La revisión estática de la Sección 4.1 | Un costo distinto en cada punta, una IP repetida |
| ospf | 5 | La convergencia y cada vecino en Full por su enlace, sin vecinos de más | Un cable mal conectado, un área equivocada |
| bgp | 7 | Cada sesión establecida, y que el banco no hace de tránsito | Un filtro olvidado |
| rutas | 4 | Cada router aprende las redes de los demás | Una red que nadie anuncia |
| camino | 14 | La interfaz de salida de cada flujo y `traceroute` salto por salto | La red funciona, pero por el respaldo |
| alcance | 10 | Ping entre redes y a internet | Algo que de verdad no llega |
| acceso | 5 | Quién llega y quién no a la gestión | Una regla que no se aplicó |
| ataques | 9 | Los siete intentos del hacker, más dos pruebas previas | Un cortafuegos abierto de más |
| socio | 8 | Lo que aprende, lo que anuncia y lo que puede abrir | Un filtro de BGP flojo |
| caidas | 6 | Dos cortes: pérdida, respaldo y vuelta | Un respaldo que no devuelve el tráfico |

### 4.3 Lo esperado sale del diseño, no de la configuración

La cabecera de `probar.py` declara, a mano, los loopbacks, las redes de cada router, los
vecinos OSPF, las sesiones BGP, los caminos y los bloqueos. Es el mismo principio que en QA
de software: el oráculo no puede salir del código que se prueba (ADR-003). Su costo es que
un cambio de diseño se escribe dos veces, en `configs/` y en `probar.py`; ese doble
registro es justamente lo que detecta que uno de los dos se equivocó.

### 4.4 Una prueba que tiene que poder fallar

Antes de comprobar que el hacker no abre el 80 ni el 443 de más, una prueba verifica desde
el propio servidor que sí escucha en 80 y en 443. Sin esa prueba previa, «cerrado desde
fuera» podía significar «no hay nada escuchando», y la prueba pasaba por la razón
equivocada (Sección 5.3).

---

## 5. Patrones de Resiliencia

Cuatro fallos aparecieron construyendo el laboratorio. Ninguno era un error de tecleo: los
cuatro venían de suponer algo sobre la red o sobre la prueba que no era cierto.

### 5.1 Se asumía que «Full» significa que la red convergió

**El supuesto.** Las pruebas empezaban en cuanto todos los vecinos OSPF quedaban en Full.
**Por qué no dio la cara.** Los pings pasaban: había conectividad, por el respaldo. La
corrida `a8d79e3` dio 5 fallos de camino; el centro de datos todavía mandaba el tráfico por
el enlace de 100 cuando las rutas buenas aún no habían entrado. **El cambio.** La
convergencia se define como vecinos en Full, sesiones BGP arriba, todas las rutas del
diseño presentes y la tabla quieta 6 s. En la última corrida tardó 19 s. **La regla.** Una
prueba tiene que esperar el estado correcto, no un indicador que se le parece.

### 5.2 Se asumía que la sesión iBGP arranca cuando puede

**El supuesto.** La iBGP entre loopbacks sube sola en cuanto hay ruta. **Por qué no dio la
cara.** Arrancaba antes de que OSPF diera ruta a los loopbacks, quedaba en Active y FRR
espera 120 s para reintentar. Mientras tanto, el centro de datos salía a internet por isp2,
y todo llegaba. **El cambio.** `timers connect 5` en cada vecino BGP. Dos pruebas de camino
lo atraparon; ningún ping lo habría hecho. **La regla.** Un temporizador por defecto es un
supuesto de otro sobre tu red.

### 5.3 Se asumía que «cerrado desde fuera» lo cerró el cortafuegos

**El supuesto.** Si el hacker no abre el 80, es que el borde lo frena. **Por qué no dio la
cara.** En ese servidor nada escuchaba en el 80: la prueba pasaba desde su primera
corrida. **El cambio.** El servidor arranca nginx en 80 y 443, y una prueba previa lo
comprueba desde dentro (Sección 4.4). **La regla.** Una prueba que nunca pudo fallar no
prueba nada.

### 5.4 Se asumía que medir la caída es mirar si sigue llegando

**El supuesto.** Si después del corte el ping responde, el respaldo funciona. **Por qué no
dio la cara.** Responder después no dice cuánto se perdió durante, ni si el tráfico vuelve
al principal al reponer el enlace. **El cambio.** Pings cada 100 ms mientras se corta,
conteo de los perdidos contra un tope (3 s para la sucursal, 15 s para el proveedor), el
camino de respaldo verificado salto por salto y la vuelta comprobada al reponer.

| Se corta | Camino de respaldo | Perdido | Vuelve solo al principal |
|---|---|---|---|
| Principal de la sucursal 1 (sede–suc1) | suc1 → cpd | 0,0 s, 0 paquetes | en 19 s |
| Salida por isp1 (sede–isp1) | suc1 → sede → cpd → isp2 → nube | 0,2 s, 2 paquetes | en 11 s |

⚠️ El tiempo de vuelta incluye los 6 s que la prueba espera para confirmar que la tabla
se quedó quieta: restarlos antes de compararlo con una red real.

**La regla.** Un respaldo se prueba de ida y de vuelta.

---

## 6. CI y Publicación

### 6.1 El flujo

`.github/workflows/lab.yml`, en `ubuntu-24.04`, en cada push a `main`, en cada pull
request y cada lunes a las 06:00 de Panamá:

1. Avisa a qa.davidameth.dev que la corrida empezó.
2. Corre `revisar.py`. Si falla, salta a un resumen sin red.
3. Instala Containerlab 0.68.0, levanta la red y corre `probar.py`.
4. Vuelca el estado de los routers y de las reglas, para depurar.
5. Guarda `resumen.json` y `escena.json` como artefactos.
6. Solo desde `main`: publica el resumen en `daveprojectdev/qa` y, si pasó, reemplaza la
   escena que dibuja la vista de davidameth.dev.

El repo es público, así que GitHub Actions no cobra los minutos. Un pull request corre las
pruebas pero no publica: sirve para frenar errores, no para el historial.

### 6.2 Lo que se publica

| Archivo | Dónde | Qué trae |
|---|---|---|
| Resumen de la corrida | `daveprojectdev/qa`, `data/corridas/lab-red/` | Las 75 pruebas con su título, estado y duración |
| Escena | qa.davidameth.dev/escenas/lab-red.json | Los caminos medidos, los cortes, los ataques y el socio |

La escena solo se reemplaza cuando la corrida pasa, así que la vista del portafolio
siempre dibuja una corrida en verde. La página de davidameth.dev no la lee en vivo: un
generador (`npm run lab:datos`) la copia a los datos del sitio, y una huella avisa cuando
la publicada cambió y la copia quedó atrás.

### 6.3 Cifras de operación

| Dato | Valor |
|---|---|
| Duración de una corrida completa | 195 s |
| Convergencia desde que arranca la red | 19 s |
| Pruebas | 75, todas en verde |
| Costo | $0 (repo público) |

---

## 7. Deuda Técnica y Huecos Conocidos

Esta sección existe para que el documento sea utilizable y no promocional.

| Asunto | Estado | Riesgo |
|---|---|---|
| Sin NAT | El banco anuncia 10.0.0.0/8 directamente para que se vea BGP sin otra capa encima | Un banco real usa direcciones privadas por dentro; este diseño no se copia tal cual |
| Sin BFD | En contenedores, apagar una interfaz avisa al otro lado al instante, y por eso la pérdida medida es casi cero | En una red real, un corte más allá de un switch tarda hasta que vence el temporizador (40 s en OSPF por defecto). Las cifras de la Sección 5.4 no se trasladan a hardware |
| Sin Batfish | `revisar.py` cubre los errores de este diseño en milisegundos; Batfish lee FRR solo dentro del formato de Cumulus Linux y su imagen pesa unos 2 GB | La revisión estática solo atrapa lo que alguien pensó en revisar |
| Un área de OSPF, solo IPv4 | Basta para seis routers internos | Lo que se aprende de áreas y de IPv6 no está aquí |
| Temporizadores de BGP de 3 y 9 s | Para que las pruebas no tarden | Un proveedor real no los aceptaría |
| `network-multitool:latest` | Los equipos de prueba usan una etiqueta que se mueve; FRR sí va fijo en 10.2.1 | Una corrida puede cambiar de resultado sin un commit |
| El diseño se escribe dos veces | En `configs/` y en la cabecera de `probar.py` (ADR-003) | Un cambio de diseño que se olvida en uno de los dos falla en rojo, que es lo que se busca, pero cuesta el doble |

---

## Apéndice A — Cronología condensada

| Etapa | Contenido | Dónde vive en este documento |
|---|---|---|
| Fase 1 (`4edf008`) | OSPF en sede, centro de datos y dos sucursales con respaldo; vecinos, rutas, camino y alcance | Secciones 1 y 2.1 |
| Convergencia (`08825ae`) | Full no basta: rutas del diseño y tabla quieta 6 s; 23 pruebas | Sección 5.1 |
| Fase 2 (`a8d79e3`) | BGP con dos proveedores, regla de gestión, revisión estática, cortes medidos; 5 fallos | Secciones 2.2, 3.1, 4.1 y 5.4 |
| iBGP (`619745a`) | Reintento cada 5 s; 56 pruebas en verde | Sección 5.2 |
| Fase 2b (`757873d`) | Hacker, borde con estado, anti-suplantación y socio; 2 fallos | Secciones 3.2 y 3.3 |
| Servidores (`b21322f`) | nginx en 80 y 443 y la prueba previa; 75 pruebas | Sección 5.3 |
| Cajeros (`74dae2a`) | El socio pasa a ser la red de cajeros automáticos y su servidor, el autorizador | Sección 2.3 |
| Escena (`acc3c32`) | El CI publica los caminos medidos para la vista del portafolio | Sección 6.2 |

La guía de estudio, con las preguntas de entrevista, está en `docs/guia.md`.

---

## Apéndice B — Referencia rápida

| Categoría | Dato |
|---|---|
| Red | Containerlab 0.68.0, FRRouting 10.2.1, iptables |
| Equipos de prueba | `ghcr.io/hellt/network-multitool` |
| Pruebas | Python, solo la biblioteca estándar: `revisar.py` y `probar.py` |
| CI | GitHub Actions, `ubuntu-24.04`, en cada push, cada pull request y cada lunes |
| Resultados | qa.davidameth.dev, suite «Lab de red» |
| Volumen | 8 routers, 9 equipos, 19 enlaces, 4 sistemas autónomos externos más el del banco |
| Cifras | 75 pruebas, 195 s por corrida, convergencia en 19 s, peor corte 0,2 s |
| Costo de operación | $0 |

---

## Apéndice C — Registros de Decisiones Arquitectónicas

### ADR-001 — Cómo se levanta la red

**Contexto.** La red tiene que existir entera en cada corrida del CI, en un runner gratuito
que dura minutos, y tiene que poder levantarse igual en una laptop.

**Opciones consideradas.**
- *GNS3 o EVE-NG.* Emuladores con imágenes de fabricante; piden una máquina virtual
  dedicada, licencias o imágenes que no se pueden publicar, y no corren en un runner
  estándar.
- *Mininet.* Liviano, pero pensado para SDN; los routers con OSPF y BGP reales se arman a
  mano encima.
- *Containerlab con FRRouting.* La topología es un YAML, cada router es un contenedor con un
  software de routing que también corre en equipos reales, y se levanta con un comando.

**Decisión.** Containerlab con FRRouting.

**Consecuencias.** La red entera vive en git y se levanta en un runner de GitHub sin costo.
Se pierde la sintaxis de un fabricante concreto: lo que se practica es el protocolo, no los
comandos de un equipo en particular. Y los cortes en contenedores se detectan al instante,
lo que hace optimistas las cifras de caída (Sección 7).

---

### ADR-002 — Cómo decide el banco por dónde sale y por dónde vuelve

**Contexto.** Dos proveedores, uno principal y uno de respaldo. La salida y la entrada se
controlan con mecanismos distintos, y el banco no debe transportar tráfico ajeno.

**Opciones consideradas.**
- *Dos rutas por defecto estáticas con distinta distancia.* Simple, pero no dice nada a
  internet sobre por dónde volver, y no reacciona a la caída de una sesión remota.
- *Preferencia local para salir, AS prepend para que vuelvan, y filtro de salida.* Los
  tres mecanismos estándar de BGP, cada uno con una prueba propia.
- *MED hacia los proveedores.* Solo sirve entre dos enlaces al mismo proveedor; aquí son
  dos sistemas autónomos distintos.

**Decisión.** Preferencia local 50 para lo que llega de isp2, AS prepend de 65100 tres
veces más hacia isp2, y `route-map` que solo anuncia 10.0.0.0/8.

**Consecuencias.** El corte de isp1 cuesta 0,2 s y la vuelta es automática. El prepend no
es una orden: internet puede ignorarlo, y en una red real un proveedor con políticas
propias podría seguir mandando tráfico por isp2. El laboratorio lo comprueba solo contra su
propia nube.

---

### ADR-003 — De dónde sale lo que las pruebas esperan

**Contexto.** Las pruebas necesitan saber qué vecino, qué ruta y qué camino es el correcto.

**Opciones consideradas.**
- *Derivarlo de las configuraciones.* Una sola fuente de verdad, sin duplicar; pero si la
  configuración se equivoca, la prueba copia el error y pasa.
- *Escribirlo a mano a partir del diseño, en la cabecera de `probar.py`.* El diseño queda
  dos veces: en `configs/` y en las pruebas.

**Decisión.** A mano, a partir del diseño.

**Consecuencias.** Las pruebas atrapan configuraciones que funcionan pero no cumplen el
diseño, como el respaldo de la sucursal 2 con costo 10, que dejó todos los pings en verde y
las pruebas de camino en rojo. A cambio, cada cambio de diseño se escribe dos veces. Si el
laboratorio creciera a decenas de routers, este ADR debería revisarse hacia un diseño
declarado en un archivo propio del que salgan las dos cosas.

---

### ADR-004 — Qué se revisa antes de levantar la red

**Contexto.** Levantar la red y esperar la convergencia cuesta más de un minuto. Los 7
errores que busca `revisar.py` se ven leyendo un archivo.

**Opciones consideradas.**
- *Nada: dejar que las pruebas con la red levantada lo encuentren.* Funciona, pero cada
  error de tecleo cuesta una corrida completa.
- *Batfish.* Analiza configuraciones sin levantar la red, con un modelo completo; lee FRR
  solo dentro del formato de Cumulus Linux y su imagen pesa unos 2 GB.
- *Una revisión propia en Python.* Cubre solo lo que alguien pensó en revisar, pero corre
  en milisegundos y sin instalar nada.

**Decisión.** Una revisión propia, `revisar.py`, con 7 comprobaciones.

**Consecuencias.** El pull request de demostración se detiene en 5 segundos. El costo:
lo que `revisar.py` no sabe buscar llega hasta las pruebas con red, que lo encuentran más tarde y
más caro. Sumar Batfish sigue en la Sección 7.

---

### ADR-005 — Cómo se limita al socio

**Contexto.** La red de cajeros tiene que llegar a un servidor del banco, el autorizador, y
a nada más.

**Opciones consideradas.**
- *Solo un cortafuegos en la línea dedicada.* Una capa; si la regla se borra, el socio ve
  todo lo que su tabla de rutas le permita.
- *Solo filtrar lo que aprende por BGP.* Una capa; si alguien le agrega una ruta estática,
  llega a todo.
- *Las dos, cada una suficiente por sí sola.*

**Decisión.** Las dos: BGP le enseña solo 10.10.20.0/24, y la cadena `SOCIO` solo deja
abrir el 443 del autorizador.

**Consecuencias.** Con las reglas de iptables quitadas a propósito, el socio siguió sin
llegar a la gestión ni a las sucursales. Se paga en pruebas: cada capa necesita sus propias
pruebas, porque una capa que nunca se ejercita sola puede estar rota sin que se note.
