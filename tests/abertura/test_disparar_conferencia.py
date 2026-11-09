"""A conferência no CTN antes da abertura (spec do CTN real, seção 4, tarefa 2.4)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from agente.abertura.disparar import (
    ALARME_CONFERENCIA_MINIMO,
    disparar_aberturas,
)
from agente.db.modelos import Cliente, Cobranca, Conversa
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.ctn import MensalidadeAberta
from agente.integracoes.disparador import DisparadorFalso
from agente.politica import Regua
from agente.tipos import StatusCobranca

from ..turno.test_orquestrador_conferencia import CTNEspiao, _Escritas

JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)


def _cobranca(sessao, matricula, divida=10020, meses=3, telefone=None):
    numero = int(matricula.split("-")[1])
    cliente = Cliente(
        matricula_ctn=matricula,
        nome="Maria Fictícia",
        telefone=telefone or f"+558399999{numero:04d}",
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


def _ctn(escritas=None, rejeitadas=None, **abertas):
    return CTNEspiao(
        escritas=escritas,
        mensalidades_abertas={
            matricula: [MensalidadeAberta(mes, valor) for mes, valor in itens]
            for matricula, itens in abertas.items()
        },
        rejeitadas_lidas=rejeitadas,
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


def test_ctn_fora_nao_envia_valor_estimado_e_deixa_a_cobranca_ativa(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    ctn = _ctn(**{"F-1": [("2026/10", 3340)]})
    ctn.fora_do_ar = True
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.enviadas, relatorio.nao_conferidas) == (0, 1)
    assert disparador.envios == []
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.conversas == []
    assert cobranca.conferida_em is None
    assert cobranca.divida_centavos == 10020

    # Na próxima rodada, com o CTN de volta, a cobrança sai com o valor conferido.
    ctn.fora_do_ar = False
    segunda = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)
    assert (segunda.enviadas, segunda.nao_conferidas) == (1, 0)
    assert formatar_reais(3340) in str(disparador.envios[0])


def test_quitada_no_ctn_nao_recebe_a_abertura_e_fica_ativa(sessao, politica):
    paga = _cobranca(sessao, "F-1")
    devendo = _cobranca(sessao, "F-2")
    ctn = _ctn(rejeitadas={"F-1": 3}, **{"F-1": [], "F-2": [("2026/10", 3340)]})
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


def test_matricula_sem_lancamentos_nao_e_quitada_e_nao_recebe_a_abertura(sessao, politica):
    sem = _cobranca(sessao, "F-1")
    ctn = _ctn()
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.sem_lancamentos, relatorio.quitadas_no_ctn, relatorio.enviadas) == (1, 0, 0)
    assert disparador.envios == []
    assert sem.conferida_em is None
    assert sem.status is StatusCobranca.ATIVA


def test_dado_suspeito_nao_recebe_a_abertura_e_e_contado(sessao, politica):
    suspeita = _cobranca(sessao, "F-1")
    ctn = _ctn(**{"F-1": [("2026/10", 0)]})
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.dados_suspeitos, relatorio.enviadas, relatorio.nao_conferidas) == (1, 0, 0)
    assert disparador.envios == []
    assert suspeita.conferida_em is None


def _lote(sessao, quitadas, total):
    """`total` cobranças: as `quitadas` primeiras já pagas no CTN, as outras devendo um mês."""
    abertas = {}
    for n in range(1, total + 1):
        _cobranca(sessao, f"F-{n}")
        abertas[f"F-{n}"] = [] if n <= quitadas else [("2026/10", 3340)]
    rejeitadas = {f"F-{n}": 2 for n in range(1, quitadas + 1)}
    return _ctn(rejeitadas=rejeitadas, **abertas)


def test_quitadas_demais_na_rodada_param_a_campanha(sessao, politica):
    # 7 de 20 conferências (35%) já pagas: o CTN ou a carteira estão errados, e ninguém mais recebe.
    ctn = _lote(sessao, quitadas=7, total=ALARME_CONFERENCIA_MINIMO + 5)
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert relatorio.alarme_conferencia is True
    assert relatorio.quitadas_no_ctn == 7
    # As conferências 8 a 19 saíram; a 20ª (que passou do limite) e as seguintes, não.
    assert relatorio.enviadas == 12
    assert len(disparador.envios) == 12
    # As 5 que sobraram não foram nem conferidas.
    assert ctn.chamadas.count("mensalidades") == ALARME_CONFERENCIA_MINIMO


def test_sem_lancamentos_demais_tambem_param_a_campanha(sessao, politica):
    # Matrículas que o CTN não conhece contam como as quitadas.
    for n in range(1, 21):
        _cobranca(sessao, f"F-{n}")
    ctn = _ctn()
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert relatorio.alarme_conferencia is True
    assert relatorio.sem_lancamentos == 20
    assert relatorio.enviadas == 0


def test_no_limite_exato_de_trinta_por_cento_a_campanha_segue(sessao, politica):
    ctn = _lote(sessao, quitadas=6, total=20)
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert relatorio.alarme_conferencia is False
    assert (relatorio.enviadas, relatorio.quitadas_no_ctn) == (14, 6)


def test_com_menos_de_vinte_conferencias_nao_ha_alarme(sessao, politica):
    # 100% quitadas, mas só 19 conferências: amostra pequena demais para parar a campanha.
    ctn = _lote(sessao, quitadas=ALARME_CONFERENCIA_MINIMO - 1, total=ALARME_CONFERENCIA_MINIMO - 1)

    relatorio = disparar_aberturas(sessao, politica, DisparadorFalso(), "outubro", SEGUNDA_10H, ctn)

    assert relatorio.alarme_conferencia is False
    assert relatorio.quitadas_no_ctn == ALARME_CONFERENCIA_MINIMO - 1


def test_telefone_compartilhado_por_duas_cobrancas_ativas_nao_recebe_a_abertura(sessao, politica):
    # O mesmo celular, com e sem o 9 (A1), em duas matrículas: não se sabe quem é quem.
    uma = _cobranca(sessao, "F-1", telefone="+5583988887777")
    outra = _cobranca(sessao, "F-2", telefone="+558388887777")
    so_a = _cobranca(sessao, "F-3")
    ctn = _ctn(**{m: [("2026/10", 3340)] for m in ("F-1", "F-2", "F-3")})
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.enviadas, relatorio.telefone_compartilhado) == (1, 2)
    assert uma.conversas == [] and outra.conversas == []
    assert len(so_a.conversas) == 1
    # Nem o CTN foi chamado para elas: não há o que conferir se a cobrança não sai.
    assert ctn.chamadas == ["mensalidades", "historico"]


def test_telefone_de_cobranca_encerrada_nao_conta_como_compartilhado(sessao, politica):
    velha = _cobranca(sessao, "F-1", telefone="+5583988887777")
    velha.status = StatusCobranca.PAGA
    atual = _cobranca(sessao, "F-2", telefone="+5583988887777")
    sessao.commit()
    ctn = _ctn(**{"F-2": [("2026/10", 3340)]})

    relatorio = disparar_aberturas(sessao, politica, DisparadorFalso(), "outubro", SEGUNDA_10H, ctn)

    assert (relatorio.enviadas, relatorio.telefone_compartilhado) == (1, 0)
    assert len(atual.conversas) == 1


class _CTNQueMudaACobranca(CTNEspiao):
    """Durante a leitura, outro processo cria uma conversa ou fecha a cobrança."""

    def __init__(self, sessao, cobranca_id, **resto):
        super().__init__(**resto)
        self._sessao = sessao
        self._cobranca_id = cobranca_id
        self.mudanca = None

    def mensalidades_em_aberto(self, matricula):
        if self.mudanca == "conversa":
            self._sessao.add(Conversa(cobranca_id=self._cobranca_id))
        elif self.mudanca == "paga":
            self._sessao.execute(
                update(Cobranca)
                .where(Cobranca.id == self._cobranca_id)
                .values(status=StatusCobranca.PAGA)
            )
        self._sessao.commit()
        self.mudanca = None
        return super().mensalidades_em_aberto(matricula)


def test_conversa_criada_durante_a_chamada_ao_ctn_nao_recebe_a_abertura(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    ctn = _CTNQueMudaACobranca(
        sessao,
        cobranca.id,
        mensalidades_abertas={"F-1": [MensalidadeAberta("2026/10", 3340)]},
        hoje=date(2026, 10, 26),
    )
    ctn.mudanca = "conversa"
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert disparador.envios == []
    assert (relatorio.enviadas, relatorio.puladas) == (0, 1)
    assert len(cobranca.conversas) == 1


def test_cobranca_paga_durante_a_chamada_ao_ctn_nao_recebe_a_abertura(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    ctn = _CTNQueMudaACobranca(
        sessao,
        cobranca.id,
        mensalidades_abertas={"F-1": [MensalidadeAberta("2026/10", 3340)]},
        hoje=date(2026, 10, 26),
    )
    ctn.mudanca = "paga"
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H, ctn)

    assert disparador.envios == []
    assert relatorio.enviadas == 0
    assert cobranca.conversas == []


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
