const https = require("https");

// ===================== CONFIG SUPABASE ======================

const SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co";
const SUPABASE_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA";

// ===================== CONFIG SICOOB ======================

const SICOOB_TOKEN_URL =
  "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token";
const SICOOB_BASE_URL =
  "https://api.sicoob.com.br/cobranca-bancaria/v3";

const SICOOB_BOLETO_URL = `${SICOOB_BASE_URL}/boletos`;
const SICOOB_SEGUNDA_VIA_URL = `${SICOOB_BASE_URL}/boletos/segunda-via`;

const CLIENT_ID = "ca417614-7d6f-4f89-ba39-f18ea496431e";
const SICOOB_SCOPE =
  "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao";

// ===================== HELPERS ======================

function jsonResponse(res, status, data) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(data));
}

// POST com cert (form ou JSON)
function httpsPostWithCert(urlString, bodyString, cert, key, headers = {}) {
  const url = new URL(urlString);

  const options = {
    hostname: url.hostname,
    path: url.pathname + url.search,
    method: "POST",
    cert,
    key,
    headers,
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        resolve({ statusCode: res.statusCode, body: data });
      });
    });

    req.on("error", (err) => reject(err));
    req.write(bodyString);
    req.end();
  });
}

// GET com cert
function httpsGetWithCert(urlString, cert, key, headers = {}) {
  const url = new URL(urlString);

  const options = {
    hostname: url.hostname,
    path: url.pathname + url.search,
    method: "GET",
    cert,
    key,
    headers,
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        resolve({ statusCode: res.statusCode, body: data });
      });
    });

    req.on("error", (err) => reject(err));
    req.end();
  });
}

// ===================== CERTIFICADO via SUPABASE ======================

async function carregarCertificado(user) {
  if (!user) {
    throw new Error("Campo 'user' não informado no payload.");
  }

  const url =
    `${SUPABASE_URL}/rest/v1/sicoob_certifica` +
    `?user=eq.${encodeURIComponent(user)}` +
    `&select=pem,key` +
    `&order=id.desc&limit=1`;

  const resp = await fetch(url, {
    headers: {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
    },
  });

  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(
      `Erro ao buscar certificado no Supabase (status ${resp.status}): ${txt}`
    );
  }

  const rows = await resp.json();
  if (!rows || !rows.length) {
    throw new Error("Nenhum certificado encontrado para este usuário.");
  }

  const row = rows[0];
  if (!row.pem || !row.key) {
    throw new Error("Campos pem/key vazios no Supabase.");
  }

  const pemBuffer = Buffer.from(row.pem, "base64");
  const keyBuffer = Buffer.from(row.key, "base64");

  return { cert: pemBuffer, key: keyBuffer };
}

// ===================== TOKEN SICOOB ======================

async function gerarToken(cert, key) {
  const form = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: CLIENT_ID,
    scope: SICOOB_SCOPE,
  });

  const { statusCode, body } = await httpsPostWithCert(
    SICOOB_TOKEN_URL,
    form.toString(),
    cert,
    key,
    {
      "Content-Type": "application/x-www-form-urlencoded",
    }
  );

  let json;
  try {
    json = JSON.parse(body);
  } catch (e) {
    throw new Error(
      `Resposta inválida do TOKEN (status ${statusCode}): ${body}`
    );
  }

  if (statusCode < 200 || statusCode >= 300) {
    throw new Error(
      `Erro ao gerar token (status ${statusCode}): ${JSON.stringify(json)}`
    );
  }

  if (!json.access_token) {
    throw new Error("Token não retornado na resposta do Sicoob.");
  }

  return json.access_token;
}

// ===================== EMITIR BOLETO ======================

async function emitirBoleto(token, payload, cert, key) {
  const bodyString = JSON.stringify(payload || {});

  const { statusCode, body } = await httpsPostWithCert(
    SICOOB_BOLETO_URL,
    bodyString,
    cert,
    key,
    {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    }
  );

  let json;
  try {
    json = JSON.parse(body);
  } catch (e) {
    throw new Error(
      `Resposta inválida ao emitir boleto (status ${statusCode}): ${body}`
    );
  }

  if (statusCode < 200 || statusCode >= 300) {
    throw new Error(
      `Erro na emissão de boleto (status ${statusCode}): ${JSON.stringify(
        json
      )}`
    );
  }

  return json;
}

