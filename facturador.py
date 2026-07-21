# -*- coding: utf-8 -*-
"""
Facturador ARCA — FIORENSA GLOBAL FOODS S.A.
Firma WSAA con openssl + emite Factura A/B por WSFEv1 (producción), directo, sin AfipSDK.
Un solo archivo, solo stdlib. Corre como servicio HTTP o por CLI para probar.
Base: La-Colonia/POS-CAJA/arca/facturador.py (probado en producción 18-jul-2026).

Uso:
  python facturador.py ultimo 6            -> lee última factura B del pto vta 6 (SOLO LECTURA)
  python facturador.py emitir-test 6       -> EMITE una Factura B de $1 real (pedir OK antes)
  python facturador.py serve 8077          -> levanta el HTTP en el puerto 8077

Endpoint HTTP:
  POST /factura   body JSON:
    { "ptovta":6, "tipo":"B", "doc_tipo":99, "doc_nro":0,
      "items":[{"desc":"Cerveza","cant":1,"precio":1500.0,"iva":21}] }
  respuesta:
    { "ok":true, "tipo":"B", "ptovta":6, "nro":1, "cae":"...", "cae_vto":"20260728",
      "importe":1500.0, "fecha":"20260718", "cuit":"30719011396",
      "qr_url":"https://www.afip.gob.ar/fe/qr/?p=..." }
"""
import subprocess, base64, datetime, re, urllib.request, urllib.error, sys, os, ssl, json, html

# --- config (en el VPS se puede pisar por env) ---
BASE = os.environ.get("ARCA_DIR", os.path.dirname(os.path.abspath(__file__)))
# En el server pasamos cert/key como base64 por env (ARCA_CERT_B64 / ARCA_KEY_B64) para no montar archivos.
def _b64_a_archivo(b64, nombre):
    ruta = os.path.join(BASE, nombre)
    with open(ruta, "wb") as f:
        f.write(base64.b64decode(b64))
    return ruta
CERT = _b64_a_archivo(os.environ["ARCA_CERT_B64"], "_cert.pem") if os.environ.get("ARCA_CERT_B64") else os.environ.get("ARCA_CERT", os.path.join(BASE, "fiorensa.crt"))
KEY  = _b64_a_archivo(os.environ["ARCA_KEY_B64"],  "_key.pem")  if os.environ.get("ARCA_KEY_B64")  else os.environ.get("ARCA_KEY",  os.path.join(BASE, "fiorensa.key"))
CUIT = os.environ.get("ARCA_CUIT", "30719011396")
PTOVTA_DEF = int(os.environ.get("ARCA_PTOVTA", "6"))
# datos para el PDF del comprobante (pisables por env)
RAZON_SOCIAL = os.environ.get("ARCA_RAZON", "FIORENSA GLOBAL FOODS S.A.")
DOMICILIO    = os.environ.get("ARCA_DOMICILIO", "Mar del Plata, Buenos Aires")
IIBB         = os.environ.get("ARCA_IIBB", CUIT)          # convenio multilateral usa el CUIT
INICIO_ACT   = os.environ.get("ARCA_INICIO", "")          # ej "01/01/2024" (opcional)
CACHE = os.path.join(BASE, "ta_cache.json")
SECRET = os.environ.get("ARCA_SECRET", "")   # si está seteado, exige header X-Fact-Token
LOG = os.path.join(BASE, "facturas.log")
BIND = os.environ.get("ARCA_BIND", "0.0.0.0")

WSAA_URL = "https://wsaa.afip.gov.ar/ws/services/LoginCms"
WSFE_URL = "https://servicios1.afip.gov.ar/wsfev1/service.asmx"

# tipo comprobante AFIP
CBTE = {"A": 1, "B": 6, "C": 11}
# condición IVA receptor (RG 5616, obligatorio): A -> Resp. Inscripto(1); B -> Consumidor Final(5)
COND_IVA_RECEP = {"A": 1, "B": 5, "C": 5}
# alícuota IVA -> Id AFIP
IVA_ID = {0: 3, 10.5: 4, 21: 5, 27: 6, 5: 8, 2.5: 9}

# AFIP usa DH viejo -> hay que bajar el nivel de seguridad SSL para el handshake
SSLCTX = ssl.create_default_context()
SSLCTX.set_ciphers("DEFAULT@SECLEVEL=1")


def _log(msg):
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        open(LOG, "a", encoding="utf-8").write(f"{ts}  {msg}\n")
    except Exception:
        pass


def _post(url, body, soapaction):
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": soapaction})
    with urllib.request.urlopen(req, timeout=30, context=SSLCTX) as r:
        return r.read().decode("utf-8", "replace")


