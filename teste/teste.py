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
# ===================== CONFIG SICOOB ======================
# ==========================================================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"
SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID_DEFAULT = "ca417614-7d6f-4f89-ba39-f18ea496431e"
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"

# ==========================================================
# ===================== CONFIG SUPABASE ====================
# ==========================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://hysrxadnigzqadnlkynq.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # << use Service Role no Render!

# cache por usuário:
# {
#   "user@dominio.com": {
#       "cert": (cert_path, key_path),
#       "cliente_id": "<CLIENT_ID OAuth>",
#       "conta": 50300
#   }
# }
CERT_CACHE: Dict[str, Dict[str, Any]] = {}

# ==========================================================
# ===================== HTCHAT CONFIG ======================
# ==========================================================

HTCHAT_DEFAULT_URL = os.environ.get("HTCHAT_URL", "https://htchat.idealcontabilidade.net:443/graphql_api")
HTCHAT_DEFAULT_TOKEN = os.environ.get("HTCHAT_TOKEN", "")  # opcional; pode vir no body também

SEND_TEXT = """
mutation send_text(
  $recipient: String!
  $message: String!
  $tipo: String!
  $sender_name: String
) {
  partner_api_send_message(
    recipient: $recipient
    message: $message
    tipo: $tipo
    sender_name: $sender_name
  ) {
    ack api_id id message msg_id recipient sender_name tipo
  }
}
""".strip()

SEND_FILE = """
mutation send_file(
  $recipient: String!
  $message: String
  $tipo: String!
  $sender_name: String
  $file: Upload!
) {
  partner_api_send_message(
    recipient: $recipient
    message: $message
    tipo: $tipo
    sender_name: $sender_name
    file: $file
  ) {
    ack api_id
    arquivo { eurl extensao id mime nome }
    id message msg_id recipient sender_name tipo
  }
}
""".strip()

QUERY_GET_SENDED = """
query get_sended($id: Int!) {
  partner_api_get_sended(id: $id) {
    ack api_id id message msg_id recipient sender_name tipo
    arquivo { eurl extensao id mime nome }
  }
}
""".strip()

# ==========================================================
# ===================== SUPABASE HELPERS ===================
# ==========================================================

