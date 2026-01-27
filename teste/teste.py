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
    """
    Insere na tabela htchat.
    Usa apenas a coluna 'numero' que já existe na tabela.
    """
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
    """
    Atualiza htchat.status filtrando por idms.
    """
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
# ===================== CONFIG SICOOB ======================
# ==========================================================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"
SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID_DEFAULT = "ca417614-7d6f-4f89-ba39-f18ea496431e"
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"

CERT_CACHE: Dict[str, Dict[str, Any]] = {}

def carregar_certificados_local(user: Optional[str] = None) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[int], Optional[str]]:
    """
    Busca o último certificado na certifica_sicoob e cria arquivos temporários PEM/KEY.
    Retorna (cert_files, cliente_id_oauth, conta, erro)
    """
    global CERT_CACHE
    cache_key = user or "_default"
    if cache_key in CERT_CACHE:
        info = CERT_CACHE[cache_key]
        return info["cert"], info.get("cliente_id"), info.get("conta"), None

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
        pem_bytes = base64.b64decode(pem_b64)
        key_bytes = base64.b64decode(key_b64)
    except Exception as e:
        return None, None, None, f"Erro ao decodificar base64: {e}"

    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")
        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)
    except Exception as e:
        return None, None, None, f"Erro ao criar arquivos temporários: {e}"

    CERT_CACHE[cache_key] = {"cert": (cert_path, key_path), "cliente_id": cliente_id, "conta": conta}
    return CERT_CACHE[cache_key]["cert"], cliente_id, conta, None

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
        return None, f"Resposta TOKEN inválida (não é JSON): {resp.text}"

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
        return None, f"Resposta inválida do Sicoob (não é JSON): {resp.text}"

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
        return None, f"Resposta inválida ao baixar PDF: {resp.text}"

    if not resp.ok:
        return None, data

    pdf_b64 = data.get("resultado", {}).get("pdfBoleto") or data.get("pdfBoleto")
    if not pdf_b64:
        return None, "Campo pdfBoleto não encontrado"

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
    arquivo {
      url
      filename
      mime
    }
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
    arquivo {
      url
      filename
      mime
    }
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
    arquivo {
      url
      filename
      mime
    }
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

# ---------- Upload variants (como no seu tester) ----------

def upload_standard_opsmap(url, token, variables, file_path, key="0", map_path="variables.file",
                           ops_in="files", verify_ssl=True, timeout=120):
    headers = {"token": token}
    vars2 = dict(variables)
    vars2["file"] = None

    operations = json.dumps({"query": SEND_FILE, "variables": vars2}, ensure_ascii=False)
    file_map = json.dumps({key: [map_path]}, ensure_ascii=False)

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    if not mime:
        mime = "application/octet-stream"

    if ops_in == "files":
        with open(file_path, "rb") as f:
            files = {
                "operations": (None, operations, "application/json"),
                "map": (None, file_map, "application/json"),
                key: (filename, f, mime),
            }
            return requests.post(url, headers=headers, files=files, verify=verify_ssl, timeout=timeout)

    data = {"operations": operations, "map": file_map}
    with open(file_path, "rb") as f:
        files = {key: (filename, f, mime)}
        return requests.post(url, headers=headers, data=data, files=files, verify=verify_ssl, timeout=timeout)

def upload_custom_query_variables_file(url, token, variables, file_path, verify_ssl=True, timeout=120):
    headers = {"token": token}
    vars2 = dict(variables)
    vars2["file"] = None

    data = {"query": SEND_FILE, "variables": json.dumps(vars2, ensure_ascii=False)}

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    if not mime:
        mime = "application/octet-stream"

    with open(file_path, "rb") as f:
        files = {"file": (filename, f, mime)}
        return requests.post(url, headers=headers, data=data, files=files, verify=verify_ssl, timeout=timeout)