def get_ta():
    """Devuelve (token, sign) del WSAA; cachea 12h en disco y reusa mientras valga."""
    if os.path.exists(CACHE):
        c = json.load(open(CACHE))
        if c.get("exp", 0) > datetime.datetime.now().timestamp() + 300:
            return c["token"], c["sign"]
    now = datetime.datetime.now(datetime.timezone.utc).astimezone()
    gen = (now - datetime.timedelta(minutes=10)).replace(microsecond=0).isoformat()
    exp = (now + datetime.timedelta(minutes=10)).replace(microsecond=0).isoformat()
    tra = (f'<?xml version="1.0" encoding="UTF-8"?>\n<loginTicketRequest version="1.0">'
           f'<header><uniqueId>{int(now.timestamp())}</uniqueId>'
           f'<generationTime>{gen}</generationTime><expirationTime>{exp}</expirationTime></header>'
           f'<service>wsfe</service></loginTicketRequest>')
    tra_p = os.path.join(BASE, "tra.xml"); cms_p = os.path.join(BASE, "tra.cms")
    open(tra_p, "w", encoding="utf-8").write(tra)
    subprocess.run(["openssl", "smime", "-sign", "-in", tra_p, "-out", cms_p,
        "-signer", CERT, "-inkey", KEY, "-outform", "DER", "-nodetach"], check=True)
    cms = base64.b64encode(open(cms_p, "rb").read()).decode()
    body = (f'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov"><soapenv:Header/><soapenv:Body>'
            f'<wsaa:loginCms><wsaa:in0>{cms}</wsaa:in0></wsaa:loginCms></soapenv:Body></soapenv:Envelope>')
    resp = _post(WSAA_URL, body, "")
    tok = re.search(r"&lt;token&gt;(.*?)&lt;/token&gt;", resp, re.S)
    sig = re.search(r"&lt;sign&gt;(.*?)&lt;/sign&gt;", resp, re.S)
    ex  = re.search(r"&lt;expirationTime&gt;(.*?)&lt;/expirationTime&gt;", resp, re.S)
    if not (tok and sig):
        raise RuntimeError("WSAA no devolvió token. Respuesta: " + resp[:1500])
    exp_ts = datetime.datetime.fromisoformat(ex.group(1)).timestamp() if ex else now.timestamp() + 36000
    json.dump({"token": tok.group(1), "sign": sig.group(1), "exp": exp_ts}, open(CACHE, "w"))
    return tok.group(1), sig.group(1)


def _auth_xml():
    t, s = get_ta()
    return (f"<ar:Auth><ar:Token>{html.escape(t)}</ar:Token>"
            f"<ar:Sign>{html.escape(s)}</ar:Sign><ar:Cuit>{CUIT}</ar:Cuit></ar:Auth>")


def ultimo(ptovta, cbte_tipo):
    body = (f'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:ar="http://ar.gov.afip.dif.FEV1/"><soap:Body><ar:FECompUltimoAutorizado>'
            f'{_auth_xml()}<ar:PtoVta>{ptovta}</ar:PtoVta><ar:CbteTipo>{cbte_tipo}</ar:CbteTipo>'
            f'</ar:FECompUltimoAutorizado></soap:Body></soap:Envelope>')
    r = _post(WSFE_URL, body, "http://ar.gov.afip.dif.FEV1/FECompUltimoAutorizado")
    m = re.search(r"<CbteNro>(\d+)</CbteNro>", r)
    if not m:
        errs = re.findall(r"<(?:Msg|Err)>(.*?)</(?:Msg|Err)>", r, re.S)
        raise RuntimeError(f"FECompUltimoAutorizado sin número. {errs or r[:1200]}")
    return int(m.group(1))


def _iva_grupos(items):
    """Agrupa por alícuota. precio es FINAL (IVA incluido). Devuelve (neto,iva,total) por alícuota."""
    por = {}
    for it in items:
        pct = float(it.get("iva", 21))
        tot = round(float(it["precio"]) * float(it.get("cant", 1)), 2)
        por[pct] = por.get(pct, 0.0) + tot
    grupos = []
    for pct, tot in por.items():
        tot = round(tot, 2)
        neto = round(tot / (1 + pct / 100.0), 2)
        iva = round(tot - neto, 2)
        grupos.append((pct, neto, iva, tot))
    return grupos


