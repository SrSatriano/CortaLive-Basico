"""Testes do cliente LLM: a chave nunca pode ir para URL, mensagem de erro ou log."""
import requests

from cortalive import llm

CHAVE_FALSA = "AIzaSyFAKE_SEGREDO_1234567890"


class _Resposta:
    ok = True
    status_code = 200

    def __init__(self, dados):
        self._dados = dados
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._dados


def _limpar_ambiente(monkeypatch):
    for nome in ("VERTEX_CRED", "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                 "OPENAI_API_KEY", "IA"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", CHAVE_FALSA)


def test_gemini_manda_chave_no_cabecalho_e_nao_na_url(monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    chamadas = []

    def get(url, **kw):
        chamadas.append((url, kw))
        return _Resposta({"models": [{"name": "models/gemini-2.5-flash",
                                      "supportedGenerationMethods": ["generateContent"]}]})

    def post(url, **kw):
        chamadas.append((url, kw))
        return _Resposta({"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})

    monkeypatch.setattr(llm.requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.pedir_json("p", "s") == {"ok": True}
    assert len(chamadas) == 2
    for url, kw in chamadas:
        assert CHAVE_FALSA not in url
        assert "key=" not in url
        assert kw["headers"]["x-goog-api-key"] == CHAVE_FALSA


def test_erro_de_conexao_nao_vaza_chave_na_mensagem(monkeypatch, capsys):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("GEMINI_MODEL", "g")

    def post(url, **kw):
        # o requests põe a URL inteira na mensagem de ConnectionError; simula uma URL antiga com ?key=
        raise requests.ConnectionError(f"Max retries exceeded with url: /v1beta/models/g:generateContent"
                                       f"?key={CHAVE_FALSA})")

    monkeypatch.setattr(llm.requests, "post", post)
    try:
        llm.pedir_json("p", "s")
    except RuntimeError as e:
        msg = str(e)
    else:
        raise AssertionError("devia levantar RuntimeError")
    assert CHAVE_FALSA not in msg
    assert "ConnectionError" in msg
    assert CHAVE_FALSA not in capsys.readouterr().out


def test_mascarar_segredos_remove_chaves_conhecidas_e_parametro_key(monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-segredo-openai-123")
    texto = f"url?key={CHAVE_FALSA}&x=1 erro sk-segredo-openai-123 fim"
    limpo = llm.mascarar_segredos(texto)
    assert CHAVE_FALSA not in limpo
    assert "sk-segredo-openai-123" not in limpo
    assert "fim" in limpo
    assert "?key=***&x=1" in limpo


def test_testar_todos_nao_imprime_chave(monkeypatch, capsys):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("GEMINI_MODEL", "g")

    def post(url, **kw):
        raise requests.ConnectionError(f"falhou url?key={CHAVE_FALSA}")

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.testar_todos() is False
    assert CHAVE_FALSA not in capsys.readouterr().out


def test_ia_preferida_vem_primeiro(monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste-claude")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-teste-openai")
    assert [n for n, _ in llm.provedores()] == ["anthropic", "gemini", "openai"]
    monkeypatch.setenv("IA", "openai")
    assert [n for n, _ in llm.provedores()][0] == "openai"
    monkeypatch.setenv("IA", "Claude")
    assert [n for n, _ in llm.provedores()][0] == "anthropic"


def test_sem_chave_nenhuma_explica_o_que_fazer(monkeypatch):
    _limpar_ambiente(monkeypatch)
    monkeypatch.delenv("GEMINI_API_KEY")
    assert llm.provedores() == []
    try:
        llm.pedir_json("p", "s")
    except RuntimeError as e:
        assert ".env" in str(e) and "GEMINI_API_KEY" in str(e)
    else:
        raise AssertionError("devia levantar RuntimeError")
