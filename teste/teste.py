# app.py — Backend Flask completo: HTChat + Sicoob + DANFSe (NFS-e Nacional)
# Requisitos:
#   pip install flask flask-cors requests

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import io
import time
import json
import tempfile
import base64
from typing import Dict, Any, Optional, Tuple, List
import requests

app = Flask(__name__)
CORS(app)

# =========================================================
# CONFIG / ENV
# =========================================================
SUPABASE_URL= "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_SERVICE_ROLE= "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0MzcxNDA4MCwiZXhwIjoyMDU5MjkwMDgwfQ.cbeC4ROB7GXbKUU47nDpnQFeIYaEcvUr8_szTRxFZOs"
SUPABASE_ANON_KEY= "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"
SUPABASE_KEY = SUPABASE_SERVICE_ROLE or SUPABASE_ANON_KEY

# DANFSe
DANFSE_TIMEOUT = int(os.getenv("DANFSE_TIMEOUT", "25"))
DANFSE_TRY_RESTRITA = os.getenv("DANFSE_TRY_RESTRITA", "1").strip() not in ("0", "false", "False")

# HTChat (GraphQL)
HTCHAT_DEFAULT_BASE_URL = os.getenv("HTCHAT_BASE_URL", "").strip()  # ex: https://seu-htchat.com
HTCHAT_DEFAULT_TOKEN = os.getenv("HTCHAT_TOKEN", "").strip()        # opcional se você não quiser buscar no Supabase
HTCHAT_TIMEOUT = int(os.getenv("HTCHAT_TIMEOUT", "30"))

# Sicoob (você pode configurar por ENV ou manter como estava no seu backend antigo)
SICOOB_TIMEOUT = int(os.getenv("SICOOB_TIMEOUT", "40"))

# OAuth
SICOOB_OAUTH_URL = os.getenv("SICOOB_OAUTH_URL", "").strip()
SICOOB_CLIENT_ID = os.getenv("SICOOB_CLIENT_ID", "").strip()
SICOOB_CLIENT_SECRET = os.getenv("SICOOB_CLIENT_SECRET", "").strip()
SICOOB_SCOPE = os.getenv("SICOOB_SCOPE", "").strip()  # se precisar

# APIs boleto
SICOOB_EMITIR_URL = os.getenv("SICOOB_EMITIR_URL", "").strip()
SICOOB_PDF_URL = os.getenv("SICOOB_PDF_URL", "").strip()

# =========================================================
# HELPERS
# =========================================================
def norm(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().strip('"').strip("'")
    if s.lower() in ("null", "undefined", "false"):
        return ""
    return s

def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def now_ms() -> int:
    return int(time.time() * 1000)

def jlog(tag: str, data: Any):
    # log simples (Render)
    try:
        print(tag, json.dumps(data, ensure_ascii=False)[:4000])
    except Exception:
        print(tag, str(data)[:2000])

# =========================================================
# SUPABASE REST
# =========================================================
def sb_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }

def sb_get(table: str, params: Dict[str, str], timeout: int = 25):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.get(url, headers=sb_headers(), params=params, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase GET {table} -> {r.status_code}: {r.text}")
    return r.json() if r.text else []

def sb_find_one(table: str, select: str, filters: Dict[str, str], order: str = "created_at.desc") -> Optional[Dict[str, Any]]:
    params = {"select": select, "limit": "1", "order": order}
    for k, v in filters.items():
        params[k] = v
    rows = sb_get(table, params=params) or []
    return rows[0] if rows else None

# =========================================================
# TEMP CERT FILES (para requests cert=(pem,key))
# =========================================================
class TempCertFiles:
    def __init__(self, pem_text: str, key_text: str):
        self.pem_text = pem_text
        self.key_text = key_text
        self.tmp = None
        self.pem_path = None
        self.key_path = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cert_")
        self.pem_path = os.path.join(self.tmp.name, "cert.pem")
        self.key_path = os.path.join(self.tmp.name, "key.pem")
        with open(self.pem_path, "w", encoding="utf-8") as f:
            f.write(self.pem_text)
        with open(self.key_path, "w", encoding="utf-8") as f:
            f.write(self.key_text)
        return (self.pem_path, self.key_path)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.tmp:
                self.tmp.cleanup()
        except Exception:
            pass

# =========================================================
# LOAD CERTS (sem cache global pra não misturar rotas)
# =========================================================
def load_sicoob_cert(user: str) -> Dict[str, Any]:
    user = norm(user)
    if not user:
        raise ValueError("Informe 'user'.")

    row = sb_find_one(
        "certifica_sicoob",
        select="id,user,pem,key,conta,numerocliente,cliente_id,created_at",
        filters={"user": f"eq.{user}"},
        order="created_at.desc"
    )
    if not row:
        raise RuntimeError("certifica_sicoob: não encontrei registro para este user.")
    if not row.get("pem") or not row.get("key"):
        raise RuntimeError("certifica_sicoob: registro sem pem/key.")
    return row

def load_dfe_cert(user: str, cnpj_cpf: str) -> Dict[str, Any]:
    user = norm(user)
    doc = only_digits(cnpj_cpf)

    if not user:
        raise ValueError("Informe 'user'.")
    if len(doc) not in (11, 14):
        raise ValueError("Informe 'cnpj_cpf' com 11 ou 14 dígitos.")

    row = sb_find_one(
        "certifica_dfe",
        select='id,user,empresa,"cnpj/cpf",pem,key,created_at',
        filters={"user": f"eq.{user}", "cnpj/cpf": f"eq.{doc}"},
        order="created_at.desc"
    )
    if not row:
        raise RuntimeError("certifica_dfe: não encontrei empresa para este user + cnpj/cpf.")
    if not row.get("pem") or not row.get("key"):
        raise RuntimeError("certifica_dfe: registro sem pem/key.")
    return row

# =========================================================
# HEALTH
# =========================================================
@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "supabase_url_set": bool(SUPABASE_URL),
        "supabase_key_set": bool(SUPABASE_KEY),
        "htchat_base_url_set": bool(HTCHAT_DEFAULT_BASE_URL),
        "sicoob_oauth_set": bool(SICOOB_OAUTH_URL and SICOOB_CLIENT_ID and SICOOB_CLIENT_SECRET),
        "sicoob_emitir_set": bool(SICOOB_EMITIR_URL),
        "sicoob_pdf_set": bool(SICOOB_PDF_URL),
    })

# =========================================================
# =========================================================
# DANFSE (NFS-e Nacional) — /danfse/pdf
# HTML manda: { user, cnpj_cpf, chave }
# Aqui NÃO usamos prestador/tomador (só debug).
# =========================================================
def try_fetch_danfse_pdf(chave50: str, cert_pair: Tuple[str, str]) -> Tuple[Optional[bytes], Dict[str, Any]]:
    chave50 = only_digits(chave50)
    if len(chave50) != 50:
        return None, {"ok": False, "etapa": "validacao", "erro": "chave deve ter 50 dígitos", "chave_len": len(chave50)}

    urls = [f"https://adn.nfse.gov.br/danfse/{chave50}"]
    if DANFSE_TRY_RESTRITA:
        urls.append(f"https://adn.producaorestrita.nfse.gov.br/danfse/{chave50}")

    last = None
    for url in urls:
        t0 = time.time()
        try:
            r = requests.get(url, cert=cert_pair, timeout=DANFSE_TIMEOUT, headers={"Accept": "application/pdf,*/*"})
            ms = int((time.time() - t0) * 1000)
            ct = (r.headers.get("content-type") or "").lower()
            last = {"url": url, "status": r.status_code, "ms": ms, "ct": ct, "len": len(r.content or b"")}

            jlog("DANFSE upstream", last)

            if r.status_code == 200 and r.content:
                if ("pdf" in ct) or (r.content[:4] == b"%PDF"):
                    return r.content, {"ok": True, "http_status": 200, "env_used": url.split("/")[2], "url": url}

        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            last = {"url": url, "status": None, "ms": ms, "erro": str(e)}
            jlog("DANFSE upstream EXC", last)

    # default error
    http_status = last.get("status") if last else 404
    return None, {
        "ok": False,
        "etapa": "http",
        "erro": "Documento não encontrado (404) nos ambientes testados." if http_status == 404 else "Falha ao obter DANFSe.",
        "env_used": "tentou_producao_e_restrita" if DANFSE_TRY_RESTRITA else "tentou_producao",
        "http_status": http_status,
        "url": last.get("url") if last else None,
    }