def emitir(ptovta, tipo, doc_tipo, doc_nro, items, receptor=""):
    cbte = CBTE[tipo]
    grupos = _iva_grupos(items)
    imp_neto = round(sum(g[1] for g in grupos), 2)
    imp_iva  = round(sum(g[2] for g in grupos), 2)
    imp_tot  = round(imp_neto + imp_iva, 2)
    nro = ultimo(ptovta, cbte) + 1
    hoy = datetime.date.today().strftime("%Y%m%d")

    alic = "".join(
        f"<ar:AlicIva><ar:Id>{IVA_ID.get(pct, 5)}</ar:Id>"
        f"<ar:BaseImp>{neto:.2f}</ar:BaseImp><ar:Importe>{iva:.2f}</ar:Importe></ar:AlicIva>"
        for pct, neto, iva, _ in grupos)

    body = (
      '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
      'xmlns:ar="http://ar.gov.afip.dif.FEV1/"><soap:Body><ar:FECAESolicitar>'
      f'{_auth_xml()}<ar:FeCAEReq>'
      f'<ar:FeCabReq><ar:CantReg>1</ar:CantReg><ar:PtoVta>{ptovta}</ar:PtoVta>'
      f'<ar:CbteTipo>{cbte}</ar:CbteTipo></ar:FeCabReq>'
      '<ar:FeDetReq><ar:FECAEDetRequest>'
      '<ar:Concepto>1</ar:Concepto>'
      f'<ar:DocTipo>{doc_tipo}</ar:DocTipo><ar:DocNro>{doc_nro}</ar:DocNro>'
      f'<ar:CbteDesde>{nro}</ar:CbteDesde><ar:CbteHasta>{nro}</ar:CbteHasta><ar:CbteFch>{hoy}</ar:CbteFch>'
      f'<ar:ImpTotal>{imp_tot:.2f}</ar:ImpTotal><ar:ImpTotConc>0</ar:ImpTotConc>'
      f'<ar:ImpNeto>{imp_neto:.2f}</ar:ImpNeto><ar:ImpOpEx>0</ar:ImpOpEx>'
      f'<ar:ImpIVA>{imp_iva:.2f}</ar:ImpIVA><ar:ImpTrib>0</ar:ImpTrib>'
      '<ar:MonId>PES</ar:MonId><ar:MonCotiz>1</ar:MonCotiz>'
      f'<ar:CondicionIVAReceptorId>{COND_IVA_RECEP[tipo]}</ar:CondicionIVAReceptorId>'
      f'<ar:Iva>{alic}</ar:Iva>'
      '</ar:FECAEDetRequest></ar:FeDetReq></ar:FeCAEReq></ar:FECAESolicitar></soap:Body></soap:Envelope>')

    r = _post(WSFE_URL, body, "http://ar.gov.afip.dif.FEV1/FECAESolicitar")
    resultado = re.search(r"<Resultado>(\w)</Resultado>", r)
    cae = re.search(r"<CAE>(\d+)</CAE>", r)
    vto = re.search(r"<CAEFchVto>(\d+)</CAEFchVto>", r)
    obs = re.findall(r"<Obs>.*?<Code>(\d+)</Code>.*?<Msg>(.*?)</Msg>.*?</Obs>", r, re.S)
    errs = re.findall(r"<Err>.*?<Code>(\d+)</Code>.*?<Msg>(.*?)</Msg>.*?</Err>", r, re.S)
    if not (cae and resultado and resultado.group(1) == "A"):
        raise RuntimeError(f"AFIP rechazó (Resultado={resultado and resultado.group(1)}). "
                           f"Errores={errs} Obs={obs}\nRESP:{r[:2000]}")

    qr_url = _qr(hoy, ptovta, cbte, nro, imp_tot, doc_tipo, doc_nro, cae.group(1))
    res = {"ok": True, "tipo": tipo, "ptovta": ptovta, "nro": nro,
           "cae": cae.group(1), "cae_vto": vto.group(1) if vto else "",
           "importe": imp_tot, "neto": imp_neto, "iva": imp_iva,
           "fecha": hoy, "cuit": CUIT, "qr_url": qr_url, "cbte": cbte,
           "obs": [f"{c}: {m}" for c, m in obs]}
    res["pdf_b64"] = _pdf(res, items, receptor, doc_tipo, doc_nro)  # None si falla el PDF (la factura ya es válida)
    return res


def _qr(fecha, ptovta, tipo_cmp, nro_cmp, importe, tipo_doc, nro_doc, cae):
    """QR obligatorio AFIP: URL con payload base64 JSON (https://www.afip.gob.ar/fe/qr/)."""
    p = {"ver": 1, "fecha": f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}", "cuit": int(CUIT),
         "ptoVta": ptovta, "tipoCmp": tipo_cmp, "nroCmp": nro_cmp, "importe": round(importe, 2),
         "moneda": "PES", "ctz": 1, "tipoDocRec": tipo_doc, "nroDocRec": nro_doc,
         "tipoCodAut": "E", "codAut": int(cae)}
    b64 = base64.b64encode(json.dumps(p, separators=(",", ":")).encode()).decode()
    return "https://www.afip.gob.ar/fe/qr/?p=" + b64


