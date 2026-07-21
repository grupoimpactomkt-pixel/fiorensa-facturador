# -*- coding: utf-8 -*-
"""
Push server de Fiore — Web Push (notificaciones al teléfono, estilo app).
Guarda las suscripciones por usuario en un archivo (sin tocar Supabase) y envía
las notificaciones con VAPID. Corre como servicio HTTP en el VPS.

Uso:
  python push_server.py serve 8078

Endpoints (POST JSON):
  /subscribe  { "usuario":"Oveja", "sub": <PushSubscription del navegador> }  -> guarda
  /unsubscribe{ "usuario":"Oveja", "endpoint":"..." }                          -> borra
  /notify     { "usuario":"Oveja", "titulo":"...", "body":"...", "url":"..." } -> envía a ese usuario
  /salud                                                                        -> {ok:true}

ENV:
  VAPID_PUBLIC / VAPID_PRIVATE  (base64url, ver push.env)
  VAPID_SUB   (mailto: de contacto, ej mailto:info@impactoestudiocreativo.com)
  PUSH_DIR    (dónde persistir subs.json; default junto al script — en el VPS montar un volumen)
"""
import os, sys, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from pywebpush import webpush, WebPushException
except Exception:  # se instala en el arranque del contenedor
    webpush = None

BASE = os.environ.get("PUSH_DIR", os.path.dirname(os.path.abspath(__file__)))
SUBS = os.path.join(BASE, "subs.json")
PUB  = os.environ.get("VAPID_PUBLIC", "")
PRIV = os.environ.get("VAPID_PRIVATE", "")
SUBJ = os.environ.get("VAPID_SUB", "mailto:info@impactoestudiocreativo.com")
BIND = os.environ.get("PUSH_BIND", "0.0.0.0")


def _load():
    try:
        return json.load(open(SUBS, encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    json.dump(d, open(SUBS, "w", encoding="utf-8"))


def add_sub(usuario, sub):
    d = _load(); arr = d.get(usuario, [])
    ep = sub.get("endpoint")
    arr = [s for s in arr if s.get("endpoint") != ep]  # dedup por endpoint
    arr.append(sub); d[usuario] = arr; _save(d)
    return len(arr)


def del_sub(usuario, endpoint):
    d = _load(); arr = [s for s in d.get(usuario, []) if s.get("endpoint") != endpoint]
    d[usuario] = arr; _save(d)


def notify(usuario, titulo, body, url="/"):
    """Envía a todas las suscripciones del usuario. Limpia las muertas (410/404)."""
    d = _load(); arr = d.get(usuario, []); ok = 0; vivos = []
    payload = json.dumps({"title": titulo, "body": body, "url": url}, ensure_ascii=False)
    for s in arr:
        try:
            webpush(subscription_info=s, data=payload,
                    vapid_private_key=PRIV, vapid_claims={"sub": SUBJ})
            ok += 1; vivos.append(s)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", 0)
            if code in (404, 410):
                continue  # suscripción muerta -> la sacamos
            vivos.append(s)  # error transitorio -> la dejamos
        except Exception:
            vivos.append(s)
    d[usuario] = vivos; _save(d)
    return ok


def serve(port):
    if webpush is None:
        raise SystemExit("Falta pywebpush (pip install pywebpush)")

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers(); self.wfile.write(b)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/salud"):
                self._send(200, {"ok": True, "pub": PUB})
            else:
                self._send(404, {"ok": False})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/subscribe"):
                    self._send(200, {"ok": True, "subs": add_sub(d["usuario"], d["sub"])})
                elif self.path.startswith("/unsubscribe"):
                    del_sub(d["usuario"], d.get("endpoint", "")); self._send(200, {"ok": True})
                elif self.path.startswith("/notify"):
                    enviados = notify(d["usuario"], d.get("titulo", "Fiore"),
                                      d.get("body", ""), d.get("url", "/"))
                    self._send(200, {"ok": True, "enviados": enviados})
                else:
                    self._send(404, {"ok": False, "error": "ruta"})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})

        def log_message(self, *a):
            pass

    print(f"Push server escuchando en {BIND}:{port}")
    ThreadingHTTPServer((BIND, port), H).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8078)
    else:
        print(__doc__)