def upload_custom_query_file_only(url, token, variables, file_path, verify_ssl=True, timeout=120):
    headers = {"token": token}
    data = {
        "query": SEND_FILE,
        "recipient": variables.get("recipient", ""),
        "tipo": variables.get("tipo", ""),
        "message": variables.get("message", ""),
        "sender_name": variables.get("sender_name") or "",
    }

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    if not mime:
        mime = "application/octet-stream"

    with open(file_path, "rb") as f:
        files = {"file": (filename, f, mime)}
        return requests.post(url, headers=headers, data=data, files=files, verify=verify_ssl, timeout=timeout)

def upload_simple_multipart(url, token, variables, file_path, verify_ssl=True, timeout=120):
    headers = {"token": token}
    data = {
        "query": SEND_FILE,
        "recipient": variables.get("recipient", ""),
        "tipo": variables.get("tipo", ""),
        "message": variables.get("message", ""),
    }
    if variables.get("sender_name"):
        data["sender_name"] = variables["sender_name"]

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    if not mime:
        mime = "application/octet-stream"

    with open(file_path, "rb") as f:
        files = {"file": (filename, f, mime)}
        return requests.post(url, headers=headers, data=data, files=files, verify=verify_ssl, timeout=timeout)

def body_lower(resp: requests.Response) -> str:
    try:
        j = resp.json()
    except Exception:
        j = {"_raw": resp.text}
    return json.dumps(j, ensure_ascii=False).lower()

def send_file_bruteforce(url, token, variables, file_path, verify_ssl=True):
    """
    Testa vários jeitos de upload e retorna (resp, modo).
    """
    attempts = []

    for key in ["0", "file"]:
        for ops_in in ["files", "data"]:
            attempts.append((
                f"standard ops/map | key={key} | ops_in={ops_in}",
                lambda k=key, oi=ops_in: upload_standard_opsmap(
                    url, token, variables, file_path,
                    key=k, map_path="variables.file", ops_in=oi, verify_ssl=verify_ssl
                )
            ))

    attempts.append(("custom query+variables+file", lambda: upload_custom_query_variables_file(url, token, variables, file_path, verify_ssl=verify_ssl)))
    attempts.append(("custom query+fields+file", lambda: upload_custom_query_file_only(url, token, variables, file_path, verify_ssl=verify_ssl)))
    attempts.append(("simple multipart", lambda: upload_simple_multipart(url, token, variables, file_path, verify_ssl=verify_ssl)))

    last = None
    for label, fn in attempts:
        try:
            r = fn()
            last = (r, label)
            b = body_lower(r)

            # sucesso típico
            if r.status_code == 200:
                try:
                    jj = r.json()
                except Exception:
                    jj = {}
                if "errors" not in jj:
                    return r, label

            # se resposta é diferente das mensagens clássicas, pode ser útil parar aqui
            if ("file is nil or empty" not in b) and ("http: no such file" not in b):
                return r, label
        except Exception:
            continue

    return last if last else (None, "Nenhum método funcionou")

def htchat_parse_send_response(resp: requests.Response) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    j = safe_json(resp)
    if resp.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat: HTTP {resp.status_code} - {j}"

    data = j.get("data") or {}
    node = pick_first(data.get("partner_api_send_message"))
    if not node:
        return None, f"Resposta HTChat inesperada: {j}"
    return node, None

