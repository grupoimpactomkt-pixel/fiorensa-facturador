# fiorensa-facturador
Servicio HTTP de facturación electrónica AFIP (WSFEv1) para la app de ventas de Fiorensa.
Sin secretos: el certificado y la clave se pasan por variables de entorno base64 (ARCA_CERT_B64 / ARCA_KEY_B64).
Corre con `python facturador.py serve 8077`. Deploy vía Dockerfile.
