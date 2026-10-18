#!/bin/sh
# Regla de acceso a la red de gestión (10.0.99.0/24, detrás de eth5 de la
# sede). Desde ahí se administra la red, así que solo entran la sede y el
# centro de datos. Se aplica a lo que la sede reenvía hacia eth5.
set -e

# Las sucursales no entran: si a un cajero le roban la sesión, desde ahí no se
# llega a los equipos de la red.
iptables -A FORWARD -o eth5 -s 10.1.0.0/16 -j REJECT --reject-with icmp-admin-prohibited

# Internet tampoco: nada que llegue de isp1 pasa a gestión. (Por isp2 llega al
# centro de datos, que solo la alcanza a través de la sede: la misma regla.)
iptables -A FORWARD -o eth5 -i eth6 -j REJECT --reject-with icmp-admin-prohibited
iptables -A FORWARD -o eth5 ! -s 10.0.0.0/8 -j REJECT --reject-with icmp-admin-prohibited
