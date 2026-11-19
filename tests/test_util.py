"""Testes de util: escrita atômica tolerante a colisão com leitor no Windows."""
import os

import pytest

from cortalive import util
from cortalive.util import ler_json, salvar_json


def test_salvar_json_tenta_de_novo_quando_replace_colide(tmp_path, monkeypatch):
    original = os.replace
    falhas = []

    def replace_que_colide(origem, destino):
        if len(falhas) < 2:
            falhas.append(1)
            raise PermissionError(13, "Acesso negado")
        return original(origem, destino)

    monkeypatch.setattr(util.os, "replace", replace_que_colide)
    monkeypatch.setattr(util, "ESPERA_SUBSTITUIR_SEG", 0.0)
    salvar_json(tmp_path / "a.json", {"x": 1})
    assert ler_json(tmp_path / "a.json") == {"x": 1}
    assert len(falhas) == 2
    assert list(tmp_path.glob("*.tmp")) == [], "não deixa arquivo temporário para trás"


def test_salvar_json_desiste_depois_do_limite_e_limpa_temporario(tmp_path, monkeypatch):
    def sempre_colide(origem, destino):
        raise PermissionError(13, "Acesso negado")

    monkeypatch.setattr(util.os, "replace", sempre_colide)
    monkeypatch.setattr(util, "ESPERA_SUBSTITUIR_SEG", 0.0)
    with pytest.raises(PermissionError):
        salvar_json(tmp_path / "a.json", {"x": 1})
    assert list(tmp_path.glob("*.tmp")) == []