def htchat_send_one(htchat_url: str, htchat_token: str, item: Dict[str, Any], verify_ssl: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    recipient = normalize_recipient(item.get("recipient", ""))
    tipo = (item.get("tipo") or "text").strip()
    message = item.get("message") or ""
    sender_name = item.get("sender_name") or ""

    if not recipient:
        return None, "recipient vazio"

    # arquivo via base64 no JSON
    if item.get("file_b64"):
        file_name = item.get("file_name") or "arquivo.bin"
        file_mime = item.get("file_mime") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        try:
            file_bytes = base64.b64decode(item["file_b64"])
        except Exception as e:
            return None, f"file_b64 inválido: {e}"

        vars2 = {"recipient": recipient, "message": message if message else "", "tipo": tipo, "sender_name": sender_name}

        # upload padrão ops/map com bytes (sem arquivo em disco)
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

        return htchat_parse_send_response(resp)

    # envio de arquivo vindo como caminho (se você decidir usar no futuro)
    if item.get("file_path"):
        fp = item["file_path"]
        if not os.path.exists(fp):
            return None, f"file_path não existe: {fp}"

        vars2 = {"recipient": recipient, "message": message if message else "", "tipo": tipo, "sender_name": sender_name}
        resp, mode = send_file_bruteforce(htchat_url, htchat_token, vars2, fp, verify_ssl=verify_ssl)
        if resp is None:
            return None, "Falha em todos os métodos de upload"
        node, err = htchat_parse_send_response(resp)
        if err:
            return None, f"{err} | modo={mode}"
        node["_upload_mode"] = mode
        return node, None

    # texto
    if not str(message).strip():
        return None, "message vazio (para texto é obrigatório)"

    try:
        resp = graphql_json(htchat_url, htchat_token, SEND_TEXT, {
            "recipient": recipient, "message": message, "tipo": tipo, "sender_name": sender_name
        }, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat texto: {e}"

    return htchat_parse_send_response(resp)

def htchat_get_sended(htchat_url: str, htchat_token: str, msg_internal_id: int, verify_ssl: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = graphql_json(htchat_url, htchat_token, QUERY_GET_SENDED, {"id": msg_internal_id}, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat get_sended: {e}"

    j = safe_json(r)
    if r.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat get_sended: HTTP {r.status_code} - {j}"

    data = j.get("data") or {}
    node = pick_first(data.get("partner_api_get_sended"))
    if not node:
        return None, f"Resposta get_sended inesperada: {j}"
    return node, None


# ==========================================================
# ===================== ROTAS ==============================
# ==========================================================

@app.get("/")
def home():
    return "API Unificada (Flask) — Sicoob + HTChat/WhatsApp + Supabase."

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
    except ValueError:
        return jsonify({"erro": f"numeroCliente inválido: {numero_cliente}"}), 400

    token, erro_tk = gerar_token_sicoob(cert_files, cliente_id_oauth)
    if erro_tk:
        return jsonify({"erro": erro_tk}), 500

    try:
        n_contrato = int(str(dados.get("numeroContratoCobranca")))
        n_nosso = int(str(dados.get("nossoNumero")))
        modalidade = int(str(dados.get("codigoModalidade")))
    except Exception as e:
        return jsonify({"erro": f"Parâmetros numéricos inválidos: {e}"}), 400

    pdf_bytes, erro_pdf = baixar_pdf_boleto(token, n_contrato, n_nosso, num_cliente_int, modalidade, cert_files)
    if erro_pdf:
        return jsonify({"erro": erro_pdf}), 500

    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name="boleto.pdf")

# -------------------- HTCHAT / WHATSAPP --------------------

@app.post("/htchat/send")
def htchat_send_batch():
    """
    Body:
    {
      "user": "teste@gmail.com",
      "delay_seconds": 15,
      "verify_ssl": true,
      "htchat_url": "https://.../graphql_api",
      "htchat_token": "TOKEN",
      "messages": [
        {"recipient":"5569...","tipo":"text","message":"oi"},
        {"recipient":"5569...","tipo":"document","message":"segue","file_name":"a.pdf","file_mime":"application/pdf","file_b64":"..."}
      ]
    }
    """
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

            # Extrair informações do arquivo se existir
            arquivo_info = ""
            if node.get("arquivo"):
                arquivo = node["arquivo"]
                if isinstance(arquivo, dict):
                    arquivo_info = f"{arquivo.get('filename') or ''} ({arquivo.get('mime') or ''})"
                elif isinstance(arquivo, str):
                    arquivo_info = arquivo

            results.append({
                "i": idx,
                "recipient": recipient_norm,
                "ok": True,
                "id": msg_internal_id,
                "ack": ack,
                "msg_id": node.get("msg_id"),
                "tipo": node.get("tipo"),
                "arquivo": arquivo_info,
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

    r = graphql_json(htchat_url, htchat_token, QUERY_RECIPIENT_EXISTS,
                    {"recipient": recipient, "api_id": api_id}, verify_ssl=bool(body.get("verify_ssl", True)))
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
# ===================== MAIN ===============================
# ==========================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
