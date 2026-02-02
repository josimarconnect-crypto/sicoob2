# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, send_file, g
from flask_cors import CORS
import requests
import tempfile
import os
import io
import base64
import json
import mimetypes
import time
import binascii
import re
import random
from typing import Dict, Any, Tuple, Optional, List

app = Flask(__name__)
CORS(app)

# ==========================================================
# ===================== CONFIG SUPABASE ====================
# ==========================================================

SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"

def sb_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h

def sb_insert_htchat(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/htchat",
            headers=sb_headers({"Prefer": "return=representation"}),
            data=json.dumps(row, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao inserir htchat no Supabase: {e}"

    if not r.ok:
        return None, f"Erro Supabase insert htchat. Status={r.status_code}, texto={r.text}"

    try:
        data = r.json()
        if isinstance(data, list) and data:
            return data[0], None
        return {"_raw": data}, None
    except Exception:
        return {"_raw": r.text}, None

def sb_update_htchat_status_by_idms(idms: str, status: str) -> Optional[str]:
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/htchat",
            headers=sb_headers(),
            params={"idms": f"eq.{idms}"},
            data=json.dumps({"status": status}, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return f"Erro ao atualizar status no Supabase: {e}"

    if not r.ok:
        return f"Erro Supabase update htchat. Status={r.status_code}, texto={r.text}"
    return None

# ==========================================================
# =========== LIMPEZA DE ARQUIVOS TEMP POR REQUEST =========
# ==========================================================

def _track_tmp(path: str):
    """Registra arquivo temporário para apagar ao final da request (evita mistura/conflito)."""
    if not path:
        return
    if not hasattr(g, "_tmp_files"):
        g._tmp_files = []
    g._tmp_files.append(path)

@app.teardown_request
def _cleanup_tmp_files(exc):
    """Apaga PEM/KEY temporários criados durante a request."""
    tmp = getattr(g, "_tmp_files", None)
    if not tmp:
        return
    for p in tmp:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ==========================================================
# ===================== CONFIG SICOOB ======================
# ==========================================================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"
SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID_DEFAULT = "ca417614-7d6f-4f89-ba39-f18ea496431e"
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"

def carregar_certificados_local(user: Optional[str] = None) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[int], Optional[str]]:
    """
    (SICOOB)
    Busca o último certificado na certifica_sicoob por USER e cria arquivos temporários PEM/KEY.
    Retorna: (cert_files, cliente_id_oauth, conta, erro)
    """
    if not SUPABASE_KEY:
        return None, None, None, "SUPABASE_KEY não configurada"

    params = {"select": "pem,key,cliente_id,conta", "order": "id.desc", "limit": "1"}
    if user:
        params["user"] = f"eq.{user}"

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/certifica_sicoob",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params=params,
            timeout=20,
        )
    except Exception as e:
        return None, None, None, f"Erro ao chamar Supabase: {e}"

    if not resp.ok:
        return None, None, None, f"Erro Supabase. Status={resp.status_code}, texto={resp.text}"

    try:
        rows: List[Dict[str, Any]] = resp.json()
    except ValueError:
        return None, None, None, f"Resposta inválida do Supabase: {resp.text}"

    if not rows:
        return None, None, None, "Nenhum certificado encontrado para este usuário"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")
    cliente_id = row.get("cliente_id")
    conta = row.get("conta")

    if not pem_b64 or not key_b64:
        return None, None, None, "Campos pem/key vazios"

    try:
        pem_bytes = base64.b64decode(str(pem_b64).strip())
        key_bytes = base64.b64decode(str(key_b64).strip())
    except Exception as e:
        return None, None, None, f"Erro ao decodificar base64: {e}"

    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")
        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)

        _track_tmp(cert_path)
        _track_tmp(key_path)

    except Exception as e:
        return None, None, None, f"Erro ao criar arquivos temporários: {e}"

    return (cert_path, key_path), cliente_id, conta, None

