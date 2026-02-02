# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify, send_file
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
from typing import Dict, Any, Tuple, Optional, List
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

app = Flask(__name__)
CORS(app)

# ==========================================================
# ===================== CONFIG SUPABASE ====================
# ==========================================================

SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"

def sb_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    if extra:
        h.update(extra)
    return h

# ==========================================================
# ===================== LOG HELPERS ========================
# ==========================================================

def _ct(resp: requests.Response) -> str:
    return (resp.headers.get("Content-Type") or "").lower()

def _preview_text(resp: requests.Response, n=800) -> str:
    try:
        return (resp.text or "")[:n]
    except Exception:
        return "<sem-texto>"

def _log_step(step: str, **k):
    # log compacto no Render
    print(f"[API] {step} :: " + " | ".join([f"{a}={k[a]}" for a in k]))

# ==========================================================
# ============= GERENCIADOR DE CERTIFICADOS ================
# ==========================================================

class CertificateManager:
    """
    Gerencia certificados temporários com isolamento por contexto.
    Evita mistura entre certificados Sicoob e DANFSE.
    """
    def __init__(self):
        self.temp_files = []
    
    def create_temp_cert(self, pem_bytes: bytes, key_bytes: bytes, context: str = "default") -> Tuple[str, str]:
        """
        Cria certificados temporários e registra para limpeza.
        
        Args:
            pem_bytes: Conteúdo do certificado PEM
            key_bytes: Conteúdo da chave privada
            context: Contexto do certificado (sicoob, danfse, etc)
        
        Returns:
            Tupla (cert_path, key_path)
        """
        try:
            cert_fd, cert_path = tempfile.mkstemp(suffix=f"_{context}.pem", prefix="cert_")
            key_fd, key_path = tempfile.mkstemp(suffix=f"_{context}.key", prefix="key_")
            
            with os.fdopen(cert_fd, "wb") as f:
                f.write(pem_bytes)
            with os.fdopen(key_fd, "wb") as f:
                f.write(key_bytes)
            
            self.temp_files.append(cert_path)
            self.temp_files.append(key_path)
            
            _log_step("cert:created", context=context, cert=cert_path[-30:], key=key_path[-30:])
            return cert_path, key_path
        except Exception as e:
            raise Exception(f"Erro ao criar certificado temporário [{context}]: {e}")
    
    def cleanup(self):
        """Remove todos os certificados temporários criados."""
        removed = 0
        for filepath in self.temp_files:
            try:
                if os.path.exists(filepath):
                    os.unlink(filepath)
                    removed += 1
            except Exception as e:
                _log_step("cert:cleanup_error", file=filepath, error=str(e))
        
        if removed > 0:
            _log_step("cert:cleaned", count=removed)
        self.temp_files.clear()

# ==========================================================
# ===================== HTCHAT (SUPABASE) ==================
# ==========================================================

