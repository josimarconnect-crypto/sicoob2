# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import io
import json
import time
import base64
import tempfile
import mimetypes
import random
import binascii
from typing import Dict, Any, Tuple, Optional, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==========================================================
# ===================== FLASK APP ==========================
# ==========================================================

app = Flask(__name__)
CORS(app)


# ==========================================================
# ===================== CONFIG SUPABASE ====================
# ==========================================================
# Use SERVICE_ROLE no backend (recomendado). Se não tiver, cai no SUPABASE_KEY.

SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"

if not SUPABASE_KEY:
    # Você pode deixar vazio e só dar erro quando chamar rotas que precisam.
    print("⚠ ATENÇÃO: SUPABASE_SERVICE_ROLE/SUPABASE_KEY não configurada(s).")

def sb_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# ==========================================================
# ===================== SESSION + RETRY ====================
# ==========================================================

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PATCH", "DELETE"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

SESSION = make_session()


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
        return base64.b64decode(b64_str)


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

def _only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _mask_chave(ch: str) -> str:
    ch = _norm_str(ch)
    if len(ch) <= 10:
        return ch
    return ch[:10] + "..." + ch[-4:]


# ==========================================================
# ============ LIMPAR TEMPS A CADA REQUEST (FIX) ============
# ==========================================================

TEMP_FILES: List[str] = []

def _safe_unlink(path: Optional[str]):
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def cleanup_temp_files():
    global TEMP_FILES
    for p in list(TEMP_FILES):
        _safe_unlink(p)
    TEMP_FILES = []

@app.before_request
def _before_request_cleanup():
    # ✅ garante que nunca “mistura” pem/key de request anterior
    cleanup_temp_files()


# ==========================================================
# ===================== SICOOB CONFIG ======================
# ==========================================================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"
SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID_DEFAULT = os.getenv("CLIENT_ID_DEFAULT", "ca417614-7d6f-4f89-ba39-f18ea496431e").strip()
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"


def carregar_certificados_sicoob_por_user(user: str) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[int], Optional[str]]:
    """
    (SICOOB)
    Busca o ÚLTIMO certificado na certifica_sicoob DO USER.
    Retorna: (cert_tuple (cert_path,key_path), cliente_id_oauth, conta, erro)
    """
    user = _norm_str(user).lower()
    if not user:
        return None, None, None, "Campo 'user' é obrigatório para Sicoob"

    if not SUPABASE_KEY:
        return None, None, None, "SUPABASE_SERVICE_ROLE/SUPABASE_KEY não configurada"

    params = {
        "select": "pem,key,cliente_id,conta,numerocliente,user,id,created_at",
        "user": f"eq.{user}",
        "order": "id.desc",
        "limit": "1",
    }

    try:
        resp = SESSION.get(
            f"{SUPABASE_URL}/rest/v1/certifica_sicoob",
            headers=sb_headers(),
            params=params,
            timeout=25,
        )
    except Exception as e:
        return None, None, None, f"Erro ao chamar Supabase (certifica_sicoob): {e}"

    if not resp.ok:
        return None, None, None, f"Erro Supabase certifica_sicoob. Status={resp.status_code}, texto={resp.text}"

    try:
        rows: List[Dict[str, Any]] = resp.json()
    except Exception:
        return None, None, None, f"Resposta inválida do Supabase (certifica_sicoob): {resp.text}"

    if not rows:
        return None, None, None, f"Nenhum certificado encontrado para user={user} na certifica_sicoob"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")
    cliente_id = row.get("cliente_id") or None
    conta = row.get("conta")

    if not pem_b64 or not key_b64:
        return None, None, None, "Campos pem/key vazios na certifica_sicoob"

    try:
        pem_bytes = base64.b64decode(str(pem_b64).strip())
        key_bytes = base64.b64decode(str(key_b64).strip())
    except Exception as e:
        return None, None, None, f"Erro ao decodificar base64 (certifica_sicoob): {e}"

    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")
        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)

        # registra para apagar antes do próximo request
        TEMP_FILES.extend([cert_path, key_path])

    except Exception as e:
        return None, None, None, f"Erro ao criar temporários Sicoob: {e}"

    return (cert_path, key_path), cliente_id, conta, None


