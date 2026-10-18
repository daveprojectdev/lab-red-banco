#!/bin/sh
# Reglas de acceso de la sede: la red de gestión y el borde con isp1.
set -e

# ── Red de gestión (10.0.99.0/24, detrás de eth5) ──
# Desde ahí se administra la red, así que solo entran la sede y el centro de
# datos. Se aplica a lo que la sede reenvía hacia eth5.

# Las sucursales no entran: si a un cajero le roban la sesión, desde ahí no se
# llega a los equipos de la red.
iptables -A FORWARD -o eth5 -s 10.1.0.0/16 -j REJECT --reject-with icmp-admin-prohibited

# Internet tampoco: nada que llegue de isp1 pasa a gestión. (Por isp2 llega al
# centro de datos, que solo la alcanza a través de la sede: la misma regla.)
iptables -A FORWARD -o eth5 -i eth6 -j REJECT --reject-with icmp-admin-prohibited
iptables -A FORWARD -o eth5 ! -s 10.0.0.0/8 -j REJECT --reject-with icmp-admin-prohibited

# ── Borde con internet (eth6, isp1) ──

# Anti-suplantación: desde internet nunca puede llegar un paquete que diga venir
# de dentro del banco (10.0.0.0/8) ni de redes privadas. Va en la tabla raw,
# antes de cualquier otra decisión, y el contador deja ver cuántos se frenaron.
iptables -t raw -A PREROUTING -i eth6 -s 10.0.0.0/8 -m comment --comment anti-suplantacion -j DROP
iptables -t raw -A PREROUTING -i eth6 -s 172.16.0.0/12 -m comment --comment anti-suplantacion -j DROP
iptables -t raw -A PREROUTING -i eth6 -s 192.168.0.0/16 -m comment --comment anti-suplantacion -j DROP

# Lo que entra de internet solo pasa si es la respuesta a algo que el banco
# pidió, o si va al servicio público (HTTPS del servidor 10.10.10.10). Todo lo
# demás se descarta en silencio: un escaneo no recibe ni un «no».
iptables -N BORDE
iptables -A FORWARD -i eth6 -j BORDE
iptables -A BORDE -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A BORDE -d 10.10.10.10 -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
iptables -A BORDE -m comment --comment borde-descartado -j DROP
