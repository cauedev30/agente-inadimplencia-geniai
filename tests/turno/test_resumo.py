"""O resumo dos turnos (`agente resumo-turnos`): só contagens, nenhum texto nem telefone."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from alembic import command

from agente.cli import main
from agente.db.modelos import Cliente, Cobranca, Conversa, Turno
from agente.db.sessao import criar_sessao
from agente.politica import Regua
from agente.tipos import Modo, ResultadoTurno
from agente.turno.resumo import resumo_dos_turnos

from ..conftest import alembic_config, escrever_politica_de_teste

JP = ZoneInfo("America/Fortaleza")
DIA = datetime(2026, 11, 17, 10, tzinfo=JP)
TEXTO_SECRETO = "texto secreto do cliente"
TELEFONE = "+5583977776666"
PIX = {"forma": "PIX", "br_code": "000201"}


def _conversa(sessao, cobranca=None):
    if cobranca is None:
        cliente = Cliente(matricula_ctn="M-9", nome="Fulana Sigilosa", telefone=TELEFONE)
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="teste",
        )
    conversa = Conversa(cobranca=cobranca)
    sessao.add(conversa)
    sessao.flush()
    return conversa


def _turno(
    sessao,
    conversa,
    resultado,
    rotulo=None,
    redacao=None,
    numeros=None,
    texto=None,
    quando=DIA,
):
    sessao.add(
        Turno(
            conversa_id=conversa.id,
            modo=Modo.ATIVO,
            acao_principal=rotulo,
            numeros=numeros or {},
            texto_enviado=texto,
            redacao=redacao or {},
            resultado=resultado,
            fechado_em=quando,
        )
    )
    sessao.flush()


def _llm(sessao, conversa, rotulo="INFORM_DEBT", numeros=None, tentativas=1, quando=DIA):
    redacao = {"saida": "LLM", "tentativas": [{"reprovadas": []}] * tentativas}
    _turno(
        sessao,
        conversa,
        ResultadoTurno.ENVIADO,
        rotulo,
        redacao,
        numeros,
        TEXTO_SECRETO,
        quando,
    )


def _atendente(sessao, conversa, motivo, passagem=None, rotulo=None):
    redacao = {"saida": "HANDOFF"}
    if passagem is not None:
        redacao["passagem"] = passagem
    _turno(sessao, conversa, ResultadoTurno.HANDOFF, rotulo or f"HUMAN:{motivo}", redacao)


@pytest.fixture
def varios_turnos(sessao):
    """Quatro conversas: duas pagam, uma vai ao atendente, uma bate no teto."""
    a = _conversa(sessao)
    b, c, d = (_conversa(sessao, a.cobranca) for _ in range(3))
    # a: o meio já na 1ª resposta, depois uma dúvida e a fixa do Pix.
    _llm(sessao, a, numeros=PIX)
    _llm(sessao, a, "ANSWER")
    # b: duas respostas sem o meio, a 3ª leva o Pix, com uma 2ª tentativa.
    _llm(sessao, b)
    _llm(sessao, b, "ANSWER")
    _llm(sessao, b, "GENERATE_PIX", PIX, tentativas=2)
    # c: a frase fixa do botão e a passagem do Luna ao atendente.
    _turno(sessao, c, ResultadoTurno.ENVIADO, "GENERATE_PIX", {"saida": "FIXA"}, PIX, TEXTO_SECRETO)
    _atendente(sessao, c, "CONCILIACAO", "LLM")
    # d: a fixa do fora do assunto, a passagem fixa do jurídico, a de palavra-chave sem leitura, a
    # frase fixa do teto, o silêncio e o texto reprovado 2 vezes.
    _turno(
        sessao,
        d,
        ResultadoTurno.ENVIADO,
        "OUT_OF_SCOPE",
        {"saida": "FRASE_FIXA", "tentativas": [{}, {}]},
        None,
        TEXTO_SECRETO,
    )
    _atendente(sessao, d, "JURIDICO", "FIXA")
    _atendente(sessao, d, "TETO", rotulo="HUMAN:TETO")
    _turno(sessao, d, ResultadoTurno.FIXA, "FIXA:CANAL_EXTERNO")
    _turno(sessao, d, ResultadoTurno.SEM_ENVIO, "NONE", {"saida": "SEM_ENVIO"})
    _turno(
        sessao,
        d,
        ResultadoTurno.HANDOFF,
        "NAO_EXECUTADA:INFORM_DEBT",
        {"saida": "HANDOFF", "tentativas": [{}, {}]},
    )
    sessao.commit()
    return sessao


def _linha(texto, *trechos):
    """A linha do relatório que tem todos os trechos."""
    achadas = [linha for linha in texto.splitlines() if all(t in linha for t in trechos)]
    assert len(achadas) == 1, (trechos, texto)
    return achadas[0]


def test_conta_os_turnos_por_saida(varios_turnos, politica):
    texto = resumo_dos_turnos(varios_turnos, politica)

    assert "Turnos contados: 13" in texto
    assert "texto do Luna: 5" in texto
    assert "mensagem fixa: 3" in texto
    assert "atendente: 4" in texto
    assert "silêncio: 1" in texto
    assert "outros: 0" in texto


def test_conta_o_atendente_por_motivo_e_por_passagem(varios_turnos, politica):
    texto = resumo_dos_turnos(varios_turnos, politica)

    assert "CONCILIACAO: 1" in texto
    assert "JURIDICO: 1" in texto
    assert "TETO: 1" in texto
    assert "VALIDACAO: 1" in texto
    assert "passagem redigida pelo Luna: 1" in texto
    assert "passagem com a frase fixa: 1" in texto
    assert "sem passagem do Luna (gate, palavra-chave ou texto reprovado): 2" in texto


def test_conta_as_reprovacoes(varios_turnos, politica):
    texto = resumo_dos_turnos(varios_turnos, politica)

    assert "turnos com 2ª tentativa: 3" in texto
    assert "saídas ao atendente por VALIDACAO: 1" in texto


def test_conta_o_pagamento_por_conversa(varios_turnos, politica):
    texto = resumo_dos_turnos(varios_turnos, politica)

    assert "conversas com resposta do agente: 4" in texto
    assert "meio de pagamento na 1ª resposta: 2 de 4" in texto
    assert "conversas que receberam o meio: 3 de 4" in texto
    # a: 1 resposta, b: 3, c: 1.
    assert "respostas até o 1º meio, em média: 1,7" in texto
    assert "conversas que bateram no teto: 1" in texto


def test_fora_da_conta_ficam_os_turnos_sem_resultado_adiados_erros_e_comandos(sessao, politica):
    conversa = _conversa(sessao)
    _llm(sessao, conversa)
    _turno(sessao, conversa, None)
    _turno(sessao, conversa, ResultadoTurno.ADIADO)
    _turno(sessao, conversa, ResultadoTurno.ERRO)
    _turno(sessao, conversa, ResultadoTurno.FIXA, "TESTE:ABERTURA", texto="[TESTE] abertura")
    _turno(sessao, conversa, ResultadoTurno.IGNORADO)
    sessao.commit()

    texto = resumo_dos_turnos(sessao, politica)

    assert "Turnos contados: 2" in texto
    assert "texto do Luna: 1" in texto
    assert "outros: 1 (IGNORADO: 1)" in texto
    assert "sem resultado: 1" in texto
    assert "adiados: 1" in texto
    assert "erros: 1" in texto
    assert "comandos de teste: 1" in texto
    assert "conversas com resposta do agente: 1" in texto


def test_o_periodo_usa_o_fechado_em_no_fuso_da_janela(sessao, politica):
    conversa = _conversa(sessao)
    # 23h30 do dia 16 em Fortaleza já é dia 17 em UTC: o fuso da janela é que vale.
    for quando in (datetime(2026, 11, 16, 23, 30, tzinfo=JP), DIA, DIA + timedelta(days=2)):
        _llm(sessao, conversa, quando=quando)
    sessao.commit()

    tudo = resumo_dos_turnos(sessao, politica)
    desde = resumo_dos_turnos(sessao, politica, desde=date(2026, 11, 17))
    ate = resumo_dos_turnos(sessao, politica, ate=date(2026, 11, 17))
    janela = resumo_dos_turnos(sessao, politica, desde=date(2026, 11, 17), ate=date(2026, 11, 17))

    assert "Turnos contados: 3" in tudo
    assert "Turnos contados: 2" in desde and "(desde 2026-11-17)" in desde
    assert "Turnos contados: 2" in ate and "(até 2026-11-17)" in ate
    assert "Turnos contados: 1" in janela and "(de 2026-11-17 até 2026-11-17)" in janela


def test_periodo_sem_turnos_diz_que_nao_ha(sessao, politica, conversa):
    texto = resumo_dos_turnos(sessao, politica, desde=date(2026, 11, 17))

    assert "Nenhum turno" in texto
    assert "texto do Luna" not in texto


def test_a_saida_nao_tem_texto_nem_telefone(varios_turnos, politica):
    texto = resumo_dos_turnos(varios_turnos, politica)

    assert TEXTO_SECRETO not in texto
    assert "Fulana" not in texto and "Sigilosa" not in texto
    assert TELEFONE not in texto and "77776666" not in texto
    assert "M-9" not in texto


# ---- agente resumo-turnos ----


@pytest.fixture
def banco(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'resumo.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(escrever_politica_de_teste(tmp_path)))
    with criar_sessao(url)() as sessao:
        conversa = _conversa(sessao)
        _llm(sessao, conversa, numeros=PIX)
        _atendente(sessao, conversa, "RETENCAO", "LLM")
        sessao.commit()
    return url


def test_o_comando_imprime_o_resumo(banco, capsys):
    codigo = main(["resumo-turnos"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "texto do Luna: 1" in saida and "RETENCAO: 1" in saida
    assert TEXTO_SECRETO not in saida
    assert "Fulana" not in saida and TELEFONE not in saida and "77776666" not in saida


def test_o_comando_grava_no_arquivo_e_filtra_pela_data(banco, tmp_path, capsys):
    arquivo = tmp_path / "pasta" / "resumo.md"

    codigo = main(
        ["resumo-turnos", "--desde", "2026-11-17", "--ate", "2026-11-17", "--saida", str(arquivo)]
    )

    assert codigo == 0
    texto = arquivo.read_text(encoding="utf-8")
    assert "Turnos contados: 2" in texto and "de 2026-11-17 até 2026-11-17" in texto
    assert "texto do Luna" not in capsys.readouterr().out


def test_o_comando_com_periodo_vazio_diz_que_nao_ha(banco, capsys):
    codigo = main(["resumo-turnos", "--desde", "2030-01-01"])

    assert codigo == 0
    assert "Nenhum turno" in capsys.readouterr().out