def gerar_token_sicoob(cert_files: Tuple[str, str], client_id_from_db: Optional[str]):
    cert_path, key_path = cert_files
    client_id = (client_id_from_db or CLIENT_ID_DEFAULT).strip()

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": SICOOB_SCOPE
    }

    try:
        resp = SESSION.post(
            SICOOB_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cert=(cert_path, key_path),
            timeout=25,
        )
    except Exception as e:
        return None, f"Erro ao chamar TOKEN: {e}"

    try:
        j = resp.json()
    except Exception:
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
        resp = SESSION.post(
            SICOOB_BOLETO_URL,
            json=dados,
            headers={"Authorization": f"Bearer {token}"},
            cert=(cert_path, key_path),
            timeout=30,
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
        resp = SESSION.get(
            SICOOB_SEGUNDA_VIA_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            cert=(cert_path, key_path),
            timeout=30,
        )
    except Exception as e:
        return None, f"Erro ao baixar PDF: {e}"

    try:
        data = resp.json()
    except Exception:
        ct = resp.headers.get("Content-Type", "")
        body = ""
        try:
            body = resp.text or ""
        except Exception:
            body = ""
        return None, f"Resposta inválida ao baixar PDF: HTTP {resp.status_code} | CT={ct or '<sem>'} | Body={(body[:1200] if body else '<vazio>')}"

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
# ===================== DANFSe / NFS-e =====================
# ==========================================================

def _sb_get_json(url: str, params: Dict[str, str], timeout: int = 25) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], int, str]:
    """GET Supabase e retorna (rows, erro, status, texto_raw)"""
    try:
        r = SESSION.get(url, headers=sb_headers(), params=params, timeout=timeout)
    except Exception as e:
        return None, f"Erro request Supabase: {e}", 0, ""
    txt = r.text or ""
    if not r.ok:
        return None, f"Erro Supabase. Status={r.status_code}, texto={txt}", r.status_code, txt
    try:
        return r.json(), None, r.status_code, txt
    except Exception:
        return None, f"Resposta Supabase não é JSON: {txt}", r.status_code, txt


def carregar_certificados_dfe_por_user_cnpj(user: str, cnpj_cpf: str, empresa: Optional[str] = None) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """
    (DANFSe)
    Busca certificado em certifica_dfe usando exatamente o que o HTML manda:
    - user (obrigatório)
    - cnpj_cpf (obrigatório - só dígitos)
    Estratégia:
      1) tenta user + cnpj_cpf em colunas possíveis: cnpj_cpf / cnpjcpf / cnpj / doc
      2) tenta user + empresa (se vier)
      3) tenta só user (último recurso)
    """
    user = _norm_str(user).lower()
    doc = _only_digits(cnpj_cpf or "")

    if not user:
        return None, "Campo 'user' é obrigatório (DANFSe)"
    if not doc:
        return None, "Campo 'cnpj_cpf' é obrigatório (DANFSe) e deve conter dígitos"

    if not SUPABASE_KEY:
        return None, "SUPABASE_SERVICE_ROLE/SUPABASE_KEY não configurada"

    base_url = f"{SUPABASE_URL}/rest/v1/certifica_dfe"

    # tentativas por possíveis nomes de coluna (porque você pode ter nomeado diferente)
    possible_doc_cols = ["cnpj_cpf", "cnpjcpf", "cnpj", "doc"]

    # 1) user + doc
    for col in possible_doc_cols:
        params = {
            "select": "id,user,empresa,pem,key,created_at",
            "user": f"eq.{user}",
            col: f"eq.{doc}",
            "order": "id.desc",
            "limit": "1",
        }
        rows, err, status, raw = _sb_get_json(base_url, params)
        if rows is not None:
            if rows:
                row = rows[0]
                break
            else:
                # coluna existe mas não achou para esse doc
                row = None
                continue
        else:
            # se deu erro 400 por coluna inexistente, só ignora e tenta outra
            if status == 400 and ("column" in (raw or "").lower() or "does not exist" in (raw or "").lower()):
                continue
            # outro erro real
            return None, f"Erro ao consultar certifica_dfe por {col}: {err}"
    else:
        row = None

    # 2) user + empresa (se vier)
    if row is None and empresa:
        params = {
            "select": "id,user,empresa,pem,key,created_at",
            "user": f"eq.{user}",
            "empresa": f"eq.{empresa}",
            "order": "id.desc",
            "limit": "1",
        }
        rows, err, _, _ = _sb_get_json(base_url, params)
        if err:
            return None, f"Erro ao consultar certifica_dfe por empresa: {err}"
        if rows:
            row = rows[0]

    # 3) só user (último recurso)
    if row is None:
        params = {
            "select": "id,user,empresa,pem,key,created_at",
            "user": f"eq.{user}",
            "order": "id.desc",
            "limit": "1",
        }
        rows, err, _, _ = _sb_get_json(base_url, params)
        if err:
            return None, f"Erro ao consultar certifica_dfe por user: {err}"
        if rows:
            row = rows[0]

    if not row:
        return None, f"Nenhum certificado encontrado na certifica_dfe para user={user} e cnpj_cpf={doc}"

    pem_b64 = row.get("pem")
    key_b64 = row.get("key")
    if not pem_b64 or not key_b64:
        return None, "Campos pem/key vazios na certifica_dfe"

    # log útil (não vaza pem)
    try:
        print("DANFSE certifica_dfe row:", {
            "id": row.get("id"),
            "user": row.get("user"),
            "empresa": row.get("empresa"),
            "doc": doc
        })
    except Exception:
        pass

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

        TEMP_FILES.extend([cert_path, key_path])

    except Exception as e:
        return None, f"Erro ao criar temporários DANFSe: {e}"

    return (cert_path, key_path), None