def _ar(n):
    """Formato de moneda argentino: 1.234,56"""
    return f"{n:,.2f}".replace(",", "·").replace(".", ",").replace("·", ".")


def _pdf(res, items, receptor="", doc_tipo=99, doc_nro=0):
    """Comprobante PDF de una carilla con el QR de AFIP. Devuelve base64, o None si falla
    (la factura AFIP ya es válida sin el PDF; nunca tumbamos la emisión por esto)."""
    try:
        from fpdf import FPDF
        import segno, io
        _s = lambda x: str(x).encode("latin-1", "replace").decode("latin-1")  # fuente core = Latin-1
        tipo = res["tipo"]
        f = res["fecha"]; fstr = f"{f[6:]}/{f[4:6]}/{f[:4]}"
        vto = res.get("cae_vto", ""); vstr = f"{vto[6:]}/{vto[4:6]}/{vto[:4]}" if len(vto) == 8 else vto
        comp = f'{res["ptovta"]:04d}-{res["nro"]:08d}'
        doc_lbl = {80: "CUIT", 96: "DNI", 99: ""}.get(doc_tipo, "Doc")
        recep_doc = f"{doc_lbl} {doc_nro}" if doc_nro else "Consumidor Final"

        pdf = FPDF(format="A4"); pdf.set_auto_page_break(False); pdf.add_page()
        L, R, W = 15, 195, 180

        # --- recuadro letra (centro) ---
        pdf.set_line_width(0.4); pdf.rect(97, 12, 16, 16)
        pdf.set_font("Helvetica", "B", 26); pdf.set_xy(97, 13); pdf.cell(16, 12, tipo, align="C")
        pdf.set_font("Helvetica", "", 7); pdf.set_xy(97, 24); pdf.cell(16, 3, f'COD. {res["cbte"]:02d}', align="C")

        # --- emisor (izq) ---
        pdf.set_xy(L, 13); pdf.set_font("Helvetica", "B", 15); pdf.cell(80, 7, RAZON_SOCIAL)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(L, 21); pdf.cell(80, 4, DOMICILIO)
        pdf.set_xy(L, 25); pdf.cell(80, 4, "IVA Responsable Inscripto")

        # --- comprobante (der) ---
        pdf.set_font("Helvetica", "B", 13); pdf.set_xy(120, 13); pdf.cell(R - 120, 7, "FACTURA", align="R")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_xy(120, 21); pdf.cell(R - 120, 4, f"N° {comp}", align="R")
        pdf.set_xy(120, 25); pdf.cell(R - 120, 4, f"Fecha: {fstr}", align="R")

        # --- datos fiscales emisor ---
        pdf.set_xy(L, 31); pdf.set_font("Helvetica", "", 8)
        pdf.cell(90, 4, f"CUIT: {res['cuit']}   Ing. Brutos: {IIBB}")
        if INICIO_ACT:
            pdf.set_xy(120, 31); pdf.cell(R - 120, 4, f"Inicio actividades: {INICIO_ACT}", align="R")
        pdf.line(L, 37, R, 37)

        # --- receptor ---
        pdf.set_xy(L, 39); pdf.set_font("Helvetica", "B", 8); pdf.cell(20, 4, "Cliente:")
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 4, _s(f"{receptor or 'Consumidor Final'}   {recep_doc}"))
        pdf.line(L, 45, R, 45)

        # --- tabla items ---
        pdf.set_xy(L, 47); pdf.set_font("Helvetica", "B", 8)
        cols = [(90, "Descripción", "L"), (20, "Cant.", "R"), (35, "P. Unit.", "R"), (35, "Subtotal", "R")]
        for w, t, a in cols:
            pdf.cell(w, 6, t, border="B", align=a)
        pdf.ln(6); pdf.set_font("Helvetica", "", 8); y = pdf.get_y()
        for it in items:
            cant = float(it.get("cant", 1)); pu = float(it["precio"]); sub = round(pu * cant, 2)
            pdf.set_x(L)
            pdf.cell(90, 5, _s(it.get("desc", ""))[:55], align="L")
            pdf.cell(20, 5, f"{cant:g}", align="R")
            pdf.cell(35, 5, _ar(pu), align="R")
            pdf.cell(35, 5, _ar(sub), align="R")
            pdf.ln(5)

        # --- totales ---
        ty = max(pdf.get_y() + 4, 90)
        pdf.set_font("Helvetica", "", 9)
        for lbl, val in [("Neto Gravado", res["neto"]), ("IVA", res["iva"])]:
            pdf.set_xy(120, ty); pdf.cell(40, 5, lbl, align="R")
            pdf.cell(20, 5, _ar(val), align="R"); ty += 5
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(120, ty + 1); pdf.cell(40, 6, "TOTAL $", align="R")
        pdf.cell(20, 6, _ar(res["importe"]), align="R")

        # --- QR + CAE (footer) ---
        qy = 250
        buff = io.BytesIO()
        segno.make(res["qr_url"], error="m").save(buff, kind="png", scale=4, border=1)
        buff.seek(0); pdf.image(buff, x=L, y=qy, w=32)
        pdf.set_xy(L + 36, qy + 6); pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, f'CAE N°: {res["cae"]}'); pdf.ln(5)
        pdf.set_x(L + 36); pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, f"Vto. CAE: {vstr}")

        out = pdf.output()  # bytes/bytearray en fpdf2 2.8
        return base64.b64encode(bytes(out)).decode()
    except Exception as e:
        _log(f"PDF omitido: {e}")
        return None


