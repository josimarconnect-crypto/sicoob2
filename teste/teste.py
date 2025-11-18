from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
import tempfile
import os
import io
import base64
from typing import Dict, Any, Tuple, Optional, List

app = Flask(__name__)
CORS(app)

# ===================== CONFIG SICOOB ======================

SICOOB_TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token"
SICOOB_BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3"

SICOOB_BOLETO_URL = f"{SICOOB_BASE_URL}/boletos"
SICOOB_SEGUNDA_VIA_URL = f"{SICOOB_BASE_URL}/boletos/segunda-via"

CLIENT_ID = "ca417614-7d6f-4f89-ba39-f18ea496431e"
SICOOB_SCOPE = "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao"

# ===================== CONFIG SUPABASE ======================

SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"
)

# cache por usuário:
# {
#   "user@dominio.com": {
#       "cert": (cert_path, key_path),
#       "cliente_id": "409987"
#   }
# }
CERT_CACHE: Dict[str, Dict[str, Any]] = {}


# ===================== CARREGAR CERTIFICADO DO SUPABASE ======================

def carregar_certificados_local(
    user: Optional[str] = None
) -> Tuple[Optional[Tuple[str, str]], Optional[str], Optional[str]]:
    """
    Busca o último certificado salvo na tabela certifica_sicoob.
    Se 'user' for informado, filtra pelos registros daquele usuário.
    Campos:
      - pem (base64)
      - key (base64)
      - cliente_id (text)  -> númeroCliente do Sicoob
      - user (text)
    Retorna:
      ( (cert_path, key_path), cliente_id, erro )
    """

    global CERT_CACHE

    cache_key = user or "_default"

    # Se já está em cache, reaproveita
    if cache_key in CERT_CACHE:
        info = CERT_CACHE[cache_key]
        return info["cert"], info.get("cliente_id"), None

    if not SUPABASE_KEY:
        return None, None, "SUPABASE_SERVICE_ROLE_KEY não configurada"

    # Monta parâmetros da consulta
    params = {
        "select": "pem,key,cliente_id",
        "order": "id.desc",
        "limit": "1",
    }

    # se tiver user, filtra: ?user=eq.email@...
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
        return None, None, f"Erro ao chamar Supabase: {e}"

    if not resp.ok:
        return None, None, f"Erro Supabase. Status={resp.status_code}, texto={resp.text}"

    try:
        rows: List[Dict[str, Any]] = resp.json()
    except ValueError:
        return None, None, f"Resposta inválida do Supabase: {resp.text}"

    if not rows:
        return None, None, "Nenhum certificado encontrado para este usuário"

    row = rows[0]
    pem_b64 = row.get("pem")
    key_b64 = row.get("key")
    cliente_id = row.get("cliente_id")

    if not pem_b64 or not key_b64:
        return None, None, "Campos pem/key vazios"

    # Decodificar base64
    try:
        pem_bytes = base64.b64decode(pem_b64)
        key_bytes = base64.b64decode(key_b64)
    except Exception as e:
        return None, None, f"Erro ao decodificar base64: {e}"

    # Criar arquivos temporários .pem e .key
    try:
        cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
        key_fd, key_path = tempfile.mkstemp(suffix=".key")

        with os.fdopen(cert_fd, "wb") as f:
            f.write(pem_bytes)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key_bytes)

    except Exception as e:
        return None, None, f"Erro ao criar arquivos temporários: {e}"

    CERT_CACHE[cache_key] = {
        "cert": (cert_path, key_path),
        "cliente_id": cliente_id,
    }

    print(
        f"✔ Certificado carregado do Supabase para {cache_key}: "
        f"{CERT_CACHE[cache_key]['cert']} | cliente_id={cliente_id}"
    )

    return CERT_CACHE[cache_key]["cert"], cliente_id, None


# ===================== TOKEN SICOOB ======================