// ===================== BAIXAR PDF ======================

async function baixarPdf(token, params, cert, key) {
  const {
    numeroContratoCobranca,
    nossoNumero,
    numeroCliente,
    codigoModalidade,
  } = params;

  const url =
    `${SICOOB_SEGUNDA_VIA_URL}` +
    `?numeroCliente=${encodeURIComponent(numeroCliente)}` +
    `&codigoModalidade=${encodeURIComponent(codigoModalidade)}` +
    `&nossoNumero=${encodeURIComponent(nossoNumero)}` +
    `&numeroContratoCobranca=${encodeURIComponent(
      numeroContratoCobranca
    )}` +
    `&gerarPdf=true`;

  const { statusCode, body } = await httpsGetWithCert(
    url,
    cert,
    key,
    {
      Authorization: `Bearer ${token}`,
    }
  );

  let json;
  try {
    json = JSON.parse(body);
  } catch (e) {
    throw new Error(
      `Resposta inválida ao baixar PDF (status ${statusCode}): ${body}`
    );
  }

  if (statusCode < 200 || statusCode >= 300) {
    throw new Error(
      `Erro ao baixar PDF (status ${statusCode}): ${JSON.stringify(json)}`
    );
  }

  const pdfB64 =
    (json.resultado && json.resultado.pdfBoleto) || json.pdfBoleto;

  if (!pdfB64) {
    throw new Error("Campo pdfBoleto não encontrado na resposta do Sicoob.");
  }

  return Buffer.from(pdfB64, "base64");
}

// ===================== HANDLER VERCEL ======================

module.exports = async (req, res) => {
  // CORS básico
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader(
    "Access-Control-Allow-Methods",
    "OPTIONS, POST"
  );
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Content-Type"
  );

  if (req.method === "OPTIONS") {
    res.statusCode = 200;
    return res.end();
  }

  if (req.method !== "POST") {
    return jsonResponse(res, 405, {
      ok: false,
      erro: "Método não permitido. Use POST.",
    });
  }

  try {
    // op=emitir ou op=pdf
    const op = (req.query && req.query.op) || "emitir";

    // body pode vir já como objeto ou como string
    let payload = req.body || {};
    if (typeof payload === "string") {
      try {
        payload = JSON.parse(payload || "{}");
      } catch (e) {
        payload = {};
      }
    }

    const user = payload.user;
    if (!user) {
      return jsonResponse(res, 400, {
        ok: false,
        erro: "Campo 'user' é obrigatório no payload.",
      });
    }

    // 1) Certificado
    const { cert, key } = await carregarCertificado(user);

    // 2) Token
    const token = await gerarToken(cert, key);

    if (op === "emitir") {
      // 3) Emissão de boleto
      const result = await emitirBoleto(token, payload, cert, key);
      const r = result.resultado || result;

      return jsonResponse(res, 200, {
        ok: true,
        resposta: result,
        numeroContratoCobranca: r.numeroContratoCobranca,
        nossoNumero: r.nossoNumero,
        pdfBoleto: r.pdfBoleto || null,
      });
    }

    if (op === "pdf") {
      // Espera no body:
      // { user, numeroContratoCobranca, nossoNumero, numeroCliente, codigoModalidade }
      const pdfBuffer = await baixarPdf(token, payload, cert, key);

      res.statusCode = 200;
      res.setHeader("Content-Type", "application/pdf");
      res.setHeader(
        "Content-Disposition",
        'inline; filename="boleto.pdf"'
      );
      return res.end(pdfBuffer);
    }

    // operação desconhecida
    return jsonResponse(res, 400, {
      ok: false,
      erro: "Parâmetro 'op' inválido. Use 'emitir' ou 'pdf'.",
    });
  } catch (e) {
    console.error("Erro geral Sicoob API:", e);
    return jsonResponse(res, 500, {
      ok: false,
      erro: e.message || String(e),
    });
  }
};
