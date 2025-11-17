import fs from "fs";
import https from "https";
import fetch from "node-fetch";

// Supabase
const SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co";
const SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA";

// SICOOB
const TOKEN_URL = "https://auth.sicoob.com.br/auth/realms/cooperado/protocol/openid-connect/token";
const BASE_URL = "https://api.sicoob.com.br/cobranca-bancaria/v3";

// Carrega cert/key do Supabase
async function getCertificate(user) {
  const url = `${SUPABASE_URL}/rest/v1/sicoob_certifica?user=eq.${user}&select=pem,key&limit=1`;

  const res = await fetch(url, {
    headers: {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
    },
  });

  const data = await res.json();

  if (!data.length) {
    throw new Error("Nenhum certificado encontrado para este usuário");
  }

  const pem = Buffer.from(data[0].pem, "base64");
  const key = Buffer.from(data[0].key, "base64");

  return { pem, key };
}

// Gera token
async function gerarToken(cert, dadosToken) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      TOKEN_URL,
      {
        method: "POST",
        cert: cert.pem,
        key: cert.key,
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
        },
      },
      (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => resolve(JSON.parse(body)));
      }
    );

    req.on("error", reject);
    req.write(new URLSearchParams(dadosToken).toString());
    req.end();
  });
}

// VerceI API endpoint
export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ erro: "Método não permitido" });
  }

  try {
    const payload = req.body;
    const user = payload.user;

    // 1) Certificado
    const cert = await getCertificate(user);

    // 2) Token
    const tokenInfo = await gerarToken(cert, {
      grant_type: "client_credentials",
      client_id: "ca417614-7d6f-4f89-ba39-f18ea496431e",
      scope:
        "boletos_inclusao boletos_consulta boletos_alteracao webhooks_inclusao",
    });

    if (!tokenInfo.access_token) {
      return res.status(500).json({ erro: "Erro ao gerar token", det: tokenInfo });
    }

    const token = tokenInfo.access_token;

    // 3) Emitir boleto
    const r = await fetch(`${BASE_URL}/boletos`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: JSON.stringify(payload),
      agent: new https.Agent({
        cert: cert.pem,
        key: cert.key,
      }),
    });

    const resposta = await r.json();

    return res.status(200).json(resposta);
  } catch (e) {
    return res.status(500).json({ erro: e.message });
  }
}

