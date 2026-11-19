"""Padrões do config (rodados num processo limpo, sem as variáveis que o conftest fixa)."""
import json
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _config_limpo(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORTE_") and k not in ("MARCA", "MAX_CORTES")}
    env.update(extra)
    codigo = ("import json; from cortalive import config as c; print(json.dumps([c.CORTE_MIN_SEG, c.CORTE_ALVO_SEG, "
              "c.CORTE_MAX_SEG, c.OUT_W, c.OUT_H, c.MARCA, c.MAX_CORTES, str(c.DADOS)]))")
    r = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env, capture_output=True, text=True, check=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cortes_passam_de_1min10_para_monetizar_no_tiktok():
    minimo, alvo, maximo = _config_limpo()[:3]
    assert minimo >= 70 and minimo <= alvo <= maximo


def test_saida_e_sempre_1080x1920():
    assert _config_limpo()[3:5] == [1080, 1920]
    assert _config_limpo(RESOLUCAO="2160x3840")[3:5] == [1080, 1920], "4K é exclusivo do Pro"


def test_marca_dagua_desligada_por_padrao():
    assert _config_limpo()[5] == ""
    assert _config_limpo(MARCA="@meucanal")[5] == "@meucanal"


def test_limite_de_cortes_configuravel_e_invalido_volta_ao_padrao():
    assert _config_limpo()[6] == 10
    assert _config_limpo(MAX_CORTES="3")[6] == 3
    assert _config_limpo(MAX_CORTES="abc")[6] == 10


def test_pasta_de_dados_configuravel(tmp_path):
    assert Path(_config_limpo(CORTALIVE_DADOS=str(tmp_path))[7]) == tmp_path
    assert (tmp_path / "cache").is_dir() and (tmp_path / "estado").is_dir()


def test_env_com_hashtags_entre_aspas_e_comentario(tmp_path):
    conteudo = 'HASHTAGS_FIXAS="#cortes #shorts"\nMARCA=@meucanal # minha marca\n'
    (tmp_path / ".env").write_text(conteudo, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("CORTALIVE_SEM_ENV", "HASHTAGS_FIXAS", "MARCA")}
    codigo = "import json; from cortalive import config as c; print(json.dumps([c.HASHTAGS_FIXAS, c.MARCA]))"
    r = subprocess.run([sys.executable, "-c", codigo], cwd=tmp_path, env={**env, "PYTHONPATH": str(RAIZ)},
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["#cortes #shorts", "@meucanal"]
