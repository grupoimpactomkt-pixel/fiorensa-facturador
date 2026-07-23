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
import os, sys, json, time
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


# ---------- usuarios (auto-registro de vendedores, sin tocar Supabase) ----------
USERS = os.path.join(BASE, "users.json")


def _uload():
    try:
        return json.load(open(USERS, encoding="utf-8"))
    except Exception:
        return {}


def _usave(d):
    json.dump(d, open(USERS, "w", encoding="utf-8"), ensure_ascii=False)


def user_register(data, pins_ocupados):
    """Registra un vendedor nuevo en estado 'pendiente'. Clave = PIN (único)."""
    pin = str(data.get("pin", "")).strip()
    nombre = str(data.get("nombre", "")).strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        return {"ok": False, "error": "El PIN tiene que ser de 4 a 6 números"}
    if not nombre:
        return {"ok": False, "error": "Falta el nombre"}
    d = _uload()
    # SEG-7: tope de pendientes para que un tercero no spamee el auto-registro / llene el disco
    if len([1 for u in d.values() if u.get("estado") == "pendiente"]) >= 25:
        return {"ok": False, "error": "Hay demasiados registros pendientes. Avisá a un admin."}
    if pin in d or pin in (pins_ocupados or []):
        return {"ok": False, "error": "Ese PIN ya está en uso, elegí otro"}
    d[pin] = {"pin": pin, "nombre": nombre, "apellido": str(data.get("apellido", "")).strip(),
              "tel": str(data.get("tel", "")).strip(), "direccion": str(data.get("direccion", "")).strip(),
              "rol": "vendedor", "estado": "pendiente", "zona": "", "created": data.get("ts", "")}
    _usave(d)
    return {"ok": True, "usuario": nombre}


def user_pending():
    return [u for u in _uload().values() if u.get("estado") == "pendiente"]


def user_decide(pin, aprobar, zona):
    d = _uload(); u = d.get(str(pin))
    if not u:
        return {"ok": False, "error": "No existe ese registro"}
    if aprobar:
        u["estado"] = "aprobado"; u["zona"] = zona or u.get("zona", "")
    else:
        del d[str(pin)]
    _usave(d)
    return {"ok": True}


def user_authmap():
    """PIN -> datos, solo de los APROBADOS. El router lo mergea con sus PINs fijos para el login."""
    return {p: {"nombre": u["nombre"], "rol": u.get("rol", "vendedor"), "zona": u.get("zona", "")}
            for p, u in _uload().items() if u.get("estado") == "aprobado"}


# ---------- PINs cambiables (SEG-2) + anti-brute-force, sin tocar Supabase ----------
PINS_F = os.path.join(BASE, "pins.json")    # overrides de PIN por persona: {nombre: {"pin","rol"}}
FAILS_F = os.path.join(BASE, "fails.json")  # anti-fuerza-bruta por IP: {ip: {"n":int,"ts":float}}
LOCK_MAX = 8        # intentos fallidos antes de bloquear
LOCK_WINDOW = 600   # ventana para contar fallos (10 min)
LOCK_BLOCK = 900    # tiempo de bloqueo una vez pasado el tope (15 min)


def _jload(f):
    try:
        return json.load(open(f, encoding="utf-8"))
    except Exception:
        return {}


def _jsave(f, d):
    json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False)


def set_pin(nombre, nuevo, rol):
    nombre = str(nombre or "").strip(); nuevo = str(nuevo or "").strip()
    if not nombre or not (nuevo.isdigit() and len(nuevo) == 6):
        return {"ok": False, "error": "PIN nuevo inválido (6 números)"}
    d = _jload(PINS_F); d[nombre] = {"pin": nuevo, "rol": rol or "vendedor"}; _jsave(PINS_F, d)
    return {"ok": True}


def pinmap():
    """Lo que consume el router para armar su mapa de login: overrides (cambios) + aprobados."""
    return {"ok": True, "overrides": _jload(PINS_F), "map": user_authmap()}


def login_gate(ip, ok):
    """Registra el intento de login por IP y devuelve si está bloqueado. ok=True resetea."""
    ip = str(ip or "?"); now = time.time(); d = _jload(FAILS_F); e = d.get(ip)
    if e and (now - e.get("ts", 0) > LOCK_WINDOW) and e.get("n", 0) < LOCK_MAX:
        e = None  # ventana vencida sin haber llegado al tope -> reset
    if ok:
        if ip in d:
            del d[ip]; _jsave(FAILS_F, d)
        return {"ok": True, "allow": True, "blocked": False}
    # bloqueado activo?
    if e and e.get("n", 0) >= LOCK_MAX and now - e.get("ts", 0) < LOCK_BLOCK:
        return {"ok": True, "allow": False, "blocked": True, "wait": int(LOCK_BLOCK - (now - e["ts"]))}
    n = (e.get("n", 0) if e else 0) + 1
    d[ip] = {"n": n, "ts": now}; _jsave(FAILS_F, d)
    blocked = n >= LOCK_MAX
    return {"ok": True, "allow": False, "blocked": blocked,
            "wait": LOCK_BLOCK if blocked else 0, "restantes": max(0, LOCK_MAX - n)}


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
                elif self.path.startswith("/user_register"):
                    self._send(200, user_register(d, d.get("pins_ocupados")))
                elif self.path.startswith("/user_pending"):
                    self._send(200, {"ok": True, "items": user_pending()})
                elif self.path.startswith("/user_approve"):
                    self._send(200, user_decide(d.get("pin"), True, d.get("zona", "")))
                elif self.path.startswith("/user_reject"):
                    self._send(200, user_decide(d.get("pin"), False, ""))
                elif self.path.startswith("/user_authmap"):
                    self._send(200, {"ok": True, "map": user_authmap()})
                elif self.path.startswith("/pinmap"):
                    self._send(200, pinmap())
                elif self.path.startswith("/set_pin"):
                    self._send(200, set_pin(d.get("nombre"), d.get("nuevo"), d.get("rol")))
                elif self.path.startswith("/login_gate"):
                    self._send(200, login_gate(d.get("ip"), bool(d.get("ok"))))
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
