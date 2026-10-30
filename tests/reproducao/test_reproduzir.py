"""A reprodução da pré-sombra (tarefa 10.3): as conversas do Chatwoot pelo turno em modo sombra."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from agente.config import Config
from agente.db.modelos import Cobranca, Conversa, Mensagem, Turno
from agente.db.sessao import criar_sessao
from agente.integracoes.jev import JevFalso, responder
from agente.reproducao.chatwoot_leitura import ChatwootLeitura
from agente.reproducao.reproduzir import RelatorioReproducao, reproduzir
from agente.tipos import Modo, OrigemMensagem

from .amostra import ler_amostra
from .fabrica import (
    CONTA,
    INBOX,
    SEGUNDA_10H,
    TOKEN,
    ChatwootSimulado,
    anexo,
    conversa,
    mensagem,
)

FIXTURES = Path(__file__).parent / "fixtures"
CARTEIRA = FIXTURES / "carteira.csv"
DESDE = datetime.fromtimestamp(SEGUNDA_10H - 3 * 86400, UTC)
T = SEGUNDA_10H
TELEFONE_UM = "+5583999990001"
TELEFONE_DOIS = "+5583999990002"
FORA_DA_CARTEIRA = "+5583999990077"
# Domingo, 22h em João Pessoa: fora da janela de atendimento. Abre na segunda, às 8h (11h UTC).
DOMINGO_22H = int(datetime(2026, 10, 26, 1, tzinfo=UTC).timestamp())
SEGUNDA_8H = datetime(2026, 10, 26, 11, tzinfo=UTC)


@pytest.fixture
def config():
    return Config(database_url="sqlite://", chatwoot_conta=CONTA, chatwoot_inbox=INBOX)


@pytest.fixture
def banco(tmp_path):
    return tmp_path / "reproducao.db"


def _exemplo() -> tuple[dict, list[dict]]:
    dados = json.loads((FIXTURES / "conversa_exemplo.json").read_text(encoding="utf-8"))
    return dados["conversa"], dados["mensagens"]


def _reproduzir(servidor, banco, politica, config, jev=None) -> RelatorioReproducao:
    with ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, TOKEN, transporte=servidor.transporte
    ) as leitura:
        return reproduzir(leitura, CARTEIRA, DESDE, banco, politica, config, jev)


def _turnos(banco) -> list[Turno]:
    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        return list(sessao.scalars(select(Turno).order_by(Turno.id)))


def _servidor_do_exemplo() -> ChatwootSimulado:
    bruta, mensagens = _exemplo()
    return ChatwootSimulado([bruta], {bruta["id"]: mensagens}, pagina=4)


def test_uma_conversa_de_exemplo_do_comeco_ao_fim(banco, politica, config):
    servidor = _servidor_do_exemplo()

    relatorio = _reproduzir(servidor, banco, politica, config)

    assert relatorio.carteira_criadas == 2
    assert relatorio.carteira_rejeitadas == 0
    assert (relatorio.conversas_lidas, relatorio.conversas_cruzadas) == (1, 1)
    # A atividade não é mensagem; o template, as do cliente e a do atendente são eventos.
    assert (relatorio.mensagens, relatorio.mensagens_ignoradas) == (5, 1)
    # O webhook não trata o template (o 1 ignorado).
    assert relatorio.eventos == {"ignorado": 1, "agendado": 3, "humano": 1}
    # A rajada das duas primeiras mensagens vira um turno; a última, outro, já com o humano.
    assert relatorio.turnos_por_resultado == {"SOMBRA": 2}
    assert relatorio.turnos_por_acao == {"INFORM_DEBT": 2}
    assert relatorio.turnos_por_gatilho == {}
    assert relatorio.adiados == 0
    assert relatorio.erros_por_tipo == {}
    turnos = _turnos(banco)
    assert [t.modo for t in turnos] == [Modo.SOMBRA, Modo.SOMBRA]
    assert [len(t.mensagens_ids) for t in turnos] == [2, 1]
    assert [t.humano_na_conversa for t in turnos] == [False, True]
    # O turno roda no instante em que a rajada vence, não no fim da reprodução.
    assert turnos[0].fechado_em == datetime.fromtimestamp(T + 3 + 7, UTC)
    assert turnos[1].fechado_em == datetime.fromtimestamp(T + 900 + 7, UTC)


def test_o_relatorio_bate_com_os_turnos_gravados(banco, politica, config):
    relatorio = _reproduzir(_servidor_do_exemplo(), banco, politica, config)

    turnos = _turnos(banco)
    assert sum(relatorio.turnos_por_resultado.values()) == len(turnos)
    assert sum(relatorio.turnos_por_acao.values()) == len(turnos)
    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        assert sessao.scalar(select(func.count()).select_from(Cobranca)) == 2
        [conversa_gravada] = sessao.scalars(select(Conversa).where(Conversa.chatwoot_conversa_id))
        assert conversa_gravada.chatwoot_conversa_id == 901
        assert conversa_gravada.human_owned is True
        origens = [m.origem for m in sessao.scalars(select(Mensagem).order_by(Mensagem.id))]
    assert origens == [
        OrigemMensagem.CLIENTE,
        OrigemMensagem.CLIENTE,
        OrigemMensagem.HUMANO,
        OrigemMensagem.CLIENTE,
    ]


def test_o_chatwoot_falso_nao_recebe_nada_e_so_sai_get(banco, politica, config):
    servidor = _servidor_do_exemplo()

    relatorio = _reproduzir(servidor, banco, politica, config)

    assert relatorio.chamadas_ao_chatwoot == 0
    assert relatorio.templates_enviados == 0
    assert {pedido.method for pedido in servidor.pedidos} == {"GET"}


def test_telefone_fora_da_carteira_nao_cruza_e_nao_gera_turno(banco, politica, config):
    bruta = conversa(902, T + 60, telefone=FORA_DA_CARTEIRA)
    mensagens = [mensagem(9101, 902, 0, "oi", T)]
    servidor = ChatwootSimulado([bruta], {902: mensagens})

    relatorio = _reproduzir(servidor, banco, politica, config)

    assert (relatorio.conversas_lidas, relatorio.conversas_cruzadas) == (1, 0)
    assert relatorio.eventos == {"sem_cobranca": 1}
    assert relatorio.turnos_por_resultado == {}
    assert _turnos(banco) == []


def test_conversa_sem_telefone_conta_como_lida_e_nao_cruzada(banco, politica, config):
    servidor = ChatwootSimulado(
        [conversa(903, T + 60, telefone=None)], {903: [mensagem(9201, 903, 0, "oi", T)]}
    )

    relatorio = _reproduzir(servidor, banco, politica, config)

    assert (relatorio.conversas_lidas, relatorio.conversas_cruzadas) == (1, 0)
    assert relatorio.conversas_sem_telefone == 1
    assert relatorio.eventos == {"sem_cobranca": 1}


def test_o_atendente_na_conversa_fica_registrado_no_turno_seguinte(banco, politica, config):
    bruta = conversa(904, T + 200)
    mensagens = [
        mensagem(9301, 904, 0, "quanto eu devo?", T),
        mensagem(9302, 904, 1, "Já te ajudo", T + 60),
        mensagem(9303, 904, 0, "tá bom", T + 120),
    ]

    relatorio = _reproduzir(ChatwootSimulado([bruta], {904: mensagens}), banco, politica, config)

    assert relatorio.eventos == {"agendado": 2, "humano": 1}
    assert [t.humano_na_conversa for t in _turnos(banco)] == [False, True]


def test_mensagem_fora_da_janela_e_adiada_e_roda_na_abertura(banco, politica, config):
    bruta = conversa(905, DOMINGO_22H + 60)
    mensagens = [mensagem(9401, 905, 0, "quanto eu devo?", DOMINGO_22H)]

    relatorio = _reproduzir(ChatwootSimulado([bruta], {905: mensagens}), banco, politica, config)

    assert relatorio.adiados == 1
    assert relatorio.turnos_por_resultado == {"SOMBRA": 1}
    [turno] = _turnos(banco)
    assert turno.fechado_em == SEGUNDA_8H


def test_junta_as_conversas_em_ordem_de_tempo(banco, politica, config):
    # As duas conversas se alternam no tempo: a ordem de gravação segue o created_at.
    primeira = conversa(906, T + 100, telefone=TELEFONE_UM)
    segunda = conversa(907, T + 200, telefone=TELEFONE_DOIS)
    servidor = ChatwootSimulado(
        [primeira, segunda],
        {
            906: [mensagem(9501, 906, 0, "a", T + 10), mensagem(9503, 906, 0, "c", T + 100)],
            907: [mensagem(9502, 907, 0, "b", T + 50), mensagem(9504, 907, 0, "d", T + 200)],
        },
    )

    relatorio = _reproduzir(servidor, banco, politica, config)

    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        gravadas = sessao.scalars(select(Mensagem).order_by(Mensagem.id)).all()
    assert [m.texto for m in gravadas] == ["a", "b", "c", "d"]
    assert relatorio.conversas_cruzadas == 2
    assert relatorio.turnos_por_resultado == {"SOMBRA": 4}


def test_usa_o_jev_recebido_e_conta_o_gatilho(banco, politica, config):
    jev = JevFalso([responder("quer_retencao", True, 0.92)])
    bruta = conversa(908, T + 60)
    mensagens = [mensagem(9601, 908, 0, "quero cancelar o cartão", T)]

    relatorio = _reproduzir(
        ChatwootSimulado([bruta], {908: mensagens}), banco, politica, config, jev
    )

    assert relatorio.turnos_por_gatilho == {"RETENCAO": 1}
    assert relatorio.turnos_por_acao == {}
    assert relatorio.turnos_por_resultado == {"SOMBRA": 1}


def test_o_erro_do_turno_entra_no_relatorio_pelo_tipo(banco, politica, config):
    jev = JevFalso(falha=RuntimeError("segredo que não pode ir ao relatório"))
    bruta = conversa(909, T + 60)
    mensagens = [mensagem(9701, 909, 0, "oi", T)]

    relatorio = _reproduzir(
        ChatwootSimulado([bruta], {909: mensagens}), banco, politica, config, jev
    )

    # Duas tentativas falham com o erro; a 3ª esgota o teto e o turno sai da fila (ERRO_INTERNO).
    assert relatorio.erros_por_tipo == {"RuntimeError": 2, "ERRO_INTERNO": 1}
    assert "segredo" not in repr(relatorio)


def test_o_log_da_reproducao_nao_leva_traceback_nem_a_mensagem_da_excecao(
    banco, politica, config, caplog
):
    sensivel = "trecho de conversa do cliente que não pode ir ao log"
    jev = JevFalso(falha=RuntimeError(sensivel))
    mensagens = [mensagem(9711, 911, 0, "oi", T)]
    caplog.set_level(logging.DEBUG)

    _reproduzir(
        ChatwootSimulado([conversa(911, T + 60)], {911: mensagens}), banco, politica, config, jev
    )

    # O erro foi registrado (o tipo fica no log), mas sem o stack trace nem a mensagem.
    erros = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert erros
    assert all("RuntimeError" in r.getMessage() for r in erros if "falhou" in r.getMessage())
    assert sensivel not in caplog.text
    for registro in caplog.records:
        assert sensivel not in registro.getMessage()
        assert not registro.exc_info
        assert not registro.exc_text
        assert not registro.stack_info
        assert "Traceback" not in registro.getMessage()
    assert "Traceback" not in caplog.text


def test_depois_da_reproducao_o_log_volta_a_ter_o_traceback(banco, politica, config, caplog):
    fabrica_antes = logging.getLogRecordFactory()
    jev = JevFalso(falha=RuntimeError("falha de teste"))
    mensagens = [mensagem(9712, 912, 0, "oi", T)]
    _reproduzir(
        ChatwootSimulado([conversa(912, T + 60)], {912: mensagens}), banco, politica, config, jev
    )

    # A fábrica de registros é a de antes: em produção o log não muda.
    assert logging.getLogRecordFactory() is fabrica_antes
    try:
        raise ValueError("outra falha")
    except ValueError:
        logging.getLogger("teste").exception("com traceback")
    registro = caplog.records[-1]
    assert registro.exc_info is not None
    assert "outra falha" in caplog.text


def test_o_audio_sem_transcritor_nao_quebra_a_reproducao(banco, politica, config):
    bruta = conversa(910, T + 60)
    mensagens = [mensagem(9801, 910, 0, "", T, anexos=[anexo("audio", 9801)])]

    relatorio = _reproduzir(ChatwootSimulado([bruta], {910: mensagens}), banco, politica, config)

    assert relatorio.erros_por_tipo == {}
    assert sum(relatorio.turnos_por_resultado.values()) == 1


def test_o_relatorio_nao_leva_texto_nem_telefone(banco, politica, config):
    relatorio = _reproduzir(_servidor_do_exemplo(), banco, politica, config)

    texto = repr(relatorio)
    assert "quanto eu devo" not in texto
    assert "999990001" not in texto


def test_recusa_um_banco_que_ja_existe(banco, politica, config):
    banco.write_bytes(b"")

    with pytest.raises(FileExistsError):
        _reproduzir(_servidor_do_exemplo(), banco, politica, config)


def test_exige_a_inbox_na_configuracao(banco, politica):
    sem_inbox = Config(database_url="sqlite://")

    with pytest.raises(ValueError, match="inbox"):
        _reproduzir(_servidor_do_exemplo(), banco, politica, sem_inbox)


def test_a_amostra_real_passa_pela_reproducao_sem_erro(tmp_path, banco, politica):
    """Só estrutura e contagens. A carteira é montada no tmp com os telefones da amostra e nomes
    inventados: nada disso entra no repositório."""
    # A amostra tem mais de uma inbox; a reprodução lê a da primeira conversa.
    amostra = ler_amostra()
    inbox = amostra[0]["conversa"]["inbox_id"]
    amostra = [arquivo for arquivo in amostra if arquivo["conversa"]["inbox_id"] == inbox]
    conversas = [arquivo["conversa"] for arquivo in amostra]
    mensagens = {arquivo["conversa"]["id"]: arquivo["mensagens"] for arquivo in amostra}
    telefones = {c["meta"]["sender"]["phone_number"] for c in conversas}
    linhas = [
        f"R-{numero:04d};Cliente {numero};{telefone};3;"
        for numero, telefone in enumerate(sorted(telefones))
    ]
    carteira = tmp_path / "carteira.csv"
    cabecalho = "matricula;nome;telefone;mensalidades_abertas;regua"
    carteira.write_text("\n".join([cabecalho, *linhas]) + "\n", encoding="utf-8")
    config = Config(database_url="sqlite://", chatwoot_inbox=inbox)
    servidor = ChatwootSimulado(conversas, mensagens, pagina=20)

    with ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, TOKEN, transporte=servidor.transporte
    ) as leitura:
        relatorio = reproduzir(
            leitura, carteira, datetime(2000, 1, 1, tzinfo=UTC), banco, politica, config
        )

    assert relatorio.conversas_lidas == len(conversas)
    assert relatorio.conversas_cruzadas == len(conversas)
    assert relatorio.carteira_criadas == len(telefones)
    assert relatorio.carteira_rejeitadas == 0
    assert relatorio.erros_por_tipo == {}
    assert relatorio.mensagens + relatorio.mensagens_ignoradas == sum(map(len, mensagens.values()))
    assert sum(relatorio.eventos.values()) == relatorio.mensagens
    assert sum(relatorio.turnos_por_resultado.values()) == len(_turnos(banco)) > 0
    assert (relatorio.chamadas_ao_chatwoot, relatorio.templates_enviados) == (0, 0)