def build_url(base: str, path_template: str, chave: str) -> str:
    base = (base or "").strip().rstrip("/")
    path_template = (path_template or "").strip()
    if not path_template.startswith("/"):
        path_template = "/" + path_template
    return base + path_template.replace("{chave}", chave)


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
    return "API Unificada (Flask) — Sicoob + DANFSe (NFS-e) + Supabase."


# -------------------- SICOOB --------------------

@app.post("/sicoob/emitir")
def sicoob_emitir():
    """
    Seu HTML envia um payload JSON com:
      - user
      - numeroCliente, numeroContaCorrente, codigoModalidade...
      - pagador {...}
    Aqui o backend usa APENAS user para pegar pem/key/cliente_id/conta.
    """
    payload = request.get_json(silent=True) or {}
    user = _norm_str(payload.get("user")).lower()
    payload.pop("user", None)

    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    cert_files, cliente_id_oauth, conta_corrente, erro_cert = carregar_certificados_sicoob_por_user(user)
    if erro_cert:
        return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500

    # Se existir conta no DB, força no payload (seu HTML já manda também)
    if conta_corrente is not None:
        try:
            payload["numeroContaCorrente"] = int(conta_corrente)
        except Exception:
            return jsonify({"ok": False, "etapa": "certificado", "erro": f"certifica_sicoob.conta inválida: {conta_corrente}"}), 500

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
        "numeroCliente": r.get("numeroCliente"),
    })


@app.post("/sicoob/pdf")
def sicoob_pdf():
    """
    Seu HTML chama /sicoob/pdf passando:
      - user
      - numeroContratoCobranca
      - nossoNumero
      - numeroCliente
      - codigoModalidade
    Aqui o backend usa APENAS user para pegar pem/key.
    """
    dados = request.get_json(silent=True) or {}
    user = _norm_str(dados.get("user")).lower()

    if not user:
        return jsonify({"erro": "Campo 'user' é obrigatório"}), 400

    numero_cliente = dados.get("numeroCliente")
    if numero_cliente is None:
        return jsonify({"erro": "numeroCliente é obrigatório"}), 400

    cert_files, cliente_id_oauth, _, erro_cert = carregar_certificados_sicoob_por_user(user)
    if erro_cert:
        return jsonify({"erro": erro_cert}), 500

    try:
        num_cliente_int = int(str(numero_cliente))
        n_contrato = int(str(dados.get("numeroContratoCobranca")))
        n_nosso = int(str(dados.get("nossoNumero")))
        modalidade = int(str(dados.get("codigoModalidade", 1)))
    except Exception as e:
        return jsonify({"erro": f"Parâmetros numéricos inválidos: {e}"}), 400

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        return jsonify({"erro": erro_tk}), 500

    pdf_bytes, erro_pdf = baixar_pdf_boleto(token, n_contrato, n_nosso, num_cliente_int, modalidade, cert_files)
    if erro_pdf:
        print("❌ ERRO /sicoob/pdf:", erro_pdf)
        return jsonify({"erro": erro_pdf}), 500

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="boleto.pdf"
    )