@app.post("/danfse/pdf")
def danfse_pdf():
    try:
        body = request.get_json(silent=True) or {}
        user = norm(body.get("user"))
        cnpj_cpf = norm(body.get("cnpj_cpf") or body.get("cnpj") or body.get("doc"))
        chave = norm(body.get("chave") or body.get("chave_acesso") or body.get("accessKey"))

        chave_digits = only_digits(chave)
        chave_mask = (chave_digits[:10] + "..." + chave_digits[-4:]) if len(chave_digits) >= 14 else chave_digits

        jlog("DANFSE request", {"user": user, "cnpj_cpf": cnpj_cpf, "chave_mask": chave_mask, "chave_len": len(chave_digits)})

        row = load_dfe_cert(user, cnpj_cpf)
        jlog("DANFSE certifica_dfe row", {"id": row.get("id"), "user": row.get("user"), "empresa": row.get("empresa"), "doc": row.get("cnpj/cpf")})

        with TempCertFiles(row["pem"], row["key"]) as (pem_path, key_path):
            pdf_bytes, info = try_fetch_danfse_pdf(chave_digits, (pem_path, key_path))
            if not pdf_bytes:
                return jsonify(info), (info.get("http_status") or 404)

            filename = f"DANFSE_{chave_digits}.pdf"
            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype="application/pdf",
                as_attachment=False,
                download_name=filename,
            )

    except Exception as e:
        msg = str(e)
        jlog("DANFSE ERROR", msg)
        return jsonify({"ok": False, "erro": msg}), 500

# =========================================================
# =========================================================
# HTCHAT — /htchat/send e /htchat/send_file
# - aceita token/base_url do HTML
# - se não vier, tenta pegar do Supabase em tabela "htchat" por user
#   (ajuste o select/colunas se tua tabela for diferente)
# =========================================================
def load_htchat_creds(user: str) -> Dict[str, str]:
    user = norm(user)
    if not user:
        raise ValueError("Informe 'user'.")

    # 1) ENV default
    if HTCHAT_DEFAULT_BASE_URL and HTCHAT_DEFAULT_TOKEN:
        return {"base_url": HTCHAT_DEFAULT_BASE_URL, "token": HTCHAT_DEFAULT_TOKEN}

    # 2) Supabase table fallback (se existir)
    row = sb_find_one(
        "htchat",
        select="id,user,base_url,token,created_at",
        filters={"user": f"eq.{user}"},
        order="created_at.desc"
    )
    if row and row.get("base_url") and row.get("token"):
        return {"base_url": norm(row["base_url"]), "token": norm(row["token"])}

    raise RuntimeError("HTChat: informe base_url/token no request ou configure HTCHAT_BASE_URL/HTCHAT_TOKEN ou tabela htchat.")

