"""Isola os testes: dados numa pasta temporária, sem ler o .env de verdade nem usar chaves reais de IA."""
import os
import tempfile

os.environ["CORTALIVE_DADOS"] = tempfile.mkdtemp(prefix="cortalive_teste_")
os.environ["CORTALIVE_SEM_ENV"] = "1"
for _chave in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERTEX_CRED",
               "GOOGLE_APPLICATION_CREDENTIALS", "IA", "MARCA", "TEMA", "MAX_CORTES"):
    os.environ.pop(_chave, None)
# A suíte usa cortes de 25–75 s (alvo 45) para os vídeos sintéticos ficarem curtos. Os padrões de verdade
# (≥ 72 s) são conferidos em tests/test_config.py, num processo separado sem estas variáveis.
os.environ.setdefault("CORTE_MIN_SEG", "25")
os.environ.setdefault("CORTE_MAX_SEG", "75")
os.environ.setdefault("CORTE_ALVO_SEG", "45")
