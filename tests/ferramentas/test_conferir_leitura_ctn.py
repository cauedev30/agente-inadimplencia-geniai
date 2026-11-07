"""A conferência de leitura do CTN real (PARADA 1): contra um servidor de mentira, sem rede.

O dado é inventado, com valores sentinela no telefone, no nome, no CPF e na chave: a saída nunca
pode conter nenhum deles.
"""

import importlib.util
import json
from datetime import date

import httpx
import pytest

from tests.integracoes.test_ctn_eficaz import (
    AGORA,
    CHAVE,
    MATRICULA,
    UNIDADE,
    URL,
    _bx,
    _lanc,
    servidor,
    tabelas_padrao,
)

from ..conftest import RAIZ

_CAMINHO = RAIZ / "ferramentas" / "ctn" / "conferir_leitura.py"
_spec = importlib.util.spec_from_file_location("ctn_conferir_leitura", _CAMINHO)
conferir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conferir)

TELEFONE_SENTINELA = "(83) 97531 8642"
NOME_SENTINELA = "Fulano Sentinela Zeta"
CPF_SENTINELA = "987.654.321-00"
PROIBIDOS = ("97531", "8642", "Sentinela", "987.654", "98765432100", CHAVE, "xx000000001")


def _tabelas():
    tabelas = tabelas_padrao()
    for tabela in tabelas.values():
        for linha in tabela:
            if "telefone" in linha:
                linha["telefone"] = TELEFONE_SENTINELA
            if "nome" in linha:
                linha["nome"] = NOME_SENTINELA
            if "cpf" in linha:
                linha["cpf"] = CPF_SENTINELA
    return tabelas


def _rodar(tratar=None, tabelas=None, **extra):
    tratar = tratar or servidor(tabelas or _tabelas())
    opcoes = {
        "url": URL,
        "unidade": UNIDADE,
        "data_ref": date(2026, 11, 6),
        "transporte": httpx.MockTransport(tratar),
        "agora": lambda: AGORA,
    }
    return conferir.executar(CHAVE, **{**opcoes, **extra})


def test_a_saida_traz_so_formatos_e_contagens_e_nenhum_dado_pessoal():
    saida = _rodar()
    for proibido in PROIBIDOS:
        assert proibido not in saida
    assert "***001" in saida
    assert "telefone E.164 com o 9 (tamanho 14" in saida
    assert "tem nome: True" in saida
    assert "meses_atraso=2, meses_ate_corte=4" in saida
    assert "historico_pagamentos(12 meses): 4 baixas; por forma: CARTAO=1, PIX=3" in saida
    assert "mensalidades_em_aberto: 2 em aberto; mes_ano no padrão AAAA/MM: 2 de 2" in saida
    assert "10^3..10^3 centavos, inteiros positivos: sim" in saida


def test_os_formatos_brutos_saem_como_molde_sem_o_valor():
    saida = _rodar()
    assert "/v1/ctn/filiados_full telefone: (99) 99999 9999=1" in saida
    assert "/v1/ctn/lancamentos mes_ano: 9999/99=" in saida
    assert "/v1/ctn/lancamentos_bx dt_baixa: 9999-99-99=" in saida
    assert "/v1/ctn/lancamentos_bx valor: float=" in saida
    assert "forma (texto bruto)" in saida and "PIX=" in saida


def test_a_paginacao_mostra_o_tipo_json_do_mais_e_as_chaves_do_corpo():
    saida = _rodar()
    assert "/v1/ctn/lancamentos_bx: `mais` é boolean; chaves do corpo: dados, mais, uso" in saida


def test_o_status_das_rotas_com_e_sem_ordem():
    saida = _rodar()
    for rota in ("lancamentos", "lancamentos_bx"):
        assert f"/v1/ctn/{rota} ordem=id: HTTP 200" in saida
        assert f"/v1/ctn/{rota} sem ordem: HTTP 400" in saida


def test_data_baixa_das_rejeitadas_conta_nulas_e_preenchidas():
    tabelas = _tabelas()
    tabelas["/v1/ctn/lancamentos"] = [
        _lanc(2, "2026/09", "rejeitada", data_baixa="2026-09-12"),
        _lanc(3, "2026/10", "rejeitada", data_baixa=None),
        _lanc(4, "2026/11", "aberta", data_baixa="2026-11-01"),
    ]
    saida = _rodar(tabelas=tabelas)
    trecho = saida.split("== 3.")[1].split("== 4.")[0]
    assert "nula=" in trecho and "preenchida=" in trecho
    assert "ausente" not in trecho


def test_a_coluna_do_de_ate_e_a_contagem_por_dia():
    tabelas = _tabelas()
    # Entra pelo `dia_relatorio` de 05/11, mas foi paga em 04/11.
    tabelas["/v1/ctn/lancamentos_bx"].append(
        _bx(
            30,
            "xx000000009",
            "PIX",
            "2026/10",
            "2026-11-04",
            33.4,
            "2026-11-05T10:00:00.000Z",
            dia="2026-11-05",
        )
    )
    saida = _rodar(tabelas=tabelas)
    coluna = saida.split("== 4.")[1].split("== 5.")[0]
    assert "2026-11-05: 2 linhas; dia_relatorio dentro=2; dt_baixa dentro=1, fora=1" in coluna
    por_dia = saida.split("== 5.")[1].split("== Formatos")[0]
    assert "2026-11-05: 2 baixas" in por_dia
    assert "2026-11-04: 0 baixas" in por_dia
    assert "2026-11-03: 0 baixas" in por_dia


