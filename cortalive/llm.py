"""Cliente de IA multi-provedor via HTTP puro (sem SDKs). Usa a SUA chave; tenta na ordem os que estiverem configurados.

Provedores (variáveis de ambiente ou `.env`):
  GEMINI_API_KEY      Google Gemini (AI Studio)
  ANTHROPIC_API_KEY   Claude (Anthropic)
  OPENAI_API_KEY      OpenAI
  VERTEX_CRED         caminho do JSON da conta de serviço do Google Cloud (Vertex AI)
`IA=gemini|claude|openai|vertex` escolhe qual tentar primeiro.
"""
import json
import os
import re
import time

import requests

from cortalive import config
from cortalive.util import log

TIMEOUT = 300
TIMEOUT_CLAUDE = 600
TIMEOUT_LISTAR_MODELOS = 30
ESPERA_SOBRECARGA_SEG = 20             # 429/5xx: espera isso e tenta de novo uma vez
TAM_ERRO_HTTP = 200                    # caracteres do corpo de erro HTTP guardados na mensagem
TAM_ERRO_TESTE = 160
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_PADRAO = "gemini-2.5-flash"
CLAUDE_PADRAO = "claude-sonnet-5-5"
OPENAI_PADRAO = "gpt-4.1"

# Variáveis de ambiente que guardam segredos: o valor delas é apagado de qualquer mensagem de erro/log.
VARIAVEIS_SEGREDO = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")
_RE_PARAM_CHAVE = re.compile(r"(?i)([?&](?:key|api_key|access_token)=)[^&\s)'\"]+")
TAM_MIN_SEGREDO = 6                    # valores menores que isso não são tratados como segredo (evita apagar "x")

NOMES_IA = {"gemini": "gemini", "google": "gemini", "claude": "anthropic", "anthropic": "anthropic",
            "openai": "openai", "chatgpt": "openai", "gpt": "openai", "vertex": "vertex"}

MODELO_USADO = None


def mascarar_segredos(texto):
    """Apaga de `texto` os parâmetros ?key=… e os valores das chaves configuradas (nunca logar segredo)."""
    texto = _RE_PARAM_CHAVE.sub(lambda m: m.group(1) + "***", str(texto))
    for nome in VARIAVEIS_SEGREDO:
        valor = os.environ.get(nome, "")
        if len(valor) >= TAM_MIN_SEGREDO:
            texto = texto.replace(valor, "***")
    return texto