# -------------------- DANFSe / NFS-e --------------------

@app.post("/danfse/pdf")
def danfse_pdf():
    """
    Seu HTML (NFS-e) envia:
      - user
      - cnpj_cpf
      - chave (50 dígitos)
      - (opcionais) prestador/tomador/servico... (podem vir vazios)
    Aqui o backend busca certificado em certifica_dfe usando user+cnpj_cpf.
    """
    body = request.get_json(silent=True) or {}

    user = _norm_str(body.get("user")).lower()
    cnpj_cpf = _only_digits(_norm_str(body.get("cnpj_cpf")))
    chave = _only_digits(_norm_str(body.get("chave")))

    # log do que chegou (sem vazar chave inteira)
    print("DANFSE request:", {
        "user": user,
        "cnpj_cpf": cnpj_cpf,
        "chave_mask": _mask_chave(chave),
        "chave_len": len(chave),
    })

    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400
    if not cnpj_cpf:
        return jsonify({"ok": False, "erro": "Campo 'cnpj_cpf' é obrigatório"}), 400
    if not chave or len(chave) != 50:
        return jsonify({"ok": False, "erro": "Campo 'chave' inválido: deve ter 50 dígitos"}), 400

    # parâmetros opcionais (se um dia você quiser controlar)
    env = _norm_str(body.get("env")).lower() or "producao"
    base_url_in = _norm_str(body.get("base_url"))
    path_pdf = _norm_str(body.get("path_pdf")) or "/danfse/{chave}"
    empresa = _norm_str(body.get("empresa")) or None

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

    cert_files, errc = carregar_certificados_dfe_por_user_cnpj(user=user, cnpj_cpf=cnpj_cpf, empresa=empresa)
    if errc:
        return jsonify({"ok": False, "etapa": "certifica_dfe", "erro": errc}), 500

    # tenta ambiente pedido e o alternativo
    if env not in ("producao", "restrita"):
        env = "producao"
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
                prev = ""
                try:
                    prev = content[:1600].decode("utf-8", errors="replace")
                except Exception:
                    prev = "<binário>"
                return jsonify({
                    "ok": False,
                    "etapa": "conteudo",
                    "erro": "Resposta não parece PDF (path_pdf errado ou serviço retornou JSON)",
                    "http_status": resp.status_code,
                    "content_type": resp.headers.get("Content-Type"),
                    "body_preview": prev,
                    "url": url,
                    "env_used": env_try,
                }), 502

            return send_file(
                io.BytesIO(content),
                mimetype="application/pdf",
                as_attachment=False,
                download_name=f"DANFSE_{chave}.pdf"
            )

        # 404 -> tenta próximo ambiente
        if resp.status_code == 404:
            continue

        # outros erros
        return jsonify({
            "ok": False,
            "etapa": "http",
            "http_status": resp.status_code,
            "content_type": resp.headers.get("Content-Type"),
            "body_preview": (resp.text or "")[:1600],
            "url": url,
            "env_used": env_try,
        }), resp.status_code

    # se chegou aqui, foi 404 nos dois ambientes
    resp = last_resp
    return jsonify({
        "ok": False,
        "etapa": "http",
        "http_status": (resp.status_code if resp is not None else 404),
        "url": last_url,
        "env_used": "tentou_producao_e_restrita",
        "erro": "Documento não encontrado (404) nos ambientes testados."
    }), 404


# ==========================================================
# ===================== MAIN ===============================
# ==========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