def test_matricula_passada_a_mao_nao_le_o_retrato():
    chamadas = []
    saida = _rodar(servidor(_tabelas(), chamadas), matriculas=[MATRICULA])
    assert "passadas com --matricula" in saida
    assert "n/d (matrícula passada à mão)" in saida
    assert all(c.url.path != "/v1/ctn/inadimplencia" for c in chamadas)


def test_sem_matricula_pega_as_primeiras_do_retrato_com_atraso():
    tabelas = _tabelas()
    for numero in range(3, 7):
        tabelas["/v1/ctn/inadimplencia"].append(
            {
                **tabelas["/v1/ctn/inadimplencia"][0],
                "matricula": f"xx00000000{numero}",
                "meses_atraso": 0 if numero == 3 else 1,
            }
        )
    saida = _rodar(tabelas=tabelas)
    matriculas = [linha for linha in saida.splitlines() if linha.startswith("matrícula ")]
    assert matriculas == ["matrícula ***001", "matrícula ***002", "matrícula ***004"]


def test_falha_numa_consulta_nao_derruba_as_outras_nem_vaza():
    base = servidor(_tabelas())

    def tratar(requisicao):
        if requisicao.url.path == "/v1/ctn/lancamentos_bx" and "matricula" in requisicao.url.params:
            return httpx.Response(500, text=f"erro com {TELEFONE_SENTINELA}")
        return base(requisicao)

    saida = _rodar(tratar)
    assert "historico_pagamentos: falhou (o CTN respondeu 500)" in saida
    assert "mensalidades_em_aberto: falhou (o CTN respondeu 500)" in saida
    assert "consultar_cliente: telefone E.164 com o 9" in saida
    for proibido in PROIBIDOS:
        assert proibido not in saida


def test_chave_recusada_aparece_como_status_e_nunca_a_chave():
    saida = _rodar(servidor(_tabelas(), chave="outra-chave"))
    assert "falhou (o CTN respondeu 401)" in saida
    assert CHAVE not in saida


def test_o_orcamento_de_chamadas_interrompe_e_diz():
    chamadas = []
    saida = _rodar(servidor(_tabelas(), chamadas), orcamento=3)
    assert len(chamadas) == 3
    assert "INTERROMPIDO: orçamento de 3 chamadas esgotado" in saida
    assert "3 de no máximo 3" in saida


def test_o_orcamento_padrao_fica_abaixo_de_40_e_a_conferencia_cabe_nele():
    chamadas = []
    _rodar(servidor(_tabelas(), chamadas))
    assert conferir.ORCAMENTO_DE_CHAMADAS < 40
    assert len(chamadas) <= conferir.ORCAMENTO_DE_CHAMADAS


def test_a_variavel_tem_prioridade_sobre_o_arquivo(tmp_path):
    (tmp_path / "eficaz.local.json").write_text(json.dumps({"token": "do-arquivo"}))
    assert conferir.ler_chave({"AGENTE_CTN_API_KEY": "da-variavel"}, tmp_path) == "da-variavel"
    assert conferir.ler_chave({}, tmp_path) == "do-arquivo"


def test_sem_chave_o_erro_nao_leva_valor(tmp_path):
    with pytest.raises(conferir.ErroDaConferencia, match="sem chave"):
        conferir.ler_chave({}, tmp_path)
    (tmp_path / "eficaz.local.json").write_text(json.dumps({"url": "https://valor-sentinela"}))
    with pytest.raises(conferir.ErroDaConferencia) as erro:
        conferir.ler_chave({}, tmp_path)
    assert "sentinela" not in str(erro.value)
    assert "'token'" in str(erro.value)


def test_main_sem_chave_sai_com_2_e_a_mensagem_no_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTE_CTN_API_KEY", raising=False)
    assert conferir.main(["--segredos", str(tmp_path)]) == 2
    assert "sem chave" in capsys.readouterr().err


def test_main_imprime_o_relatorio(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTE_CTN_API_KEY", CHAVE)
    visto = {}

    def falso(chave, **opcoes):
        visto.update(chave=chave, **opcoes)
        return "relatório de mentira"

    monkeypatch.setattr(conferir, "executar", falso)
    argumentos = ["--matricula", "A", "--matricula", "B", "--data-ref", "2026-11-05"]
    assert conferir.main(argumentos + ["--unidade", "xx"]) == 0
    assert capsys.readouterr().out.strip() == "relatório de mentira"
    assert visto["matriculas"] == ["A", "B"]
    assert visto["data_ref"] == date(2026, 11, 5)
    assert visto["unidade"] == "xx"
    assert visto["url"] == conferir.URL_EFICAZ