def _ensure_pdf_libs():
    """Red de seguridad: si el contenedor arranca sin las libs del PDF (p.ej. un redeploy
    de EasyPanel que revierte el command), las instala al boot. No-op si ya están."""
    try:
        import fpdf, segno  # noqa: F401
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q",
                            "fpdf2", "segno"], check=True)
            _log("PDF libs instaladas en el arranque")
        except Exception as e:
            _log(f"No pude instalar libs PDF al boot: {e} (la factura sale igual, sin PDF)")


# ---- HTTP server ----
def serve(port):
    _ensure_pdf_libs()
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                self._send(200, {"ok": True, "cuit": CUIT})
            else:
                self._send(404, {"ok": False})

        def do_POST(self):
            try:
                if SECRET and self.headers.get("X-Fact-Token", "") != SECRET:
                    return self._send(401, {"ok": False, "error": "no autorizado"})
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                res = emitir(int(d.get("ptovta", PTOVTA_DEF)), d.get("tipo", "B"),
                             int(d.get("doc_tipo", 99)), int(d.get("doc_nro", 0)), d["items"],
                             d.get("receptor", ""))
                _log(f'OK tipo={res["tipo"]} pv={res["ptovta"]} nro={res["nro"]} '
                     f'cae={res["cae"]} imp={res["importe"]}')
                self._send(200, res)
            except Exception as e:
                _log(f"ERROR {e}")
                self._send(500, {"ok": False, "error": str(e)})

        def log_message(self, *a):
            pass

    print(f"Facturador ARCA escuchando en {BIND}:{port} (CUIT {CUIT})")
    ThreadingHTTPServer((BIND, port), H).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ultimo"
    if cmd == "ultimo":
        pv = int(sys.argv[2]) if len(sys.argv) > 2 else PTOVTA_DEF
        print(f"Última Factura B en pto vta {pv}: {ultimo(pv, 6)}")
    elif cmd == "emitir-test":
        pv = int(sys.argv[2]) if len(sys.argv) > 2 else PTOVTA_DEF
        r = emitir(pv, "B", 99, 0, [{"desc": "PRUEBA", "cant": 1, "precio": 1.0, "iva": 21}])
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8077)
    elif cmd == "pdf-demo":
        # arma un PDF de muestra SIN emitir contra AFIP (para ver el diseño)
        demo = {"tipo": "B", "fecha": "20260720", "cae_vto": "20260730", "cbte": 6,
                "ptovta": 6, "nro": 2, "cae": "86294661836322", "cuit": CUIT,
                "neto": 1239.67, "iva": 260.33, "importe": 1500.0,
                "qr_url": _qr("20260720", 6, 6, 2, 1500.0, 99, 0, "86294661836322")}
        items = [{"desc": "Cerveza Quilmes 1L", "cant": 6, "precio": 200.0, "iva": 21},
                 {"desc": "Gaseosa Coca 2.25L", "cant": 1, "precio": 300.0, "iva": 21}]
        b64 = _pdf(demo, items, "COMERCIO 24HS")
        if not b64:
            print("PDF falló (ver facturas.log)"); sys.exit(1)
        out = sys.argv[2] if len(sys.argv) > 2 else "demo_factura.pdf"
        open(out, "wb").write(base64.b64decode(b64))
        print(f"PDF de muestra escrito en {out} ({len(b64)} chars b64)")
    else:
        print(__doc__)