def sb_insert_htchat(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/htchat",
            headers={**sb_headers({"Content-Type": "application/json"}), "Prefer": "return=representation"},
            data=json.dumps(row, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao inserir htchat: {e}"

    if not r.ok:
        return None, f"Erro insert htchat. Status={r.status_code}, texto={r.text}"

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
            headers={**sb_headers({"Content-Type": "application/json"})},
            params={"idms": f"eq.{idms}"},
            data=json.dumps({"status": status}, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return f"Erro ao atualizar status htchat: {e}"

    if not r.ok:
        return f"Erro update htchat. Status={r.status_code}, texto={r.text}"
    return None

# ==========================================================
# ===================== SICOOB =============================
# ==========================================================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"
SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID_DEFAULT = "ca417614-7d6f-4f89-ba39-f18ea496431e"
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"

# cache por user (Sicoob)
CERT_CACHE: Dict[str, Dict[str, Any]] = {}

def carregar_certificados_sicoob(user: Optional[str]) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[int], Optional[str]]:
    """
    Busca último pem/key na certifica_sicoob (por user) e cria arquivos temporários.
    Retorna: (cert_files, cliente_id_oauth, conta, erro)
    """
    cache_key = (user or "").strip().lower() or "_default"

    if cache_key in CERT_CACHE:
        info = CERT_CACHE[cache_key]
        return info["cert"], info.get("cliente_id"), info.get("conta"), None

    params = {"select": "pem,key,cliente_id,conta", "order": "id.desc", "limit": "1"}
    if user:
        params["user"] = f"eq.{user}"

    _log_step("certifica_sicoob:query", user=user, cache_key=cache_key)

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/certifica_sicoob",
            headers=sb_headers(),
            params=params,
            timeout=20,
        )
    except Exception as e:
        return None, None, None, f"Erro Supabase certifica_sicoob: {e}"

    if not resp.ok:
        return None, None, None, f"Erro Supabase certifica_sicoob. Status={resp.status_code} - {resp.text}"

    try:
        rows = resp.json()
    except Exception:
        return None, None, None, f"Supabase retornou não-JSON: {resp.text}"

    if not rows:
        return None, None, None, "Nenhum certificado encontrado (certifica_sicoob)"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")
    cliente_id = row.get("cliente_id")
    conta = row.get("conta")

    if not pem_b64 or not key_b64:
        return None, None, None, "Campos pem/key vazios (certifica_sicoob)"

    try:
        pem_bytes = base64.b64decode(pem_b64)
        key_bytes = base64.b64decode(key_b64)
    except Exception as e:
        return None, None, None, f"Erro base64 pem/key: {e}"

    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix="_sicoob.pem")
        key_fd, key_path = tempfile.mkstemp(suffix="_sicoob.key")
        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)
    except Exception as e:
        return None, None, None, f"Erro ao criar pem/key temp: {e}"

    CERT_CACHE[cache_key] = {"cert": (cert_path, key_path), "cliente_id": cliente_id, "conta": conta}
    _log_step("certifica_sicoob:ok", cliente_id=cliente_id, conta=conta, cache_key=cache_key)
    return CERT_CACHE[cache_key]["cert"], cliente_id, conta, None

def gerar_token_sicoob(cert_files: Tuple[str, str], client_id_from_db: Optional[str]):
    """
    Gera token OAuth do Sicoob com headers mais completos para evitar bloqueio WAF
    """
    cert_path, key_path = cert_files
    client_id = (client_id_from_db or CLIENT_ID_DEFAULT or "").strip()

    # Validação básica do client_id
    if not client_id:
        return None, "client_id vazio ou inválido"

    # Verificar se os arquivos de certificado existem
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        return None, f"Arquivos de certificado não encontrados: cert={os.path.exists(cert_path)}, key={os.path.exists(key_path)}"

    # Dados do formulário (application/x-www-form-urlencoded)
    form_data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": SICOOB_SCOPE
    }

    # Headers completos para evitar bloqueio do WAF
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache"
    }

    _log_step("sicoob:token:request", 
             client_id=client_id[:20] + "...", 
             scope=SICOOB_SCOPE,
             cert_exists=os.path.exists(cert_path),
             key_exists=os.path.exists(key_path))

    try:
        # Criar sessão com retry
        session = requests.Session()
        
        # Configurar retry strategy
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        
        # Fazer a requisição
        resp = session.post(
            SICOOB_TOKEN_URL,
            data=urlencode(form_data),  # Usar urlencode explícito
            headers=headers,
            cert=(cert_path, key_path),
            timeout=30,  # Aumentar timeout
            verify=True  # Verificar SSL
        )
        
    except requests.exceptions.SSLError as e:
        return None, f"Erro SSL ao chamar TOKEN (certificado pode estar inválido): {e}"
    except requests.exceptions.Timeout as e:
        return None, f"Timeout ao chamar TOKEN (30s): {e}"
    except requests.exceptions.ConnectionError as e:
        return None, f"Erro de conexão ao chamar TOKEN: {e}"
    except Exception as e:
        return None, f"Erro inesperado ao chamar TOKEN: {e}"

    ctype = _ct(resp)
    _log_step("sicoob:token:response", 
             status=resp.status_code, 
             ctype=ctype,
             content_length=len(resp.content))

    # Verificar se a resposta é HTML (bloqueio WAF)
    if "text/html" in ctype:
        preview = _preview_text(resp, 1000)
        
        # Verificar se é bloqueio de WAF
        if "Request Rejected" in preview or "support ID" in preview.lower():
            return None, (
                f"BLOQUEIO WAF DETECTADO - HTTP {resp.status_code}\n"
                f"Possíveis causas:\n"
                f"1. Certificado digital inválido ou expirado\n"
                f"2. Client ID incorreto: {client_id[:30]}...\n"
                f"3. IP do servidor não está na whitelist do Sicoob\n"
                f"4. Headers da requisição considerados suspeitos\n"
                f"Resposta: {preview}"
            )
        
        return None, f"Resposta TOKEN inválida (HTML): HTTP {resp.status_code} | Body={preview}"

    # Tentar parsear JSON
    try:
        j = resp.json()
    except Exception:
        return None, f"Resposta TOKEN inválida (não é JSON): HTTP {resp.status_code} | CT={ctype} | Body={_preview_text(resp, 800)}"

    # Verificar se foi bem sucedido
    if not resp.ok:
        error_desc = j.get("error_description") or j.get("error") or str(j)
        return None, f"Erro ao obter Token (HTTP {resp.status_code}): {error_desc}"

    # Extrair token
    token = j.get("access_token")
    if not token:
        return None, f"Token não retornado na resposta. Campos disponíveis: {list(j.keys())}"
    
    _log_step("sicoob:token:success", token_length=len(token), expires_in=j.get("expires_in"))
    return token, None