def htchat_post_graphql(base_url: str, token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/graphql"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    payload = {"query": query, "variables": variables}
    t0 = time.time()
    r = requests.post(url, headers=headers, json=payload, timeout=HTCHAT_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    jlog("HTCHAT graphql", {"url": url, "status": r.status_code, "ms": ms, "len": len(r.content or b"")})
    txt = r.text or ""
    try:
        js = json.loads(txt) if txt else {}
    except Exception:
        js = {"raw": txt}
    if r.status_code >= 400:
        raise RuntimeError(f"HTChat HTTP {r.status_code}: {txt[:500]}")
    if "errors" in js and js["errors"]:
        raise RuntimeError(f"HTChat GraphQL errors: {json.dumps(js['errors'], ensure_ascii=False)[:800]}")
    return js

@app.post("/htchat/send")
def htchat_send():
    """
    Espera:
      {
        "user": "email",
        "base_url": "...",   (opcional)
        "token": "...",      (opcional)
        "phone": "5599999999999",
        "message": "texto"
      }
    """
    try:
        body = request.get_json(silent=True) or {}
        user = norm(body.get("user"))
        base_url = norm(body.get("base_url"))
        token = norm(body.get("token"))
        phone = only_digits(norm(body.get("phone") or body.get("to")))
        message = norm(body.get("message") or body.get("text"))

        if not phone:
            return jsonify({"ok": False, "erro": "Informe 'phone'."}), 400
        if not message:
            return jsonify({"ok": False, "erro": "Informe 'message'."}), 400

        if not base_url or not token:
            creds = load_htchat_creds(user)
            base_url = base_url or creds["base_url"]
            token = token or creds["token"]

        # Query (ajuste se tua operação tiver outro nome)
        query = """
        mutation SendText($phone: String!, $message: String!) {
          partner_api_send_message(phone: $phone, message: $message) {
            ok
            message
          }
        }
        """
        variables = {"phone": phone, "message": message}
        resp = htchat_post_graphql(base_url, token, query, variables)

        return jsonify({"ok": True, "resp": resp})

    except Exception as e:
        msg = str(e)
        jlog("HTCHAT SEND ERROR", msg)
        return jsonify({"ok": False, "erro": msg}), 500

@app.post("/htchat/send_file")
def htchat_send_file():
    """
    Espera:
      {
        "user": "...",
        "base_url": "...", (opcional)
        "token": "...",    (opcional)
        "phone": "...",
        "filename": "arquivo.pdf",
        "mime": "application/pdf",
        "file_base64": "....",
        "caption": "opcional"
      }
    """
    try:
        body = request.get_json(silent=True) or {}
        user = norm(body.get("user"))
        base_url = norm(body.get("base_url"))
        token = norm(body.get("token"))
        phone = only_digits(norm(body.get("phone") or body.get("to")))
        filename = norm(body.get("filename") or "arquivo.bin")
        mime = norm(body.get("mime") or "application/octet-stream")
        file_b64 = norm(body.get("file_base64"))
        caption = norm(body.get("caption"))

        if not phone:
            return jsonify({"ok": False, "erro": "Informe 'phone'."}), 400
        if not file_b64:
            return jsonify({"ok": False, "erro": "Informe 'file_base64'."}), 400

        if not base_url or not token:
            creds = load_htchat_creds(user)
            base_url = base_url or creds["base_url"]
            token = token or creds["token"]

        # Query (ajuste se tua operação tiver outro nome)
        query = """
        mutation SendFile($phone: String!, $filename: String!, $mime: String!, $file_base64: String!, $caption: String) {
          partner_api_send_file(phone: $phone, filename: $filename, mime: $mime, file_base64: $file_base64, caption: $caption) {
            ok
            message
          }
        }
        """
        variables = {
            "phone": phone,
            "filename": filename,
            "mime": mime,
            "file_base64": file_b64,
            "caption": caption if caption else None,
        }

        resp = htchat_post_graphql(base_url, token, query, variables)
        return jsonify({"ok": True, "resp": resp})

    except Exception as e:
        msg = str(e)
        jlog("HTCHAT FILE ERROR", msg)
        return jsonify({"ok": False, "erro": msg}), 500

# =========================================================
# =========================================================
# SICOOB — token + emitir + pdf
# - certificado sempre por user (certifica_sicoob)
# - se você já tinha endpoints diferentes, só troca as ENV
# =========================================================
_sicoob_token_cache = {"token": None, "exp": 0}

def sicoob_get_token() -> str:
    # cache local (processo) para evitar pedir token toda hora
    now = time.time()
    if _sicoob_token_cache["token"] and _sicoob_token_cache["exp"] > now + 15:
        return _sicoob_token_cache["token"]

    if not (SICOOB_OAUTH_URL and SICOOB_CLIENT_ID and SICOOB_CLIENT_SECRET):
        raise RuntimeError("Configure SICOOB_OAUTH_URL, SICOOB_CLIENT_ID, SICOOB_CLIENT_SECRET no Render.")

    data = {
        "grant_type": "client_credentials",
        "client_id": SICOOB_CLIENT_ID,
        "client_secret": SICOOB_CLIENT_SECRET,
    }
    if SICOOB_SCOPE:
        data["scope"] = SICOOB_SCOPE

    t0 = time.time()
    r = requests.post(SICOOB_OAUTH_URL, data=data, timeout=SICOOB_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    jlog("SICOOB token", {"status": r.status_code, "ms": ms, "len": len(r.content or b"")})

    txt = r.text or ""
    try:
        js = json.loads(txt) if txt else {}
    except Exception:
        # quando vem HTML (403 etc)
        raise RuntimeError(f"Token inválido (não é JSON): HTTP {r.status_code} - {txt[:400]}")

    if r.status_code >= 400:
        raise RuntimeError(f"Token HTTP {r.status_code}: {txt[:400]}")

    token = js.get("access_token") or js.get("token")
    exp_in = int(js.get("expires_in") or 900)
    if not token:
        raise RuntimeError(f"Token sem access_token: {txt[:300]}")

    _sicoob_token_cache["token"] = token
    _sicoob_token_cache["exp"] = now + exp_in
    return token

def sicoob_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

@app.post("/sicoob/emitir")
def sicoob_emitir():
    """
    Recebe do teu HTML:
      user, numeroContaCorrente, numeroCliente, ... pagador, etc.
    E completa:
      numeroContaCorrente / numeroCliente a partir da certifica_sicoob (por user)
    """
    try:
        body = request.get_json(silent=True) or {}
        user = norm(body.get("user"))
        cert = load_sicoob_cert(user)

        if not SICOOB_EMITIR_URL:
            return jsonify({"ok": False, "erro": "Configure SICOOB_EMITIR_URL no Render."}), 500

        # completa defaults do Supabase
        numeroContaCorrente = body.get("numeroContaCorrente")
        if numeroContaCorrente is None or str(numeroContaCorrente).strip() == "":
            numeroContaCorrente = cert.get("conta")

        numeroCliente = body.get("numeroCliente")
        if numeroCliente is None or str(numeroCliente).strip() == "":
            numeroCliente = cert.get("numerocliente")

        # normaliza
        try:
            numeroContaCorrente = int(numeroContaCorrente)
        except Exception:
            raise ValueError("certifica_sicoob.conta inválida (não numérico).")

        try:
            numeroCliente = int(str(numeroCliente).strip())
        except Exception:
            raise ValueError("certifica_sicoob.numerocliente inválido (não numérico).")

        payload = dict(body)
        payload["user"] = user
        payload["numeroContaCorrente"] = numeroContaCorrente
        payload["numeroCliente"] = numeroCliente

        token = sicoob_get_token()
        t0 = time.time()
        r = requests.post(SICOOB_EMITIR_URL, headers=sicoob_headers(token), json=payload, timeout=SICOOB_TIMEOUT)
        ms = int((time.time() - t0) * 1000)

        txt = r.text or ""
        jlog("SICOOB emitir", {"status": r.status_code, "ms": ms, "resp_head": txt[:300]})

        if r.status_code >= 400:
            return jsonify({"ok": False, "erro": f"HTTP {r.status_code}: {txt[:800]}"}), 500

        # repassa JSON como veio (HTML já sabe tratar)
        try:
            js = r.json()
        except Exception:
            js = {"raw": txt}

        return jsonify(js)

    except Exception as e:
        msg = str(e)
        jlog("SICOOB EMIT ERROR", msg)
        return jsonify({"ok": False, "erro": msg}), 500

@app.post("/sicoob/pdf")
def sicoob_pdf():
    """
    Recebe:
      user, numeroContratoCobranca, nossoNumero, numeroCliente (opcional)
    Retorna:
      PDF binário (application/pdf)
    """
    try:
        body = request.get_json(silent=True) or {}
        user = norm(body.get("user"))
        cert = load_sicoob_cert(user)

        if not SICOOB_PDF_URL:
            return jsonify({"ok": False, "erro": "Configure SICOOB_PDF_URL no Render."}), 500

        numeroCliente = body.get("numeroCliente")
        if numeroCliente is None or str(numeroCliente).strip() == "":
            numeroCliente = cert.get("numerocliente")

        try:
            numeroCliente = int(str(numeroCliente).strip())
        except Exception:
            raise ValueError("certifica_sicoob.numerocliente inválido (não numérico).")

        payload = dict(body)
        payload["user"] = user
        payload["numeroCliente"] = numeroCliente

        token = sicoob_get_token()
        t0 = time.time()
        r = requests.post(SICOOB_PDF_URL, headers=sicoob_headers(token), json=payload, timeout=SICOOB_TIMEOUT)
        ms = int((time.time() - t0) * 1000)

        ct = (r.headers.get("content-type") or "").lower()
        jlog("SICOOB pdf", {"status": r.status_code, "ms": ms, "ct": ct, "len": len(r.content or b"")})

        if r.status_code >= 400:
            return jsonify({"ok": False, "erro": f"HTTP {r.status_code}: {r.text[:800]}"}), 500

        # Se vier base64 em JSON, converte; se vier binário PDF, devolve direto
        if "application/json" in ct:
            js = r.json()
            b64 = js.get("pdfBoleto") or js.get("pdf_base64")
            if not b64:
                return jsonify({"ok": False, "erro": "JSON sem pdfBoleto/pdf_base64"}), 500
            pdf_bytes = base64.b64decode(b64)
        else:
            pdf_bytes = r.content

        if not pdf_bytes:
            return jsonify({"ok": False, "erro": "PDF vazio"}), 500

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False,
            download_name="boleto.pdf"
        )

    except Exception as e:
        msg = str(e)
        jlog("SICOOB PDF ERROR", msg)
        return jsonify({"ok": False, "erro": msg}), 500

# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