def _descrever_erro(e):
    """Mensagem curta e sem segredo. Erros de conexão do requests trazem a URL inteira: só o tipo basta."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {mascarar_segredos(e.response.text[:TAM_ERRO_HTTP])}"
    if isinstance(e, requests.RequestException):
        return type(e).__name__
    return f"{type(e).__name__}: {mascarar_segredos(e)}"


def _anthropic(prompt, sistema):
    global MODELO_USADO
    modelo = os.environ.get("ANTHROPIC_MODEL", CLAUDE_PADRAO)
    # Os modelos novos pensam antes de responder; esforço "medium" + teto alto evita resposta vazia.
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=TIMEOUT_CLAUDE, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": os.environ.get("ANTHROPIC_VERSION", "2023-06-01"),
        "content-type": "application/json"}, json={
        "model": modelo, "max_tokens": 32000, "system": sistema,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": os.environ.get("ANTHROPIC_EFFORT", "medium")},
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    j = r.json()
    if j.get("stop_reason") == "refusal":
        raise RuntimeError("a IA recusou o pedido (stop_reason=refusal)")
    txt = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    if not txt.strip():
        raise RuntimeError(f"resposta vazia (stop_reason={j.get('stop_reason')})")
    MODELO_USADO = modelo
    return txt


def _gemini_modelo(chave):
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    try:
        r = requests.get(GEMINI_URL, headers={"x-goog-api-key": chave}, timeout=TIMEOUT_LISTAR_MODELOS)
        nomes = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        for pref in ("pro", "flash"):
            cand = sorted([n for n in nomes if n.startswith("gemini") and pref in n
                           and not any(x in n for x in ("lite", "image", "tts", "live", "exp", "preview"))])
            if cand:
                return cand[-1]
    except Exception as e:  # noqa: BLE001 - sem lista de modelos: usa o padrão
        log(f"não consegui listar os modelos do Gemini ({_descrever_erro(e)}); usando o padrão", "llm")
    return GEMINI_PADRAO


def _gemini(prompt, sistema):
    global MODELO_USADO
    chave = os.environ.get("GEMINI_API_KEY")
    modelo = _gemini_modelo(chave)
    r = requests.post(
        f"{GEMINI_URL}/{modelo}:generateContent", headers={"x-goog-api-key": chave},
        timeout=TIMEOUT, json={
            "systemInstruction": {"parts": [{"text": sistema}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}})
    r.raise_for_status()
    MODELO_USADO = modelo
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


VERTEX_MODELOS = ["gemini-3.1-pro", "gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3-flash-preview",
                  "gemini-3.1-flash", "gemini-2.5-pro", "gemini-2.5-flash"]


def _arq_modelo_vertex():
    return config.ESTADO / "vertex_modelo.txt"


def _vertex_cred():
    """Caminho do JSON de conta de serviço do Vertex AI (VERTEX_CRED ou GOOGLE_APPLICATION_CREDENTIALS)."""
    for nome in ("VERTEX_CRED", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = os.environ.get(nome, "").strip().strip('"')
        if p and os.path.isfile(p):
            return p
    return None


def _vertex(prompt, sistema):
    global MODELO_USADO
    try:
        import google.auth.transport.requests as gtr
        from google.oauth2 import service_account
    except ImportError as e:
        raise RuntimeError("para usar o Vertex instale: pip install google-auth") from e
    cred = service_account.Credentials.from_service_account_file(
        _vertex_cred(), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    cred.refresh(gtr.Request())
    projeto = os.environ.get("VERTEX_PROJECT") or cred.project_id
    local = os.environ.get("VERTEX_LOCATION", "global")
    host = "aiplatform.googleapis.com" if local == "global" else f"{local}-aiplatform.googleapis.com"
    arq = _arq_modelo_vertex()
    lembrado = arq.read_text().strip() if arq.exists() else ""
    modelos = [os.environ["VERTEX_MODEL"]] if os.environ.get("VERTEX_MODEL") else \
        ([lembrado] if lembrado else []) + [m for m in VERTEX_MODELOS if m != lembrado]
    ultimo = None
    for modelo in modelos:
        url = f"https://{host}/v1/projects/{projeto}/locations/{local}/publishers/google/models/{modelo}:generateContent"
        r = requests.post(url, timeout=TIMEOUT, headers={"Authorization": f"Bearer {cred.token}"}, json={
            "systemInstruction": {"parts": [{"text": sistema}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}})
        if r.ok:
            if modelo != lembrado:
                arq.write_text(modelo)
            MODELO_USADO = modelo
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        ultimo = r
        if r.status_code not in (404, 400):     # 404/400 = modelo não existe nesse local; tenta o próximo
            break
    ultimo.raise_for_status()
    raise RuntimeError("nenhum modelo do Vertex respondeu")


def _openai(prompt, sistema):
    global MODELO_USADO
    modelo = os.environ.get("OPENAI_MODEL", OPENAI_PADRAO)
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, json={
                          "model": modelo,
                          "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    MODELO_USADO = modelo
    return r.json()["choices"][0]["message"]["content"]


def provedores():
    """[(nome, função)] dos provedores com chave configurada; `IA=` no .env escolhe o primeiro."""
    p = []
    if _vertex_cred():
        p.append(("vertex", _vertex))
    if os.environ.get("ANTHROPIC_API_KEY"):
        p.append(("anthropic", _anthropic))
    if os.environ.get("GEMINI_API_KEY"):
        p.append(("gemini", _gemini))
    if os.environ.get("OPENAI_API_KEY"):
        p.append(("openai", _openai))
    preferida = NOMES_IA.get(os.environ.get("IA", "").strip().lower())
    if preferida:
        p.sort(key=lambda par: par[0] != preferida)
    return p


def extrair_json(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    ini = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0], default=0)
    fim = max(txt.rfind("}"), txt.rfind("]"))
    return json.loads(txt[ini:fim + 1])


def pedir_json(prompt, sistema):
    ps = provedores()
    if not ps:
        raise RuntimeError("Nenhuma chave de IA configurada. Coloque no .env GEMINI_API_KEY, ANTHROPIC_API_KEY, "
                           "OPENAI_API_KEY ou VERTEX_CRED (veja o .env.exemplo).")
    erros = []
    for nome, f in ps:
        for tentativa in range(2):
            try:
                log(f"Consultando IA ({nome})…", "llm")
                return extrair_json(f(prompt, sistema))
            except requests.HTTPError as e:
                erros.append(f"{nome}: {_descrever_erro(e)}")
                if e.response is not None and e.response.status_code in (429, 500, 502, 503, 529) and tentativa == 0:
                    time.sleep(ESPERA_SOBRECARGA_SEG)    # sobrecarga momentânea: espera e tenta de novo
                    continue
                break
            except Exception as e:  # noqa: BLE001 - qualquer falha passa para o próximo provedor
                erros.append(f"{nome}: {_descrever_erro(e)}")
                break
    raise RuntimeError("Todas as IAs falharam:\n" + "\n".join(erros))


def testar_todos():
    """Faz uma chamada mínima em cada provedor configurado e diz qual funciona (nunca mostra chaves)."""
    ps = provedores()
    if not ps:
        print("Nenhuma IA configurada (sem chaves no .env nem nas variáveis do Windows).")
        return False
    algum = False
    for nome, f in ps:
        try:
            r = extrair_json(f('Responda só com o JSON {"ok": true}', "Responda somente JSON."))
            ok = bool(r.get("ok")) if isinstance(r, dict) else False
        except Exception as e:  # noqa: BLE001 - o teste só informa qual provedor falhou
            ok, r = False, _descrever_erro(e)[:TAM_ERRO_TESTE]
        algum |= ok
        extra = f" ({MODELO_USADO})" if ok and MODELO_USADO else ""
        print(f"{'OK    ' if ok else 'FALHOU'} {nome}{extra}" + ("" if ok else f"  -> {r}"), flush=True)
    return algum