def gerar_token_sicoob(cert_files: Tuple[str, str], client_id_from_db: Optional[str]):
    cert_path, key_path = cert_files
    client_id = client_id_from_db or CLIENT_ID_DEFAULT

    data = {"grant_type": "client_credentials", "client_id": client_id, "scope": SICOOB_SCOPE}

    try:
        resp = requests.post(
            SICOOB_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cert=(cert_path, key_path),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao chamar TOKEN: {e}"

    try:
        j = resp.json()
    except ValueError:
        return None, f"Resposta TOKEN inválida (não é JSON): HTTP {resp.status_code} - {resp.text[:800]}"

    if not resp.ok:
        return None, f"Erro Token: {j}"

    token = j.get("access_token")
    if not token:
        return None, "Token não retornado"
    return token, None

def emitir_boleto_sicoob(token: str, dados: Dict[str, Any], cert_files: Tuple[str, str]):
    cert_path, key_path = cert_files
    try:
        resp = requests.post(
            SICOOB_BOLETO_URL,
            json=dados,
            headers={"Authorization": f"Bearer {token}"},
            cert=(cert_path, key_path),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao emitir boleto: {e}"

    try:
        j = resp.json()
    except Exception:
        return None, f"Resposta inválida do Sicoob (não é JSON): HTTP {resp.status_code} - {resp.text[:800]}"

    if not resp.ok:
        return None, f"Erro na emissão: {j}"

    return j, None

def baixar_pdf_boleto(token: str, n_contrato: int, n_nosso: int, n_cliente: int, modalidade: int, cert_files: Tuple[str, str]):
    cert_path, key_path = cert_files
    params = {
        "numeroCliente": n_cliente,
        "codigoModalidade": modalidade,
        "nossoNumero": n_nosso,
        "numeroContratoCobranca": n_contrato,
        "gerarPdf": "true"
    }

    try:
        resp = requests.get(
            SICOOB_SEGUNDA_VIA_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            cert=(cert_path, key_path),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao baixar PDF: {e}"

    try:
        data = resp.json()
    except ValueError:
        ct = resp.headers.get("Content-Type", "")
        txt = ""
        try:
            txt = resp.text or ""
        except Exception:
            txt = ""
        return None, f"Resposta inválida ao baixar PDF: HTTP {resp.status_code} | CT={ct or '<sem>'} | Body={(txt[:1200] if txt else '<vazio>')}"

    if not resp.ok:
        return None, data

    pdf_b64 = data.get("resultado", {}).get("pdfBoleto") or data.get("pdfBoleto")
    if not pdf_b64:
        return None, f"Campo pdfBoleto não encontrado: {data}"

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception:
        return None, "Erro ao decodificar pdfBoleto"

    return pdf_bytes, None

# ==========================================================
# ===================== HTCHAT QUERIES =====================
# ==========================================================

SEND_TEXT = """
mutation send_text($recipient: String!, $message: String!, $tipo: String!, $sender_name: String) {
  partner_api_send_message(
    recipient: $recipient,
    message: $message,
    tipo: $tipo,
    sender_name: $sender_name
  ) {
    id
    ack
    msg_id
    tipo
    message
    arquivo { eurl mime }
  }
}
""".strip()

SEND_FILE = """
mutation send_file($recipient: String!, $message: String, $tipo: String!, $sender_name: String, $file: Upload) {
  partner_api_send_message(
    recipient: $recipient,
    message: $message,
    tipo: $tipo,
    sender_name: $sender_name,
    file: $file
  ) {
    id
    ack
    msg_id
    tipo
    message
    arquivo { eurl mime }
  }
}
""".strip()

QUERY_GET_SENDED = """
query get_sended($id: Int!) {
  partner_api_get_sended(id: $id) {
    id
    ack
    msg_id
    tipo
    message
    arquivo { eurl mime }
  }
}
""".strip()

QUERY_RECIPIENT_EXISTS = """
query recipient_exists($recipient: String!, $api_id: String) {
  partner_api_recipient_exists(recipient: $recipient, api_id: $api_id) {
    recipient
  }
}
""".strip()

# ==========================================================
# ===================== UTILS BASE64 =======================
# ==========================================================

def decode_b64_to_bytes(b64_str: str) -> bytes:
    if not b64_str:
        return b""
    if "base64," in b64_str:
        b64_str = b64_str.split("base64,", 1)[1]
    b64_str = b64_str.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    missing = (-len(b64_str)) % 4
    if missing:
        b64_str += "=" * missing
    try:
        return base64.b64decode(b64_str, validate=True)
    except binascii.Error:
        try:
            return base64.b64decode(b64_str)
        except Exception as e:
            raise ValueError(f"Falha ao decodificar base64: {e}")

# ==========================================================
# ===================== HTCHAT HELPERS =====================
# ==========================================================

def safe_json(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text}

def pick_first(v):
    if isinstance(v, list):
        return v[0] if v else {}
    if isinstance(v, dict):
        return v
    return {}

def normalize_recipient(recipient: str) -> str:
    r = (recipient or "").strip()
    if not r:
        return ""
    if "@s.whatsapp.net" in r:
        return r
    if r.isdigit():
        return f"{r}@s.whatsapp.net"
    return r

def graphql_json(url, token, query, variables, verify_ssl=True, timeout=30):
    headers = {"token": token}
    return requests.post(
        url,
        headers=headers,
        json={"query": query, "variables": variables},
        verify=verify_ssl,
        timeout=timeout,
    )

def htchat_parse_send_response(resp: requests.Response) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    j = safe_json(resp)
    if resp.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat: HTTP {resp.status_code} - {j}"
    data = j.get("data") or {}
    node = pick_first(data.get("partner_api_send_message"))
    if not node:
        return None, f"Resposta HTChat inesperada: {j}"
    return node, None

def htchat_parse_get_response(resp: requests.Response) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    j = safe_json(resp)
    if resp.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat get_sended: HTTP {resp.status_code} - {j}"
    data = j.get("data") or {}
    node = pick_first(data.get("partner_api_get_sended"))
    if not node:
        return None, f"Resposta get_sended inesperada: {j}"
    return node, None

def extract_arquivo_info(node: Dict[str, Any]) -> str:
    arq = node.get("arquivo")
    if not arq:
        return ""
    if isinstance(arq, list):
        arq = arq[0] if arq else None
    if isinstance(arq, dict):
        eurl = arq.get("eurl") or ""
        mime = arq.get("mime") or ""
        if eurl and mime:
            return f"{eurl} ({mime})"
        return eurl or mime or ""
    if isinstance(arq, str):
        return arq
    return ""

def htchat_send_one(htchat_url: str, htchat_token: str, item: Dict[str, Any], verify_ssl: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    recipient = normalize_recipient(item.get("recipient", ""))
    message = item.get("message") or ""
    sender_name = item.get("sender_name") or ""

    if not recipient:
        return None, "recipient vazio"

    has_file = bool(item.get("file_b64")) or bool(item.get("file_path"))
    tipo = (item.get("tipo") or "text").strip()
    if has_file:
        tipo = "text"

    if item.get("file_b64"):
        file_name = item.get("file_name") or "arquivo.bin"
        file_mime = item.get("file_mime") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        try:
            file_bytes = decode_b64_to_bytes(item["file_b64"])
        except Exception as e:
            return None, f"file_b64 inválido: {e}"

        vars2 = {"recipient": recipient, "message": message if message else "", "tipo": tipo, "sender_name": sender_name}
        operations = json.dumps({"query": SEND_FILE, "variables": {**vars2, "file": None}}, ensure_ascii=False)
        file_map = json.dumps({"0": ["variables.file"]}, ensure_ascii=False)

        files = {
            "operations": (None, operations, "application/json"),
            "map": (None, file_map, "application/json"),
            "0": (file_name, io.BytesIO(file_bytes), file_mime),
        }

        try:
            resp = requests.post(htchat_url, headers={"token": htchat_token}, files=files, verify=verify_ssl, timeout=120)
        except Exception as e:
            return None, f"Erro upload (bytes): {e}"

        node, err = htchat_parse_send_response(resp)
        if node:
            node["_upload_mode"] = "bytes ops/map"
        return node, err

    if not str(message).strip():
        return None, "message vazio (para texto é obrigatório)"

    try:
        resp = graphql_json(htchat_url, htchat_token, SEND_TEXT, {
            "recipient": recipient, "message": message, "tipo": tipo, "sender_name": sender_name
        }, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat texto: {e}"

    node, err = htchat_parse_send_response(resp)
    if node:
        node["_upload_mode"] = "text"
    return node, err

def htchat_get_sended(htchat_url: str, htchat_token: str, msg_internal_id: int, verify_ssl: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = graphql_json(htchat_url, htchat_token, QUERY_GET_SENDED, {"id": msg_internal_id}, verify=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat get_sended: {e}"
    return htchat_parse_get_response(r)

# ==========================================================
# ============ DANFSe (OFICIAL) - CERTIFICA_DFE ============
# ==========================================================

def carregar_certificados_dfe_local(user: str, cnpj_cpf: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """
    (DFE / NFS-e / DANFSe)
    Busca o certificado na certifica_dfe usando USER + CNPJ/CPF (vem do HTML).
    Cria PEM/KEY temporários por request e apaga no teardown_request.
    """
    if not user:
        return None, "user é obrigatório"
    cnpj_cpf = (cnpj_cpf or "").strip()
    if not cnpj_cpf:
        return None, "cnpj_cpf (ou cnpj) é obrigatório para identificar o certificado da empresa"

    if not SUPABASE_KEY:
        return None, "SUPABASE_KEY não configurada"

    def _fetch(params: Dict[str, str]) -> requests.Response:
        return requests.get(
            f"{SUPABASE_URL}/rest/v1/certifica_dfe",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params=params,
            timeout=20,
        )

    # Tentativa 1: coluna cnpj_cpf
    params1 = {
        "select": "pem,key,user,cnpj_cpf,cnpj,empresa,id",
        "order": "id.desc",
        "limit": "1",
        "user": f"eq.{user}",
        "cnpj_cpf": f"eq.{cnpj_cpf}",
    }

    try:
        resp = _fetch(params1)
    except Exception as e:
        return None, f"Erro ao chamar Supabase (certifica_dfe): {e}"

    rows: List[Dict[str, Any]] = []
    if resp.ok:
        try:
            rows = resp.json()
        except Exception:
            rows = []

    # Tentativa 2: fallback para coluna cnpj (caso sua tabela use esse nome)
    if not rows:
        params2 = dict(params1)
        params2.pop("cnpj_cpf", None)
        params2["cnpj"] = f"eq.{cnpj_cpf}"
        try:
            resp2 = _fetch(params2)
        except Exception as e:
            return None, f"Erro ao chamar Supabase (certifica_dfe) fallback: {e}"

        if not resp2.ok:
            return None, f"Erro Supabase certifica_dfe. Status={resp2.status_code}, texto={resp2.text}"

        try:
            rows = resp2.json()
        except ValueError:
            return None, f"Resposta inválida do Supabase (certifica_dfe): {resp2.text}"

    if not rows:
        return None, "Nenhum certificado encontrado na certifica_dfe para este user+cnpj"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")

    try:
        print("DANFSE cert row:", {
            "id": row.get("id"),
            "user": row.get("user"),
            "cnpj_cpf": row.get("cnpj_cpf") or row.get("cnpj"),
            "empresa": row.get("empresa"),
        })
    except Exception:
        pass

    if not pem_b64 or not key_b64:
        return None, "Campos pem/key vazios na certifica_dfe"

    try:
        pem_bytes = base64.b64decode(str(pem_b64).strip())
        key_bytes = base64.b64decode(str(key_b64).strip())
    except Exception as e:
        return None, f"Erro ao decodificar base64 (certifica_dfe): {e}"

    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")
        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)

        _track_tmp(cert_path)
        _track_tmp(key_path)

    except Exception as e:
        return None, f"Erro ao criar arquivos temporários (DFE): {e}"

    return (cert_path, key_path), None

def build_url(base: str, path_template: str, chave: str) -> str:
    base = (base or "").strip().rstrip("/")
    path_template = (path_template or "").strip()
    if not path_template.startswith("/"):
        path_template = "/" + path_template
    return base + path_template.replace("{chave}", chave)

def preview_body(resp: requests.Response, limit: int = 1500) -> str:
    try:
        return (resp.text or "")[:limit]
    except Exception:
        return "<não foi possível ler texto>"

def try_problem_json(resp: requests.Response) -> Optional[Dict[str, Any]]:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/problem+json" not in ct:
        return None
    try:
        return resp.json()
    except Exception:
        return None

# ==========================================================
# ============ SESSION + RETRY (MELHORIA API) ==============
# ==========================================================

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

SESSION = make_session()

def http_get_with_retry(
    url: str,
    cert_tuple: Tuple[str, str],
    timeout: int = 60,
    tries: int = 5,
    backoff_base: float = 1.5,
):
    headers = {
        "Accept": "application/pdf",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DANFSeDownloader/1.0",
    }

    connect_timeout = 10
    read_timeout = max(10, int(timeout))

    last_exc = None
    last_resp = None

    for i in range(1, max(1, tries) + 1):
        try:
            t0 = time.time()
            resp = SESSION.get(
                url,
                cert=cert_tuple,
                timeout=(connect_timeout, read_timeout),
                allow_redirects=True,
                headers=headers,
                stream=True,
            )
            dt = round((time.time() - t0) * 1000)
            last_resp = resp

            print("DANFSE upstream:", {
                "url": url,
                "status": resp.status_code,
                "ms": dt,
                "ct": resp.headers.get("Content-Type"),
                "len": resp.headers.get("Content-Length"),
            })

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = backoff_base * i * (1 + random.random() * 0.30)
                time.sleep(wait)
                continue

            return resp
        except requests.RequestException as e:
            last_exc = e
            wait = backoff_base * i * (1 + random.random() * 0.30)
            time.sleep(wait)

    if last_resp is not None:
        return last_resp
    raise last_exc if last_exc else RuntimeError("Falha desconhecida em GET (sem resposta).")

def read_stream_bytes(resp: requests.Response, max_size_bytes: int = 15 * 1024 * 1024) -> bytes:
    chunks: List[bytes] = []
    size = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > max_size_bytes:
            raise ValueError("Resposta grande demais (limite de segurança excedido).")
        chunks.append(chunk)
    return b"".join(chunks)

# ==========================================================
# ===================== ROTAS ==============================
# ==========================================================

@app.get("/")
def home():
    return "API Unificada (Flask) — Sicoob + HTChat/WhatsApp + DANFSe (mTLS) + Supabase."

# -------------------- SICOOB --------------------

@app.post("/sicoob/emitir")
def api_emitir():
    payload = request.get_json(silent=True) or {}
    user = payload.get("user")
    payload.pop("user", None)

    cert_files, cliente_id_oauth, conta_corrente, erro_cert = carregar_certificados_local(user)
    if erro_cert:
        return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500

    if conta_corrente is not None:
        try:
            payload["numeroContaCorrente"] = int(conta_corrente)
        except ValueError:
            return jsonify({"ok": False, "etapa": "certificado", "erro": f"Valor inválido em certifica_sicoob.conta: {conta_corrente}"}), 500

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        return jsonify({"ok": False, "etapa": "token", "erro": erro_tk}), 500

    result, erro_bolet = emitir_boleto_sicoob(token, payload, cert_files)
    if erro_bolet:
        return jsonify({"ok": False, "etapa": "boleto", "erro": erro_bolet}), 500

    r = result.get("resultado", result)
    return jsonify({
        "ok": True,
        "resposta": result,
        "numeroContratoCobranca": r.get("numeroContratoCobranca"),
        "nossoNumero": r.get("nossoNumero"),
        "pdfBoleto": r.get("pdfBoleto"),
    })

@app.post("/sicoob/pdf")
def api_pdf():
    dados = request.get_json(silent=True) or {}
    user = dados.get("user")

    numero_cliente = dados.get("numeroCliente")
    if numero_cliente is None:
        return jsonify({"erro": "numeroCliente é obrigatório para baixar o PDF"}), 400

    cert_files, cliente_id_oauth, _, erro_cert = carregar_certificados_local(user)
    if erro_cert:
        return jsonify({"erro": erro_cert}), 500

    try:
        num_cliente_int = int(str(numero_cliente))
        n_contrato = int(str(dados.get("numeroContratoCobranca")))
        n_nosso = int(str(dados.get("nossoNumero")))
        modalidade = int(str(dados.get("codigoModalidade")))
    except Exception as e:
        return jsonify({"erro": f"Parâmetros numéricos inválidos: {e}"}), 400

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        return jsonify({"erro": erro_tk}), 500

    pdf_bytes, erro_pdf = baixar_pdf_boleto(token, n_contrato, n_nosso, num_cliente_int, modalidade, cert_files)
    if erro_pdf:
        print("❌ ERRO /sicoob/pdf:", erro_pdf)
        return jsonify({"erro": erro_pdf}), 500

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name="boleto.pdf")

# -------------------- HTCHAT / WHATSAPP --------------------

@app.post("/htchat/send")
def htchat_send_batch():
    body = request.get_json(silent=True) or {}

    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    if not htchat_url:
        return jsonify({"ok": False, "erro": "Campo 'htchat_url' é obrigatório"}), 400
    if not htchat_token:
        return jsonify({"ok": False, "erro": "Campo 'htchat_token' é obrigatório"}), 400

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "erro": "Campo 'messages' deve ser uma lista com pelo menos 1 item"}), 400

    delay_seconds = body.get("delay_seconds", 15)
    try:
        delay_seconds = int(delay_seconds)
        if delay_seconds < 0:
            delay_seconds = 0
    except Exception:
        delay_seconds = 15

    verify_ssl = bool(body.get("verify_ssl", True))

    results = []
    for idx, item in enumerate(messages, start=1):
        recipient_norm = normalize_recipient(item.get("recipient", ""))
        anexo_desc = item.get("file_name") or ""

        node, err = htchat_send_one(htchat_url, htchat_token, item, verify_ssl=verify_ssl)

        if err:
            row_err = {
                "numero": recipient_norm,
                "mensagem": item.get("message") or "",
                "anexo": anexo_desc,
                "idms": "",
                "status": f"erro: {err}",
                "user": user,
            }
            _, sb_err = sb_insert_htchat(row_err)
            if sb_err:
                print("⚠ Falha ao inserir erro htchat:", sb_err)

            results.append({"i": idx, "recipient": recipient_norm, "ok": False, "erro": err})
        else:
            msg_internal_id = node.get("id")
            ack = node.get("ack")

            row_ok = {
                "numero": recipient_norm,
                "mensagem": item.get("message") or "",
                "anexo": anexo_desc,
                "idms": str(msg_internal_id) if msg_internal_id is not None else "",
                "status": f"{ack}" if ack is not None else "sent",
                "user": user,
            }
            _, sb_err = sb_insert_htchat(row_ok)
            if sb_err:
                print("⚠ Falha ao inserir htchat:", sb_err)

            results.append({
                "i": idx,
                "recipient": recipient_norm,
                "ok": True,
                "id": msg_internal_id,
                "ack": ack,
                "msg_id": node.get("msg_id"),
                "tipo": node.get("tipo"),
                "arquivo": extract_arquivo_info(node),
                "upload_mode": node.get("_upload_mode"),
            })

        if idx < len(messages) and delay_seconds > 0:
            time.sleep(delay_seconds)

    return jsonify({"ok": True, "delay_seconds": delay_seconds, "results": results})

@app.post("/htchat/status")
def htchat_update_status():
    body = request.get_json(silent=True) or {}

    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    if not htchat_url:
        return jsonify({"ok": False, "erro": "Campo 'htchat_url' é obrigatório"}), 400
    if not htchat_token:
        return jsonify({"ok": False, "erro": "Campo 'htchat_token' é obrigatório"}), 400

    raw_id = body.get("id")
    if raw_id is None:
        return jsonify({"ok": False, "erro": "Campo 'id' é obrigatório"}), 400

    try:
        msg_internal_id = int(str(raw_id))
    except Exception:
        return jsonify({"ok": False, "erro": f"id inválido: {raw_id}"}), 400

    verify_ssl = bool(body.get("verify_ssl", True))

    node, err = htchat_get_sended(htchat_url, htchat_token, msg_internal_id, verify_ssl=verify_ssl)
    if err:
        return jsonify({"ok": False, "erro": err}), 500

    ack = node.get("ack")
    status_str = f"{ack}" if ack is not None else "null"

    up_err = sb_update_htchat_status_by_idms(str(msg_internal_id), status_str)
    if up_err:
        print("⚠ Falha ao atualizar status no Supabase:", up_err)

    return jsonify({"ok": True, "id": msg_internal_id, "ack": ack, "updated_status": status_str, "node": node})

@app.post("/htchat/recipient_exists")
def htchat_recipient_exists():
    body = request.get_json(silent=True) or {}
    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    recipient = normalize_recipient(body.get("recipient", ""))
    api_id = body.get("api_id")

    if not htchat_url or not htchat_token or not recipient:
        return jsonify({"ok": False, "erro": "htchat_url, htchat_token e recipient são obrigatórios"}), 400

    r = graphql_json(
        htchat_url, htchat_token, QUERY_RECIPIENT_EXISTS,
        {"recipient": recipient, "api_id": api_id},
        verify=bool(body.get("verify_ssl", True))
    )
    return jsonify({"ok": True, "resp": safe_json(r)})

@app.post("/htchat/get_sended")
def htchat_get_sended_route():
    body = request.get_json(silent=True) or {}
    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    raw_id = body.get("id")

    if not htchat_url or not htchat_token or raw_id is None:
        return jsonify({"ok": False, "erro": "htchat_url, htchat_token e id são obrigatórios"}), 400

    try:
        mid = int(str(raw_id))
    except Exception:
        return jsonify({"ok": False, "erro": "id inválido"}), 400

    node, err = htchat_get_sended(htchat_url, htchat_token, mid, verify_ssl=bool(body.get("verify_ssl", True)))
    if err:
        return jsonify({"ok": False, "erro": err}), 500
    return jsonify({"ok": True, "node": node})

# ==========================================================
# ===================== DANFSe (TRATATIVA) =================
# ==========================================================

def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

def _norm_env(v: Any) -> str:
    e = _norm_str(v).lower()
    if e in ("restrita", "producao", "produção"):
        return "restrita" if e == "restrita" else "producao"
    return "producao"

def _safe_payload_for_log(body: Dict[str, Any]) -> Dict[str, Any]:
    chave = _norm_str(body.get("chave"))
    cnpj = _norm_str(body.get("cnpj_cpf") or body.get("cnpj"))
    return {
        "user": _norm_str(body.get("user")),
        "cnpj_cpf": cnpj,
        "env": _norm_str(body.get("env")),
        "base_url": _norm_str(body.get("base_url")),
        "path_pdf": _norm_str(body.get("path_pdf")),
        "chave_len": len(chave),
        "chave_prefix": chave[:10],
    }

@app.post("/danfse/pdf")
def danfse_pdf():
    body = request.get_json(silent=True) or {}
    print("DANFSE request body:", _safe_payload_for_log(body))

    user = _norm_str(body.get("user"))
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    chave = _norm_str(body.get("chave"))
    if not chave or len(chave) < 20:
        return jsonify({"ok": False, "erro": "Campo 'chave' inválido/curto"}), 400

    cnpj_cpf = _norm_str(body.get("cnpj_cpf") or body.get("cnpj"))
    if not cnpj_cpf:
        return jsonify({"ok": False, "erro": "Envie 'cnpj_cpf' (ou 'cnpj') para selecionar o certificado correto"}), 400

    env = _norm_env(body.get("env"))
    base_url_in = _norm_str(body.get("base_url"))
    path_pdf = _norm_str(body.get("path_pdf")) or "/danfse/{chave}"

    def _base_for(env_local: str) -> str:
        if base_url_in:
            return base_url_in
        return "https://adn.producaorestrita.nfse.gov.br" if env_local == "restrita" else "https://adn.nfse.gov.br"

    try:
        timeout = int(body.get("timeout", 120))
    except Exception:
        timeout = 120
    try:
        tries = int(body.get("tries", 5))
    except Exception:
        tries = 5
    try:
        backoff = float(body.get("backoff", 1.5))
    except Exception:
        backoff = 1.5

    cert_files, errc = carregar_certificados_dfe_local(user=user, cnpj_cpf=cnpj_cpf)
    if errc:
        return jsonify({"ok": False, "etapa": "certifica_dfe", "erro": errc}), 500

    env_try_order = [env, ("restrita" if env == "producao" else "producao")]

    last_resp = None
    last_url = None

    for env_try in env_try_order:
        base_url = _base_for(env_try)
        url = build_url(base_url, path_pdf, chave)
        last_url = url

        try:
            resp = http_get_with_retry(
                url=url,
                cert_tuple=cert_files,
                timeout=max(10, timeout),
                tries=max(1, tries),
                backoff_base=max(0.1, backoff),
            )
        except Exception as e:
            return jsonify({"ok": False, "etapa": "request", "erro": f"Falha ao chamar serviço DANFSe: {e}", "url": url}), 502

        last_resp = resp

        if resp.status_code < 400:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            try:
                content = read_stream_bytes(resp, max_size_bytes=15 * 1024 * 1024)
            except Exception as e:
                return jsonify({
                    "ok": False,
                    "etapa": "download",
                    "erro": f"Falha ao ler PDF via stream: {e}",
                    "http_status": resp.status_code,
                    "content_type": resp.headers.get("Content-Type"),
                    "url": url,
                    "env_used": env_try,
                }), 502

            is_pdf = ("pdf" in ctype) or (content[:4] == b"%PDF")
            if not is_pdf:
                body_prev = ""
                try:
                    body_prev = content[:1600].decode("utf-8", errors="replace")
                except Exception:
                    body_prev = "<binário>"
                return jsonify({
                    "ok": False,
                    "etapa": "conteudo",
                    "erro": "Resposta não parece PDF (path_pdf errado ou serviço retornou JSON/HTML)",
                    "http_status": resp.status_code,
                    "content_type": resp.headers.get("Content-Type"),
                    "body_preview": body_prev,
                    "url": url,
                    "env_used": env_try,
                }), 502

            return send_file(
                io.BytesIO(content),
                mimetype="application/pdf",
                as_attachment=False,
                download_name=f"DANFSE_{chave}.pdf"
            )

        if resp.status_code == 404:
            continue

        prob = try_problem_json(resp)
        return jsonify({
            "ok": False,
            "etapa": "http",
            "http_status": resp.status_code,
            "content_type": resp.headers.get("Content-Type"),
            "problem_json": prob,
            "body_preview": preview_body(resp, 1600),
            "url": url,
            "env_used": env_try,
        }), resp.status_code

    resp = last_resp
    url = last_url
    prob = try_problem_json(resp) if resp is not None else None
    return jsonify({
        "ok": False,
        "etapa": "http",
        "http_status": (resp.status_code if resp is not None else 404),
        "content_type": (resp.headers.get("Content-Type") if resp is not None else None),
        "problem_json": prob,
        "body_preview": (preview_body(resp, 1600) if resp is not None else ""),
        "url": url,
        "env_used": "tentou_producao_e_restrita",
        "erro": "Documento não encontrado (404) nos ambientes testados. Verifique se o HTML está mandando a chave correta e o cnpj_cpf correto."
    }), 404

# ==========================================================
# ===================== MAIN ===============================
# ==========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
