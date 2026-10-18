# Seguridad

## Reportar una vulnerabilidad

Si encuentras un problema de seguridad en este repositorio, escríbeme a
**hola@davidameth.dev** con el asunto «Seguridad: lab-red-banco». Por favor no abras un
*issue* público mientras no esté corregido.

Respondo en un plazo de 3 días hábiles.

## Qué hay aquí y qué no

- Es un laboratorio: la red solo existe dentro de cada corrida de GitHub Actions y se
  apaga al terminar. Las direcciones son privadas (10.0.0.0/8) y no son las de ninguna
  red real.
- No hay contraseñas ni claves en el código. El único secreto es `QA_TOKEN`, un token de
  grano fino que solo puede escribir en el repositorio `qa`, y vive como secreto de
  GitHub Actions.

---

**English:** to report a security issue, email hola@davidameth.dev with the subject
“Security: lab-red-banco”. Please don't open a public issue until it's fixed.