def baixar_pdf_boleto(token: str, n_contrato: int, n_nosso: int, n_cliente: int, modalidade: int, cert_files: Tuple[str, str]):
    cert_path, key_path = cert_files
    params = {
        "numeroCliente": n_cliente,
        "codigoModalidade": modalidade,
        "nossoNumero": n_nosso,
        "numeroContratoCobranca": n_contrato,
        "gerarPdf": "true"
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9"
    }

    _log_step("sicoob:segunda_via:request", 
             numeroCliente=n_cliente, 
             contrato=n_contrato, 
             nossoNumero=n_nosso, 
             modalidade=modalidade)

    try:
        resp = requests.get(
            SICOOB_SEGUNDA_VIA_URL,
            headers=headers,
            params=params,
            cert=(cert_path, key_path),
            timeout=30,
        )
    except Exception as e:
        return None, f"Erro ao baixar PDF: {e}"

    ctype = _ct(resp)
    _log_step("sicoob:segunda_via:response", status=resp.status_code, ctype=ctype)

    try:
        data = resp.json()
    except Exception:
        # aqui também pode vir HTML
        return None, f"Resposta inválida segunda-via (não JSON): HTTP {resp.status_code} | CT={ctype} | Body={_preview_text(resp, 1000)}"

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
# ===================== DANFSE =============================
# ==========================================================

def carregar_certificado_danfse(user: str) -> Tuple[Optional[Tuple[str, str]], Optional[str]]:
    """
    Busca certificado DANFSE no Supabase (tabela certifica_danfse).
    Retorna: (cert_files, erro)
    """
    params = {
        "select": "pem,key",
        "user": f"eq.{user}",
        "order": "id.desc",
        "limit": "1"
    }

    _log_step("danfse:cert:query", user=user)

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/certifica_danfse",
            headers=sb_headers(),
            params=params,
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao buscar certificado DANFSE: {e}"

    if not resp.ok:
        return None, f"Erro ao buscar certificado DANFSE. Status={resp.status_code} - {resp.text}"

    try:
        rows = resp.json()
    except Exception:
        return None, f"Resposta inválida do Supabase: {resp.text}"

    if not rows:
        return None, "Nenhum certificado DANFSE encontrado para este usuário"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")

    if not pem_b64 or not key_b64:
        return None, "Campos pem/key vazios (certifica_danfse)"

    try:
        pem_bytes = base64.b64decode(pem_b64)
        key_bytes = base64.b64decode(key_b64)
    except Exception as e:
        return None, f"Erro ao decodificar base64: {e}"

    # Criar gerenciador de certificados isolado
    cert_mgr = CertificateManager()
    try:
        cert_path, key_path = cert_mgr.create_temp_cert(pem_bytes, key_bytes, context="danfse")
        _log_step("danfse:cert:ok", user=user)
        return (cert_path, key_path), None
    except Exception as e:
        cert_mgr.cleanup()
        return None, str(e)

