"""A conferência no CTN antes da abertura (spec do CTN real, seção 4, tarefa 2.4)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from agente.abertura.disparar import disparar_aberturas
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn import MensalidadeAberta
from agente.integracoes.disparador import DisparadorFalso
from agente.politica import Regua

from ..turno.test_orquestrador_conferencia import CTNEspiao, _Escritas

JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)


def _cobranca(sessao, matricula, divida=10020, meses=3):
    numero = int(matricula.split("-")[1])
    cliente = Cliente(
        matricula_ctn=matricula, nome="Maria Fictícia", telefone=f"+558399999{numero:04d}"
    )
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=meses,
        divida_centavos=divida,
        propenso_cartao=False,
        campanha="outubro",
    )
    sessao.add(cobranca)
    sessao.commit()
    return cobranca


def _ctn(escritas=None, **abertas):
    return CTNEspiao(
        escritas=escritas,
        mensalidades_abertas={
            matricula: [MensalidadeAberta(mes, valor) for mes, valor in itens]
            for matricula, itens in abertas.items()
        },
        hoje=date(2026, 10, 26),
    )


def test_o_disparo_confere_e_a_abertura_leva_o_valor_conferido(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    ctn = _ctn(**{"F-1": [("2026/09", 3340), ("2026/10", 3340)]})
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert relatorio.enviadas == 1
    assert ctn.chamadas == ["mensalidades", "historico"]
    assert cobranca.conferida_em == SEGUNDA_10H
    assert cobranca.divida_centavos == 6680
    [envio] = disparador.envios
    assert formatar_reais(6680) in str(envio)


def test_sem_ctn_o_disparo_nao_confere(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    assert relatorio.enviadas == 1
    assert cobranca.conferida_em is None


def test_a_cobranca_ja_conferida_hoje_nao_chama_o_ctn(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    cobranca.conferida_em = SEGUNDA_10H - timedelta(hours=1)
    sessao.commit()
    ctn = _ctn()

    disparar_aberturas(sessao, politica, DisparadorFalso(), "outubro", SEGUNDA_10H, ctn)

    assert ctn.chamadas == []


def test_ctn_fora_nao_bloqueia_a_abertura_e_nao_marca_conferida(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    ctn = _ctn(**{"F-1": [("2026/10", 3340)]})
    ctn.fora_do_ar = True
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert relatorio.enviadas == 1
    assert cobranca.conferida_em is None
    assert cobranca.divida_centavos == 10020
    assert formatar_reais(10020) in str(disparador.envios[0])


def test_quitada_no_ctn_nao_recebe_a_abertura_e_fica_ativa(sessao, politica):
    paga = _cobranca(sessao, "F-1")
    devendo = _cobranca(sessao, "F-2")
    ctn = _ctn(**{"F-1": [], "F-2": [("2026/10", 3340)]})
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.enviadas, relatorio.quitadas_no_ctn) == (1, 1)
    assert len(disparador.envios) == 1
    assert paga.conversas == []
    assert paga.meses_em_aberto == []
    assert paga.status.value == "ATIVA"
    assert len(devendo.conversas) == 1

    # Uma segunda rodada no mesmo dia não manda a abertura à quitada nem chama o CTN para ela.
    chamadas = len(ctn.chamadas)
    segunda = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)
    assert (segunda.enviadas, segunda.quitadas_no_ctn) == (0, 1)
    assert len(ctn.chamadas) == chamadas
    assert len(sessao.scalars(select(Conversa)).all()) == 1


def test_durante_a_chamada_ao_ctn_nao_ha_escrita_pendente(sessao, politica):
    escritas = _Escritas(sessao)
    _cobranca(sessao, "F-1")
    _cobranca(sessao, "F-2")
    ctn = _ctn(escritas, **{"F-1": [("2026/10", 3340)], "F-2": [("2026/10", 3340)]})

    disparar_aberturas(sessao, politica, DisparadorFalso(), "outubro", SEGUNDA_10H, ctn)

    # Cada envio faz commit; a cobrança seguinte é conferida com a transação limpa, sem a trava
    # curta da anterior. As 4 chamadas (2 por cobrança) viram zero escritas pendentes.
    assert ctn.chamadas == ["mensalidades", "historico"] * 2
    assert ctn.escritas_pendentes == [0, 0, 0, 0]


def test_fora_da_janela_nao_chama_o_ctn(sessao, politica):
    _cobranca(sessao, "F-1")
    ctn = _ctn()
    domingo = datetime(2026, 11, 1, 10, tzinfo=JP)

    relatorio = disparar_aberturas(sessao, politica, DisparadorFalso(), "outubro", domingo, ctn)

    assert relatorio.fora_da_janela is True
    assert ctn.chamadas == []
