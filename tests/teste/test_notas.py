"""O `/nota` e o relatório das notas (D56, tarefa 2.3): a nota fica presa ao turno que o testador
acabou de ler, não vai ao LLM nem conta no teto, e o relatório junta tudo com o telefone
mascarado."""

from datetime import date

import pytest
from alembic import command
from sqlalchemy import select

from agente.cli import main
from agente.db.historico import mensagens_no_teto
from agente.db.modelos import Cliente, Cobranca, Conversa, Mensagem, NotaTeste, Turno
from agente.db.sessao import criar_sessao
from agente.integracoes.disparador import FalhaNoEnvio
from agente.politica import Regua
from agente.teste.notas import gravar_nota, relatorio_das_notas
from agente.tipos import OrigemMensagem, ResultadoTurno
from tests.conftest import TESTADOR_1, alembic_config
from tests.teste.auxiliares import CHATWOOT_ID, Mandante
from tests.turno.test_orquestrador_gates import SEGUNDA_10H

NOTA_ANOTADA = "[TESTE] Nota anotada."
NOTA_VAZIA = "[TESTE] Escreva a nota depois do /nota."


def _conversa_do_chatwoot(sessao):
    return sessao.scalars(
        select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
    ).one()


def _notas(sessao):
    return list(sessao.scalars(select(NotaTeste).order_by(NotaTeste.id)))


def _turnos(sessao):
    return list(sessao.scalars(select(Turno).order_by(Turno.id)))


@pytest.fixture
def conversa_aberta(sessao, testador, deps):
    """O caso 1, depois do 1º contato e de uma conversa de verdade (o Luna respondeu)."""
    testador.manda("Oi")
    testador.roda(deps)
    testador.manda("Quanto eu devo?")
    testador.roda(deps)
    return testador


def test_a_nota_fica_presa_ao_ultimo_turno_que_nao_e_de_comando(
    sessao, conversa_aberta, conversa_de_teste, deps
):
    turno_do_llm = _turnos(sessao)[-1]
    assert turno_do_llm.acao_principal is not None
    assert not turno_do_llm.acao_principal.startswith("TESTE:")
    conversa_aberta.manda("/nota a resposta saiu seca demais")

    assert conversa_aberta.roda(deps) is ResultadoTurno.FIXA

    [nota] = _notas(sessao)
    assert nota.turno_id == turno_do_llm.id
    assert nota.conversa_id == conversa_de_teste.id
    assert nota.cobranca_id == conversa_de_teste.cobranca_id
    assert nota.caso == 1
    assert nota.texto == "a resposta saiu seca demais"
    comando = sessao.get(Mensagem, nota.mensagem_id)
    assert comando.texto == "/nota a resposta saiu seca demais"
    assert comando.origem is OrigemMensagem.TESTE
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == NOTA_ANOTADA
    ultimo = _turnos(sessao)[-1]
    assert (ultimo.acao_principal, ultimo.resultado) == ("TESTE:NOTA", ResultadoTurno.FIXA)


def test_a_nota_logo_depois_da_abertura_nao_tem_turno(sessao, testador, conversa_de_teste, deps):
    testador.manda("Oi")
    testador.roda(deps)  # a abertura (TESTE:ABERTURA)
    testador.manda("/nota o texto da abertura está estranho")

    testador.roda(deps)

    [nota] = _notas(sessao)
    assert nota.turno_id is None


def test_a_nota_sem_texto_pede_o_texto_e_nao_grava(sessao, conversa_aberta, deps):
    conversa_aberta.manda("/nota")

    conversa_aberta.roda(deps)

    assert _notas(sessao) == []
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == NOTA_VAZIA


def test_a_nota_nao_vai_ao_llm_nem_conta_no_teto(sessao, conversa_aberta, conversa_de_teste, deps):
    chamadas_do_jev = len(deps.jev.chamadas)
    pedidos = len(deps.redator.pedidos)
    no_teto = mensagens_no_teto(sessao, conversa_de_teste.id)
    conversa_aberta.manda("/nota anotando")

    conversa_aberta.roda(deps)

    assert len(deps.jev.chamadas) == chamadas_do_jev
    assert len(deps.redator.pedidos) == pedidos
    assert mensagens_no_teto(sessao, conversa_de_teste.id) == no_teto
    [nota] = _notas(sessao)
    assert sessao.get(Mensagem, nota.mensagem_id).conta_no_teto is False


def test_a_nota_e_desfeita_com_o_turno_se_a_resposta_nao_sai(
    sessao, conversa_aberta, deps, monkeypatch
):
    def falha(*_, **__):
        raise FalhaNoEnvio("chatwoot fora")

    monkeypatch.setattr(deps.chatwoot, "enviar_mensagem", falha)
    conversa_aberta.manda("/nota vai falhar")

    conversa_aberta.roda(deps)  # o orquestrador desfaz o turno e agenda a nova tentativa

    assert _notas(sessao) == []


