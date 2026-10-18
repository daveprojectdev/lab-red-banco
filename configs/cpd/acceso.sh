#!/bin/sh
# Reglas de acceso del centro de datos: el borde con isp2 y la línea del socio.
set -e

# ── Borde con internet (eth5, isp2): lo mismo que la sede con isp1 ──
iptables -t raw -A PREROUTING -i eth5 -s 10.0.0.0/8 -m comment --comment anti-suplantacion -j DROP
iptables -t raw -A PREROUTING -i eth5 -s 172.16.0.0/12 -m comment --comment anti-suplantacion -j DROP
iptables -t raw -A PREROUTING -i eth5 -s 192.168.0.0/16 -m comment --comment anti-suplantacion -j DROP

iptables -N BORDE
iptables -A FORWARD -i eth5 -j BORDE
iptables -A BORDE -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A BORDE -d 10.10.10.10 -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
iptables -A BORDE -m comment --comment borde-descartado -j DROP

# ── Línea dedicada del socio (eth6) ──
# El socio, una procesadora de pagos, solo puede abrir conexiones HTTPS al
# servidor de pagos (10.10.20.10). Nada más del banco, ni en un sentido ni en
# el otro: ni el socio entra a otra red, ni el banco abre conexiones hacia él.
iptables -N SOCIO
iptables -A FORWARD -i eth6 -j SOCIO
iptables -A FORWARD -o eth6 -j SOCIO
iptables -A SOCIO -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A SOCIO -i eth6 -d 10.10.20.10 -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
iptables -A SOCIO -m comment --comment socio-descartado -j DROP