def sb_headers():
    if not SUPABASE_KEY:
        return None
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def sb_insert_htchat(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Insere na tabela htchat e retorna a linha inserida (se prefer=return=representation).
    Campos esperados:
      number, mensagem, anexo, idms, status, user
    """
    h = sb_headers()
    if not h:
        return None, "SUPABASE_SERVICE_ROLE_KEY não configurada"
    h2 = dict(h)
    h2["Prefer"] = "return=representation"

    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/htchat",
            headers=h2,
            data=json.dumps(row, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return None, f"Erro ao inserir htchat no Supabase: {e}"

    if not r.ok:
        return None, f"Erro Supabase insert htchat. Status={r.status_code}, texto={r.text}"

    try:
        data = r.json()
        # supabase retorna lista
        if isinstance(data, list) and data:
            return data[0], None
        return {"_raw": data}, None
    except Exception:
        return {"_raw": r.text}, None

def sb_update_htchat_status_by_idms(idms: str, status: str) -> Optional[str]:
    """
    Atualiza htchat.status filtrando por idms.
    """
    h = sb_headers()
    if not h:
        return "SUPABASE_SERVICE_ROLE_KEY não configurada"

    params = {"idms": f"eq.{idms}"}
    body = {"status": status}

    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/htchat",
            headers=h,
            params=params,
            data=json.dumps(body, ensure_ascii=False),
            timeout=20,
        )
    except Exception as e:
        return f"Erro ao atualizar status no Supabase: {e}"

    if not r.ok:
        return f"Erro Supabase update htchat. Status={r.status_code}, texto={r.text}"

    return None

# ==========================================================
# =========== CARREGAR CERTIFICADO + CLIENT_ID + CONTA =====
# ==========================================================

def carregar_certificados_local(
    user: Optional[str] = None
) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[int], Optional[str]]:
    """
    Busca o último certificado salvo na tabela certifica_sicoob.
    Se 'user' for informado, filtra pelos registros daquele usuário.

    Tabela certifica_sicoob:
      - pem (text, base64)
      - key (text, base64)
      - cliente_id (text)  -> CLIENT_ID OAuth do Sicoob
      - conta (bigint)     -> número da conta corrente
      - user (text)

    Retorna:
      ( (cert_path, key_path), cliente_id, conta, erro )
    """

    global CERT_CACHE
    cache_key = user or "_default"

    if cache_key in CERT_CACHE:
        info = CERT_CACHE[cache_key]
        return info["cert"], info.get("cliente_id"), info.get("conta"), None

    if not SUPABASE_KEY:
        return None, None, None, "SUPABASE_SERVICE_ROLE_KEY não configurada"

    params = {
        "select": "pem,key,cliente_id,conta",
        "order": "id.desc",
        "limit": "1",
    }
    if user:
        params["user"] = f"eq.{user}"

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/certifica_sicoob",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
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

    CERT_CACHE[cache_key] = {
        "cert": (cert_path, key_path),
        "cliente_id": cliente_id,
        "conta": conta,
    }

    print(
        f"✔ Certificado carregado do Supabase para {cache_key}: "
        f"{CERT_CACHE[cache_key]['cert']} | cliente_id={cliente_id} | conta={conta}"
    )

    return CERT_CACHE[cache_key]["cert"], cliente_id, conta, None

# ==========================================================
# ================= TOKEN SICOOB (CLIENT_ID DINÂMICO) ======
# ==========================================================

def gerar_token_sicoob(
    cert_files: Tuple[str, str],
    client_id_from_db: Optional[str]
):
    cert_path, key_path = cert_files
    client_id = client_id_from_db or CLIENT_ID_DEFAULT

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": SICOOB_SCOPE,
    }

    print(">> TOKEN: usando client_id =", client_id)

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

# ==========================================================
# ===================== EMITIR BOLETO ======================
# ==========================================================

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

# ==========================================================
# ===================== BAIXAR PDF (SEGUNDA VIA) ===========
# ==========================================================

def baixar_pdf_boleto(
    token: str,
    n_contrato: int,
    n_nosso: int,
    n_cliente: int,
    modalidade: int,
    cert_files: Tuple[str, str]
):
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
# ===================== HTCHAT HELPERS =====================
# ==========================================================

def safe_json(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text}

def graphql_json(url, token, query, variables, verify_ssl=True, timeout=30):
    headers = {"token": token}
    return requests.post(
        url,
        headers=headers,
        json={"query": query, "variables": variables},
        verify=verify_ssl,
        timeout=timeout,
    )

def upload_standard_opsmap(
    url: str,
    token: str,
    variables: Dict[str, Any],
    file_bytes: bytes,
    filename: str,
    mime: Optional[str] = None,
    verify_ssl: bool = True,
    timeout: int = 120
):
    """
    Standard GraphQL multipart (operations/map), enviando operations/map como parts
    e arquivo como part "0" (padrão).
    """
    headers = {"token": token}

    vars2 = dict(variables)
    vars2["file"] = None

    operations = json.dumps({"query": SEND_FILE, "variables": vars2}, ensure_ascii=False)
    file_map = json.dumps({"0": ["variables.file"]}, ensure_ascii=False)

    if not mime:
        mime, _ = mimetypes.guess_type(filename)
    if not mime:
        mime = "application/octet-stream"

    files = {
        "operations": (None, operations, "application/json"),
        "map": (None, file_map, "application/json"),
        "0": (filename, io.BytesIO(file_bytes), mime),
    }
    return requests.post(url, headers=headers, files=files, verify=verify_ssl, timeout=timeout)

def htchat_send_one(
    htchat_url: str,
    htchat_token: str,
    item: Dict[str, Any],
    verify_ssl: bool = True
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Envia 1 mensagem (texto ou arquivo).
    item esperado:
      - recipient (string)
      - tipo (text|document|image|audio|video)
      - message (string)
      - sender_name (opcional)
      - file_b64 (opcional)  base64 do arquivo
      - file_name (opcional)
      - file_mime (opcional)
    """
    recipient = (item.get("recipient") or "").strip()
    tipo = (item.get("tipo") or "text").strip()
    message = item.get("message") or ""
    sender_name = item.get("sender_name") or None

    if not recipient:
        return None, "recipient vazio"

    # Normaliza recipient se vier só número
    # (se você já manda com @s.whatsapp.net, mantém)
    if "@s.whatsapp.net" not in recipient and recipient.isdigit():
        recipient = f"{recipient}@s.whatsapp.net"

    file_b64 = item.get("file_b64")
    if file_b64:
        # envio com arquivo
        file_name = item.get("file_name") or "arquivo.bin"
        file_mime = item.get("file_mime")

        try:
            file_bytes = base64.b64decode(file_b64)
        except Exception as e:
            return None, f"file_b64 inválido: {e}"

        variables = {
            "recipient": recipient,
            "message": message if message else "",
            "tipo": tipo,
            "sender_name": sender_name,
        }

        try:
            r = upload_standard_opsmap(
                htchat_url, htchat_token, variables,
                file_bytes=file_bytes,
                filename=file_name,
                mime=file_mime,
                verify_ssl=verify_ssl
            )
        except Exception as e:
            return None, f"Erro upload: {e}"

        j = safe_json(r)
        if r.status_code != 200 or "errors" in j:
            return None, f"Erro HTChat upload: HTTP {r.status_code} - {j}"

        node = (j.get("data") or {}).get("partner_api_send_message") or {}
        return node, None

    # envio só texto
    if not message:
        return None, "message vazio (para texto é obrigatório)"

    variables = {
        "recipient": recipient,
        "message": message,
        "tipo": tipo,
        "sender_name": sender_name,
    }

    try:
        r = graphql_json(htchat_url, htchat_token, SEND_TEXT, variables, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat texto: {e}"

    j = safe_json(r)
    if r.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat texto: HTTP {r.status_code} - {j}"

    node = (j.get("data") or {}).get("partner_api_send_message") or {}
    return node, None

def htchat_get_sended(
    htchat_url: str,
    htchat_token: str,
    msg_internal_id: int,
    verify_ssl: bool = True
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        r = graphql_json(htchat_url, htchat_token, QUERY_GET_SENDED, {"id": msg_internal_id}, verify_ssl=verify_ssl, timeout=30)
    except Exception as e:
        return None, f"Erro HTChat get_sended: {e}"

    j = safe_json(r)
    if r.status_code != 200 or "errors" in j:
        return None, f"Erro HTChat get_sended: HTTP {r.status_code} - {j}"

    node = (j.get("data") or {}).get("partner_api_get_sended") or {}
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
            return jsonify({
                "ok": False,
                "etapa": "certificado",
                "erro": f"Valor inválido em certifica_sicoob.conta: {conta_corrente}"
            }), 500

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

    pdf_bytes, erro_pdf = baixar_pdf_boleto(
        token, n_contrato, n_nosso, num_cliente_int, modalidade, cert_files
    )
    if erro_pdf:
        return jsonify({"erro": erro_pdf}), 500

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="boleto.pdf"
    )

# -------------------- HTCHAT / WHATSAPP --------------------

@app.post("/htchat/send")
def htchat_send_batch():
    """
    Envia uma LISTA de mensagens com DELAY de 15s entre elas e salva na tabela htchat.

    Body exemplo:
    {
      "user": "teste@gmail.com",
      "delay_seconds": 15,
      "verify_ssl": true,
      "htchat_url": "https://.../graphql_api",     (opcional)
      "htchat_token": "SEU_TOKEN_AQUI",            (opcional)
      "messages": [
        {"recipient":"5569...","tipo":"text","message":"oi","sender_name":""},
        {"recipient":"5569...","tipo":"document","message":"segue","file_name":"a.pdf","file_mime":"application/pdf","file_b64":"..."}
      ]
    }
    """
    body = request.get_json(silent=True) or {}

    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

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

    htchat_url = (body.get("htchat_url") or HTCHAT_DEFAULT_URL).strip()
    htchat_token = (body.get("htchat_token") or HTCHAT_DEFAULT_TOKEN).strip()
    if not htchat_token:
        return jsonify({"ok": False, "erro": "HTCHAT_TOKEN não informado (env HTCHAT_TOKEN ou no body htchat_token)"}), 400

    results = []
    for idx, item in enumerate(messages, start=1):
        recipient = (item.get("recipient") or "").strip()

        # Pré-grava no Supabase como queued (idms vazio por enquanto)
        anexo_desc = ""
        if item.get("file_b64"):
            anexo_desc = item.get("file_name") or "arquivo"

        row = {
            "number": recipient,
            "mensagem": item.get("message") or "",
            "anexo": anexo_desc,
            "idms": "",                  # preenche depois do envio
            "status": "queued",
            "user": user,
        }
        sb_row, sb_err = sb_insert_htchat(row)
        if sb_err:
            # mesmo se falhar o log no supabase, tenta enviar
            print("⚠ Falha ao inserir htchat:", sb_err)

        # Envia de fato
        node, err = htchat_send_one(htchat_url, htchat_token, item, verify_ssl=verify_ssl)

        if err:
            # atualiza status como erro (se tiver inserido)
            # não temos idms; salva status genérico
            results.append({"i": idx, "recipient": recipient, "ok": False, "erro": err})
            continue

        # pega id retornado pelo send_message (é o Int que você usa no get_sended)
        msg_internal_id = node.get("id")
        ack = node.get("ack")

        # Atualiza linha no supabase (se possível) usando idms=msg_internal_id
        # Como inserimos idms vazio, fazemos outro patch filtrando por (user, number, mensagem, status=queued) seria arriscado.
        # Então: inserção "best effort" e aqui fazemos um novo INSERT com idms preenchido se quiser rastreio perfeito.
        # Para ficar simples e robusto: fazemos um NOVO INSERT "sent" com idms preenchido.
        row2 = {
            "number": recipient,
            "mensagem": item.get("message") or "",
            "anexo": anexo_desc,
            "idms": str(msg_internal_id) if msg_internal_id is not None else "",
            "status": f"sent_ack_{ack}" if ack is not None else "sent",
            "user": user,
        }
        _, _ = sb_insert_htchat(row2)

        results.append({
            "i": idx,
            "recipient": recipient,
            "ok": True,
            "id": msg_internal_id,
            "ack": ack,
            "msg_id": node.get("msg_id"),
            "tipo": node.get("tipo"),
            "arquivo": node.get("arquivo"),
        })

        # delay entre mensagens (exceto após a última)
        if idx < len(messages) and delay_seconds > 0:
            time.sleep(delay_seconds)

    return jsonify({"ok": True, "delay_seconds": delay_seconds, "results": results})

@app.post("/htchat/status")
def htchat_update_status():
    """
    Consulta o get_sended(id) e atualiza htchat.status com o ack.

    Body:
    {
      "user": "teste@gmail.com",
      "id": 14,
      "verify_ssl": true,
      "htchat_url": "...",      (opcional)
      "htchat_token": "..."     (opcional)
    }
    """
    body = request.get_json(silent=True) or {}

    user = (body.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "erro": "Campo 'user' é obrigatório"}), 400

    raw_id = body.get("id")
    if raw_id is None:
        return jsonify({"ok": False, "erro": "Campo 'id' (Int do send_message) é obrigatório"}), 400

    try:
        msg_internal_id = int(str(raw_id))
    except Exception:
        return jsonify({"ok": False, "erro": f"id inválido: {raw_id}"}), 400

    verify_ssl = bool(body.get("verify_ssl", True))
    htchat_url = (body.get("htchat_url") or HTCHAT_DEFAULT_URL).strip()
    htchat_token = (body.get("htchat_token") or HTCHAT_DEFAULT_TOKEN).strip()
    if not htchat_token:
        return jsonify({"ok": False, "erro": "HTCHAT_TOKEN não informado (env HTCHAT_TOKEN ou no body htchat_token)"}), 400

    node, err = htchat_get_sended(htchat_url, htchat_token, msg_internal_id, verify_ssl=verify_ssl)
    if err:
        return jsonify({"ok": False, "erro": err}), 500

    ack = node.get("ack")
    status_str = f"{ack}" if ack is not None else "null"

    # Atualiza tabela htchat pelo idms (string do id)
    up_err = sb_update_htchat_status_by_idms(str(msg_internal_id), status_str)
    if up_err:
        # não impede retornar status
        print("⚠ Falha ao atualizar status no Supabase:", up_err)

    return jsonify({"ok": True, "id": msg_internal_id, "ack": ack, "updated_status": status_str, "node": node})

# ==========================================================
# ===================== MAIN ===============================
# ==========================================================

if __name__ == "__main__":
    # local:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