def gerar_token_sicoob(cert_files: Tuple[str, str]):
    cert_path, key_path = cert_files

    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "scope": SICOOB_SCOPE,
    }

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
        return None, f"Resposta TOKEN inválida: {resp.text}"

    if not resp.ok:
        return None, f"Erro Token: {j}"

    token = j.get("access_token")
    if not token:
        return None, "Token não retornado"

    return token, None


# ===================== EMITIR BOLETO ======================

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
        return None, f"Resposta inválida do Sicoob: {resp.text}"

    if not resp.ok:
        return None, f"Erro na emissão: {j}"

    return j, None


# ===================== BAIXAR PDF ======================

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


# ===================== ROTAS ======================

@app.get("/")
def home():
    return "API Sicoob (Flask) — certificado e cliente_id vindos do Supabase por usuário."


@app.post("/sicoob/emitir")
def api_emitir():
    """
    Espera um JSON com todos os dados do boleto + campo 'user'
    Exemplo:
    {
        "user": "email@cliente.com",
        "numeroContaCorrente": 218812,
        "codigoModalidade": 1,
        "numeroParcela": 1,
        "seuNumero": "1",
        "valor": 150,
        "dataEmissao": "2025-11-17",
        "dataVencimento": "2025-12-17",
        ...
    }

    Se "numeroCliente" NÃO for enviado, o backend usa o "cliente_id"
    da tabela certifica_sicoob, vinculado a esse user.
    """
    payload = request.get_json(silent=True) or {}

    user = payload.get("user")
    # Remove "user" antes de mandar pro Sicoob
    payload.pop("user", None)

    cert_files, cliente_id, erro_cert = carregar_certificados_local(user)
    if erro_cert:
        return jsonify({"ok": False, "etapa": "certificado", "erro": erro_cert}), 500

    # Se numeroCliente não enviado no payload, tenta usar cliente_id do Supabase
    if not payload.get("numeroCliente") and cliente_id:
        try:
            payload["numeroCliente"] = int(str(cliente_id))
        except ValueError:
            # Se der erro de conversão, manda como está (string) — o Sicoob vai acusar se estiver inválido
            payload["numeroCliente"] = cliente_id

    token, erro_tk = gerar_token_sicoob(cert_files)
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
    """
    Espera um JSON:
    {
        "user": "email@cliente.com",
        "numeroContratoCobranca": 123,
        "nossoNumero": 456,
        "numeroCliente": 409987,        # opcional
        "codigoModalidade": 1
    }

    Se "numeroCliente" não vier, o backend tenta usar "cliente_id"
    da tabela certifica_sicoob vinculada ao user.
    """
    dados = request.get_json(silent=True) or {}

    user = dados.get("user")
    # "user" só para achar o certificado e o cliente_id
    cert_files, cliente_id, erro_cert = carregar_certificados_local(user)
    if erro_cert:
        return jsonify({"erro": erro_cert}), 500

    # Decide o numeroCliente
    num_cliente = dados.get("numeroCliente") or cliente_id
    if not num_cliente:
        return jsonify({"erro": "numeroCliente não informado e cliente_id não encontrado para este usuário"}), 400

    try:
        num_cliente_int = int(str(num_cliente))
    except ValueError:
        return jsonify({"erro": f"numeroCliente/cliente_id inválido: {num_cliente}"}), 400

    token, erro_tk = gerar_token_sicoob(cert_files)
    if erro_tk:
        return jsonify({"erro": erro_tk}), 500

    try:
        n_contrato = int(str(dados.get("numeroContratoCobranca")))
        n_nosso = int(str(dados.get("nossoNumero")))
        modalidade = int(str(dados.get("codigoModalidade")))
    except Exception as e:
        return jsonify({"erro": f"Parâmetros numéricos inválidos: {e}"}), 400

    pdf_bytes, erro_pdf = baixar_pdf_boleto(
        token,
        n_contrato,
        n_nosso,
        num_cliente_int,
        modalidade,
        cert_files
    )

    if erro_pdf:
        return jsonify({"erro": erro_pdf}), 500

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="boleto.pdf"
    )


if __name__ == "__main__":
    # para rodar local
    app.run(host="0.0.0.0", port=5000, debug=True)