def gerar_danfse_pdf(xml_content: str, cert_files: Tuple[str, str]) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Gera DANFSE PDF a partir do XML usando o serviço externo.
    
    Args:
        xml_content: Conteúdo do XML da NFS-e
        cert_files: Tupla (cert_path, key_path) do certificado DANFSE
    
    Returns:
        Tupla (pdf_bytes, erro)
    """
    cert_path, key_path = cert_files
    
    # URL do serviço de geração de DANFSE (ajuste conforme necessário)
    # Este é um exemplo - você deve usar a URL real do seu serviço
    danfse_url = "https://seu-servico-danfse.com/api/gerar-pdf"
    
    _log_step("danfse:pdf:request", xml_size=len(xml_content))
    
    try:
        # Fazer requisição com certificado
        resp = requests.post(
            danfse_url,
            data={"xml": xml_content},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/pdf",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            cert=(cert_path, key_path),
            timeout=30,
            verify=True
        )
    except Exception as e:
        return None, f"Erro ao chamar serviço DANFSE: {e}"
    
    _log_step("danfse:pdf:response", status=resp.status_code, ctype=_ct(resp))
    
    if not resp.ok:
        try:
            error_data = resp.json()
            return None, f"Erro no serviço DANFSE: {error_data}"
        except:
            return None, f"Erro no serviço DANFSE (HTTP {resp.status_code}): {_preview_text(resp, 500)}"
    
    # Verificar se retornou PDF
    if "application/pdf" not in _ct(resp):
        return None, f"Resposta não é PDF: {_ct(resp)}"
    
    pdf_bytes = resp.content
    _log_step("danfse:pdf:success", size=len(pdf_bytes))
    
    return pdf_bytes, None

# ==========================================================
# ===================== HTCHAT (GraphQL) ===================
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

def decode_b64_to_bytes(b64_str: str) -> bytes:
    """Decodifica string base64 para bytes"""
    try:
        return base64.b64decode(b64_str)
    except Exception as e:
        raise ValueError(f"Base64 inválido: {e}")

def graphql_json(url, token, query, variables, verify_ssl=True, timeout=30):
    return requests.post(
        url,
        headers={"token": token},
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

    has_file = bool(item.get("file_b64"))
    tipo = (item.get("tipo") or "text").strip()

    # compat com arquivo
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
            return None, f"Erro upload: {e}"

        node, err = htchat_parse_send_response(resp)
        if node:
            node["_upload_mode"] = "file"
        return node, err

    if not str(message).strip():
        return None, "message vazio"

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
        r = graphql_json(htchat_url, htchat_token, QUERY_GET_SENDED, {"id": msg_internal_id}, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat get_sended: {e}"
    return htchat_parse_get_response(r)

# ==========================================================
# ===================== ROTAS ==============================
# ==========================================================

@app.get("/")
def home():
    return jsonify({
        "app": "API Sicoob + HTChat + DANFSE + Supabase",
        "version": "3.0",
        "endpoints": {
            "sicoob": ["/sicoob/pdf", "/sicoob/emitir"],
            "danfse": ["/danfse/pdf"],
            "htchat": ["/htchat/send", "/htchat/status", "/htchat/recipient_exists", "/htchat/get_sended"]
        }
    })

# =================== ROTAS SICOOB ===================

@app.post("/sicoob/pdf")
def sicoob_pdf():
    """Gera PDF de boleto Sicoob"""
    dados = request.get_json(silent=True) or {}
    user = dados.get("user")

    numero_cliente = dados.get("numeroCliente")
    if numero_cliente is None:
        return jsonify({"erro": "numeroCliente é obrigatório"}), 400

    cert_files, cliente_id_oauth, _, erro_cert = carregar_certificados_sicoob(user)
    if erro_cert:
        _log_step("sicoob:pdf:erro_cert", erro=erro_cert)
        return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500

    try:
        num_cliente_int = int(str(numero_cliente))
        n_contrato = int(str(dados.get("numeroContratoCobranca")))
        n_nosso = int(str(dados.get("nossoNumero")))
        modalidade = int(str(dados.get("codigoModalidade")))
    except Exception as e:
        return jsonify({"erro": f"Parâmetros numéricos inválidos: {e}"}), 400

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        _log_step("sicoob:pdf:erro_token", erro=erro_tk)
        return jsonify({"ok": False, "etapa": "token", "erro": erro_tk}), 500

    pdf_bytes, erro_pdf = baixar_pdf_boleto(token, n_contrato, n_nosso, num_cliente_int, modalidade, cert_files)
    if erro_pdf:
        _log_step("sicoob:pdf:erro_segunda_via", erro=str(erro_pdf)[:400])
        return jsonify({"ok": False, "etapa": "segunda_via", "erro": erro_pdf}), 500

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name="boleto.pdf")


@app.post("/sicoob/emitir")
def sicoob_emitir():
    """Emite boleto Sicoob"""
    payload = request.get_json(silent=True) or {}
    user = payload.get("user")
    payload.pop("user", None)

    cert_files, cliente_id_oauth, conta_corrente, erro_cert = carregar_certificados_sicoob(user)
    if erro_cert:
        return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500

    if conta_corrente is not None:
        try:
            payload["numeroContaCorrente"] = int(conta_corrente)
        except ValueError:
            return jsonify({"ok": False, "etapa": "certificado", "erro": f"conta inválida: {conta_corrente}"}), 500

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        return jsonify({"ok": False, "etapa": "token", "erro": erro_tk}), 500

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.post(
            SICOOB_BOLETO_URL,
            json=payload,
            headers=headers,
            cert=cert_files,
            timeout=30,
        )
    except Exception as e:
        return jsonify({"ok": False, "etapa": "boleto", "erro": f"Erro request: {e}"}), 500

    try:
        j = resp.json()
    except Exception:
        return jsonify({"ok": False, "etapa": "boleto", "erro": f"Não JSON: HTTP {resp.status_code} | CT={_ct(resp)} | Body={_preview_text(resp, 1000)}"}), 500

    if not resp.ok:
        return jsonify({"ok": False, "etapa": "boleto", "erro": j}), 500

    r = j.get("resultado", j)
    return jsonify({"ok": True, "resposta": j, "numeroContratoCobranca": r.get("numeroContratoCobranca"), "nossoNumero": r.get("nossoNumero")})


# =================== ROTAS DANFSE ===================

@app.post("/danfse/pdf")
def danfse_pdf():
    """
    Gera PDF do DANFSE a partir do XML.
    Usa certificado DANFSE isolado (não mistura com Sicoob).
    """
    dados = request.get_json(silent=True) or {}
    user = dados.get("user")
    xml_content = dados.get("xml")
    
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400
    
    if not xml_content:
        return jsonify({"ok": False, "erro": "Campo 'xml' é obrigatório"}), 400
    
    # Criar gerenciador de certificados isolado para DANFSE
    cert_mgr = CertificateManager()
    
    try:
        # Carregar certificado DANFSE
        cert_files, erro_cert = carregar_certificado_danfse(user)
        if erro_cert:
            _log_step("danfse:pdf:erro_cert", erro=erro_cert)
            return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500
        
        # Gerar PDF
        pdf_bytes, erro_pdf = gerar_danfse_pdf(xml_content, cert_files)
        
        if erro_pdf:
            _log_step("danfse:pdf:erro", erro=erro_pdf)
            return jsonify({"ok": False, "etapa": "geracao_pdf", "erro": erro_pdf}), 500
        
        # Retornar PDF
        return send_file(
            io.BytesIO(pdf_bytes), 
            mimetype="application/pdf", 
            as_attachment=False, 
            download_name="danfse.pdf"
        )
    
    finally:
        # IMPORTANTE: Limpar certificados temporários DANFSE
        cert_mgr.cleanup()
        _log_step("danfse:pdf:cleanup", msg="Certificados DANFSE removidos")


# =================== ROTAS HTCHAT ===================

@app.post("/htchat/send")
def htchat_send_batch():
    body = request.get_json(silent=True) or {}
    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    if not htchat_url or not htchat_token:
        return jsonify({"ok": False, "erro": "htchat_url e htchat_token são obrigatórios"}), 400

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "erro": "messages deve ser lista com pelo menos 1 item"}), 400

    delay_seconds = body.get("delay_seconds", 15)
    try:
        delay_seconds = max(0, int(delay_seconds))
    except Exception:
        delay_seconds = 15

    verify_ssl = bool(body.get("verify_ssl", True))

    results = []
    for idx, item in enumerate(messages, start=1):
        recipient_norm = normalize_recipient(item.get("recipient", ""))
        anexo_desc = item.get("file_name") or ""

        node, err = htchat_send_one(htchat_url, htchat_token, item, verify_ssl=verify_ssl)

        if err:
            sb_insert_htchat({"numero": recipient_norm, "mensagem": item.get("message") or "", "anexo": anexo_desc,
                             "idms": "", "status": f"erro: {err}", "user": user})
            results.append({"i": idx, "recipient": recipient_norm, "ok": False, "erro": err})
        else:
            mid = node.get("id")
            ack = node.get("ack")
            sb_insert_htchat({"numero": recipient_norm, "mensagem": item.get("message") or "", "anexo": anexo_desc,
                             "idms": str(mid) if mid is not None else "", "status": f"{ack}" if ack is not None else "sent", "user": user})
            results.append({"i": idx, "recipient": recipient_norm, "ok": True, "id": mid, "ack": ack,
                            "msg_id": node.get("msg_id"), "tipo": node.get("tipo"),
                            "arquivo": extract_arquivo_info(node), "upload_mode": node.get("_upload_mode")})

        if idx < len(messages) and delay_seconds > 0:
            time.sleep(delay_seconds)

    return jsonify({"ok": True, "delay_seconds": delay_seconds, "results": results})


@app.post("/htchat/status")
def htchat_status():
    body = request.get_json(silent=True) or {}
    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "user é obrigatório"}), 400

    htchat_url = (body.get("htchat_url") or "").strip()
    htchat_token = (body.get("htchat_token") or "").strip()
    if not htchat_url or not htchat_token:
        return jsonify({"ok": False, "erro": "htchat_url e htchat_token são obrigatórios"}), 400

    raw_id = body.get("id")
    if raw_id is None:
        return jsonify({"ok": False, "erro": "id é obrigatório"}), 400

    try:
        msg_internal_id = int(str(raw_id))
    except Exception:
        return jsonify({"ok": False, "erro": "id inválido"}), 400

    node, err = htchat_get_sended(htchat_url, htchat_token, msg_internal_id, verify_ssl=bool(body.get("verify_ssl", True)))
    if err:
        return jsonify({"ok": False, "erro": err}), 500

    ack = node.get("ack")
    status_str = f"{ack}" if ack is not None else "null"
    sb_update_htchat_status_by_idms(str(msg_internal_id), status_str)

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

    r = graphql_json(htchat_url, htchat_token, QUERY_RECIPIENT_EXISTS, {"recipient": recipient, "api_id": api_id},
                    verify_ssl=bool(body.get("verify_ssl", True)))
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