def _duas_notas(sessao, testador, deps):
    """Uma nota no caso 1 e outra no caso 3 (conversa nova do Chatwoot)."""
    testador.manda("Oi")
    testador.roda(deps)
    testador.manda("Quanto eu devo?")
    testador.roda(deps)
    testador.manda("/nota caso 1: valor certo")
    testador.roda(deps)
    testador.manda("/caso 3")
    testador.roda(deps)
    nova = Mandante(sessao, _conversa_do_chatwoot(sessao))
    nova.n = testador.n
    nova.manda("Oi")
    nova.roda(deps)
    nova.manda("/nota caso 3: abertura\ncom duas linhas")
    nova.roda(deps)


def test_o_relatorio_junta_as_notas_dos_dois_casos(sessao, testador, deps, politica_de_teste):
    _duas_notas(sessao, testador, deps)

    relatorio = relatorio_das_notas(sessao, politica_de_teste, None)

    assert len(_notas(sessao)) == 2
    assert relatorio.startswith("# Notas do teste")
    assert relatorio.index("caso 1: valor certo") < relatorio.index("caso 3: abertura")
    assert "caso 1, testador ***7777" in relatorio
    assert "caso 3, testador ***7777" in relatorio
    # A data e a hora no fuso da janela (10h em Fortaleza mais os minutos da conversa).
    assert "26/10/2026 10:" in relatorio
    assert "> caso 3: abertura\n> com duas linhas" in relatorio
    # O turno e as mensagens da conversa (sem as do modo teste).
    assert "resultado" in relatorio
    assert "- cliente: Quanto eu devo?" in relatorio
    assert "- abertura: Oi, Ana!" in relatorio
    assert "Nota anotada" not in relatorio and "Agora você é o caso" not in relatorio
    assert "- teste:" not in relatorio


def test_o_telefone_nunca_aparece_inteiro_no_relatorio(sessao, testador, deps, politica_de_teste):
    _duas_notas(sessao, testador, deps)
    nota = _notas(sessao)[0]
    nota.texto = f"o cliente {TESTADOR_1} pediu"
    sessao.commit()

    relatorio = relatorio_das_notas(sessao, politica_de_teste, None)

    for pedaco in (TESTADOR_1, TESTADOR_1[3:], "988887777", "88887777"):
        assert pedaco not in relatorio
    assert "***7777" in relatorio


def test_o_relatorio_filtra_pela_data(sessao, testador, deps, politica_de_teste):
    _duas_notas(sessao, testador, deps)

    do_dia = relatorio_das_notas(sessao, politica_de_teste, date(2026, 10, 26))
    depois = relatorio_das_notas(sessao, politica_de_teste, date(2026, 10, 27))

    assert "(desde 2026-10-26)" in do_dia and "2 nota(s)" in do_dia
    assert "Nenhuma nota" in depois


def test_gravar_nota_direto_tem_turno_nulo_sem_turnos(sessao, conversa_de_teste):
    comando = Mensagem(
        conversa_id=conversa_de_teste.id,
        origem=OrigemMensagem.TESTE,
        texto="/nota x",
        conta_no_teto=False,
        momento=SEGUNDA_10H,
    )
    sessao.add(comando)
    sessao.flush()

    nota = gravar_nota(sessao, conversa_de_teste, comando, "x", SEGUNDA_10H)

    assert nota.turno_id is None and nota.caso == 1


# ---- agente notas-teste ----


@pytest.fixture
def banco_com_uma_nota(tmp_path, monkeypatch, politica_de_teste):
    """Um banco de arquivo migrado até o head, com uma nota de um testador, e o comando apontado
    para ele e para a política de teste."""
    url = f"sqlite:///{(tmp_path / 'notas.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)
    monkeypatch.setenv("AGENTE_POLITICA_PATH", str(tmp_path / "politica.toml"))
    with criar_sessao(url)() as sessao:
        cliente = Cliente(matricula_ctn="SIM-1-7777", nome="Ana", telefone=TESTADOR_1)
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="teste",
        )
        conversa = Conversa(cobranca=cobranca, chatwoot_conversa_id=1)
        sessao.add(conversa)
        sessao.flush()
        comando = Mensagem(
            conversa_id=conversa.id,
            origem=OrigemMensagem.TESTE,
            texto="/nota pelo cli",
            conta_no_teto=False,
            momento=SEGUNDA_10H,
        )
        sessao.add(comando)
        sessao.flush()
        gravar_nota(sessao, conversa, comando, "pelo cli", SEGUNDA_10H)
        sessao.commit()
    return url


def test_o_comando_imprime_o_relatorio(banco_com_uma_nota, capsys):
    codigo = main(["notas-teste"])

    saida = capsys.readouterr().out
    assert codigo == 0
    assert "pelo cli" in saida and "***7777" in saida
    assert TESTADOR_1 not in saida


def test_o_comando_grava_no_arquivo_e_filtra_pela_data(banco_com_uma_nota, tmp_path, capsys):
    arquivo = tmp_path / "notas.md"

    codigo = main(["notas-teste", "--desde", "2026-10-26", "--saida", str(arquivo)])

    assert codigo == 0
    texto = arquivo.read_text(encoding="utf-8")
    assert "pelo cli" in texto and "(desde 2026-10-26)" in texto
    assert "pelo cli" not in capsys.readouterr().out
