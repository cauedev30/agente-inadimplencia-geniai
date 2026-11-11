"""A conferência no 1º turno do dia (spec do CTN real, seção 4, tarefa 2.4)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import registrar_mensagem
from agente.db.modelos import Conversa, Mensagem, Transicao, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso, MensalidadeAberta
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.integracoes.pix import PixFalso
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.fila import mensagens_pendentes
from agente.turno.handoff import NOTA_CONCILIACAO_REPETIDA, devolver_ao_robo
from agente.turno.orquestrador import Dependencias, processar_turno

from ..conftest import piloto_com
from .auxiliares import SEGUNDA_10H, TELEFONE, _bruto
from .test_orquestrador_gates import DOMINGO

ESCRITA = ("INSERT", "UPDATE", "DELETE")


class _Escritas:
    """Conta os comandos de escrita executados desde o último commit da sessão."""

    def __init__(self, sessao):
        self.pendentes = 0
        motor = sessao.get_bind()
        event.listen(motor, "before_cursor_execute", self._executar)
        event.listen(motor, "commit", self._commit)
        self._motor = motor

    def _executar(self, _conn, _cursor, comando, *_resto):
        if comando.lstrip().upper().startswith(ESCRITA):
            self.pendentes += 1

    def _commit(self, _conn):
        self.pendentes = 0


class CTNEspiao(CTNFalso):
    """Conta as chamadas de leitura e guarda quantas escritas estavam pendentes em cada uma."""

    def __init__(self, escritas=None, **resto):
        super().__init__(**resto)
        self.chamadas = []
        self.escritas = escritas
        self.escritas_pendentes = []

    def _registrar(self, nome):
        self.chamadas.append(nome)
        if self.escritas is not None:
            self.escritas_pendentes.append(self.escritas.pendentes)

    def mensalidades_em_aberto(self, matricula):
        self._registrar("mensalidades")
        return super().mensalidades_em_aberto(matricula)

    def historico_pagamentos(self, matricula, meses):
        self._registrar("historico")
        return super().historico_pagamentos(matricula, meses)


def _ctn(escritas=None, abertas=((("2026/09", 3340), ("2026/10", 3340))), rejeitadas=None):
    # `rejeitadas`: quantos lançamentos rejeitados o CTN devolve (sem ele, são as abertas); sem
    # abertas e sem rejeitadas, o CTN "não tem lançamento nenhum" da matrícula.
    ctn = CTNEspiao(
        escritas=escritas,
        clientes=[ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)],
        mensalidades_abertas={"M-0001": [MensalidadeAberta(m, v) for m, v in abertas]},
        rejeitadas_lidas={"M-0001": rejeitadas} if rejeitadas is not None else None,
        hoje=date(2026, 10, 26),
    )
    return ctn


def _deps(politica, ctn, conferir=True):
    return Dependencias(
        politica=piloto_com(politica, TELEFONE),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        llm_turno=LLMTurnoFalso(respostas=[_bruto()] * 10, registrar=True),
        ctn=ctn,
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        conferir_no_contato=conferir,
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _cliente_diz(sessao, conversa, texto, quando, n):
    registrar_mensagem(sessao, conversa.id, OrigemMensagem.CLIENTE, texto, f"cw-{n}", False, quando)
    sessao.commit()


def test_o_1o_turno_confere_e_manda_o_valor_conferido(sessao, ligada, politica):
    ctn = _ctn()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert ctn.chamadas == ["mensalidades", "historico"]
    cobranca = ligada.cobranca
    assert cobranca.conferida_em == SEGUNDA_10H
    assert cobranca.divida_centavos == 6680
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 66,80" in texto and "R$ 100,20" not in texto


def test_o_2o_turno_do_mesmo_dia_nao_chama_o_ctn(sessao, ligada, politica):
    ctn = _ctn()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    assert len(ctn.chamadas) == 2

    mais_tarde = SEGUNDA_10H + timedelta(hours=2)
    _cliente_diz(sessao, ligada, "e se eu pagar amanhã?", mais_tarde, 2)
    processar_turno(sessao, ligada.id, mais_tarde, deps)

    assert len(ctn.chamadas) == 2


def test_conferida_ontem_confere_de_novo(sessao, ligada, politica):
    ctn = _ctn()
    ligada.cobranca.conferida_em = SEGUNDA_10H - timedelta(days=1)
    sessao.commit()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert ctn.chamadas == ["mensalidades", "historico"]
    assert ligada.cobranca.conferida_em == SEGUNDA_10H


def _sem_valor_ao_cliente(deps):
    """Nada com valor saiu ao cliente: nem texto com R$, nem Pix."""
    assert all("R$" not in texto for texto in deps.chatwoot.enviadas(555))
    assert deps.pix.cobrancas == {}


def test_ctn_fora_nao_cita_valor_e_o_turno_espera(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # O CTN fora é integração fora: o turno espera com a espera das integrações e a mensagem do
    # cliente segue pendente. Nenhum valor estimado (o do retrato, R$ 100,20) sai.
    assert resultado is ResultadoTurno.ERRO
    assert deps.chatwoot.enviadas(555) == []
    assert ligada.falhas_turno == 1
    assert ligada.turno_em is not None
    assert len(mensagens_pendentes(sessao, ligada.id)) == 1
    assert ligada.cobranca.conferida_em is None
    assert ligada.cobranca.divida_centavos == 10020
    _sem_valor_ao_cliente(deps)


def test_ctn_fora_em_todas_as_tentativas_leva_ao_humano_sem_valor(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    for tentativa in range(deps.politica.integracao_tentativas):
        quando = SEGUNDA_10H + timedelta(minutes=10 * tentativa)
        resultado = processar_turno(sessao, ligada.id, quando, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.INTEGRACAO_INDISPONIVEL
    _sem_valor_ao_cliente(deps)


def test_ctn_que_volta_na_2a_tentativa_confere_e_manda_o_valor_conferido(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO

    ctn.fora_do_ar = False
    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H + timedelta(minutes=10), deps)

    assert resultado is ResultadoTurno.ENVIADO
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 66,80" in texto and "R$ 100,20" not in texto
    assert ligada.falhas_turno == 0


@pytest.mark.parametrize(
    "ctn_sem_conferencia",
    [
        # nenhum lançamento rejeitado veio, e o retrato diz que há 3 meses em atraso
        lambda: _ctn(abertas=()),
        # dado suspeito: valor zero
        lambda: _ctn(abertas=(("2026/10", 0),)),
        # dado suspeito: mês repetido
        lambda: _ctn(abertas=(("2026/10", 3340), ("2026/10", 3340))),
    ],
    ids=["sem_lancamentos", "valor_zero", "mes_repetido"],
)
def test_conferencia_sem_valor_confiavel_tambem_espera_sem_citar_valor(
    sessao, ligada, politica, ctn_sem_conferencia
):
    deps = _deps(politica, ctn_sem_conferencia())
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ERRO
    assert deps.chatwoot.enviadas(555) == []
    assert ligada.cobranca.conferida_em is None
    assert ligada.cobranca.status is StatusCobranca.ATIVA
    _sem_valor_ao_cliente(deps)


def test_ctn_fora_no_toque_do_botao_tambem_nao_gera_pix(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, BOTAO_PAGAR_PIX.texto, SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ERRO
    _sem_valor_ao_cliente(deps)


def test_durante_a_chamada_ao_ctn_nao_ha_escrita_pendente_nem_trava(sessao, ligada, politica):
    escritas = _Escritas(sessao)
    ctn = _ctn(escritas)
    deps = _deps(politica, ctn)
    # A mensagem do cliente já está commitada: o turno começa sem escrita pendente.
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    # Nenhum UPDATE/INSERT executado desde o último commit quando o CTN foi chamado: a linha da
    # cobrança (FOR NO KEY UPDATE) só é pega depois, na gravação curta, e solta no commit.
    assert ctn.chamadas == ["mensalidades", "historico"]
    assert ctn.escritas_pendentes == [0, 0]


def test_sem_a_chave_do_ctn_real_o_turno_nao_confere(sessao, ligada, politica):
    ctn = _ctn()
    deps = _deps(politica, ctn, conferir=False)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert ctn.chamadas == []
    assert ligada.cobranca.conferida_em is None


def _quitada():
    """O CTN devolveu 3 rejeitadas (os 3 meses do retrato) e as três foram pagas."""
    return _ctn(abertas=(), rejeitadas=3)


def _saidas_para_o_humano(sessao):
    return [
        t.motivo
        for t in sessao.scalars(
            select(Transicao)
            .where(Transicao.para == EstadoConversa.ATENDIMENTO_HUMANO)
            .order_by(Transicao.id)
        )
    ]


def test_quitada_no_ctn_nao_cobra_e_passa_ao_humano(sessao, ligada, politica):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    cobranca = ligada.cobranca
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.meses_em_aberto == []
    assert cobranca.divida_centavos == 10020
    # Nenhuma cobrança nem Pix: só o handoff, com o motivo da conciliação, e a mensagem do canal.
    texto_agente = sessao.scalars(
        select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    ).all()
    assert all("R$" not in m.texto for m in texto_agente)
    assert deps.pix.cobrancas == {}
    assert MotivoHumano.CONCILIACAO.value in " ".join(
        str(chamada) for chamada in deps.chatwoot.chamadas
    )
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _saidas_para_o_humano(sessao) == [MotivoHumano.CONCILIACAO]
    turno = sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()
    assert turno.acao_principal == "HUMAN:CONCILIACAO"


def test_quitada_no_ctn_no_toque_do_botao_nao_gera_pix(sessao, ligada, politica):
    deps = _deps(politica, _quitada())
    _cliente_diz(sessao, ligada, BOTAO_PAGAR_PIX.texto, SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert deps.pix.cobrancas == {}


def test_quitada_no_ctn_com_humano_na_conversa_nao_faz_nada(sessao, ligada, politica):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    ligada.human_owned = True
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.IGNORADO
    # O gate do humano vem antes: nem o CTN foi chamado, nem o Chatwoot.
    assert ctn.chamadas == []
    assert deps.chatwoot.chamadas == []
    assert _saidas_para_o_humano(sessao) == []


def test_quitada_no_ctn_devolvida_ao_robo_volta_ao_humano_sem_mensagem(sessao, ligada, politica):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF
    devolver_ao_robo(sessao, ligada, SEGUNDA_10H + timedelta(minutes=30))
    sessao.commit()
    mais_tarde = SEGUNDA_10H + timedelta(minutes=40)
    _cliente_diz(sessao, ligada, "e aí?", mais_tarde, 2)

    resultado = processar_turno(sessao, ligada.id, mais_tarde, deps)

    # A 2ª vez: volta ao humano, com a nota, e o cliente não recebe a mensagem de novo.
    assert resultado is ResultadoTurno.HANDOFF
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _saidas_para_o_humano(sessao) == [MotivoHumano.CONCILIACAO, MotivoHumano.CONCILIACAO]
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    notas = [c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada"]
    assert NOTA_CONCILIACAO_REPETIDA in notas[-1]
    # Mesmo dia: o CTN não foi chamado de novo.
    assert ctn.chamadas == ["mensalidades"]


def test_quitada_no_ctn_com_a_6a_ja_enviada_segue_o_gate_do_teto(sessao, ligada, politica):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    for i in range(deps.politica.teto_resolucao):
        registrar_mensagem(
            sessao, ligada.id, OrigemMensagem.AGENTE, f"r{i}", f"a{i}", True, SEGUNDA_10H
        )
    sessao.commit()
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H + timedelta(minutes=1), 1)

    primeira = processar_turno(sessao, ligada.id, SEGUNDA_10H + timedelta(minutes=1), deps)
    devolver_ao_robo(sessao, ligada, SEGUNDA_10H + timedelta(minutes=30))
    sessao.commit()
    mais_tarde = SEGUNDA_10H + timedelta(minutes=40)
    _cliente_diz(sessao, ligada, "e aí?", mais_tarde, 2)
    segunda = processar_turno(sessao, ligada.id, mais_tarde, deps)

    # A 6ª sai uma vez (TETO); a volta é silenciosa. A quitada nem chegou a ser conferida.
    assert (primeira, segunda) == (ResultadoTurno.FIXA, ResultadoTurno.HANDOFF)
    assert len(deps.chatwoot.enviadas(555)) == 1
    assert _saidas_para_o_humano(sessao) == [MotivoHumano.TETO, MotivoHumano.TETO]
    assert ctn.chamadas == []


def test_quitada_no_ctn_com_a_ultima_mensagem_de_mais_de_24h_vai_ao_humano_sem_mensagem(
    sessao, ligada, politica
):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "oi", SEGUNDA_10H - timedelta(hours=25), 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert deps.chatwoot.enviadas(555) == []
    assert deps.disparador.envios == []
    assert ligada.motivo_humano is MotivoHumano.FORA_DAS_24H
    assert ctn.chamadas == []


def test_quitada_no_ctn_fora_da_janela_de_atendimento_adia_sem_chamar_o_ctn(
    sessao, ligada, politica
):
    ctn = _quitada()
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "oi", DOMINGO, 1)

    resultado = processar_turno(sessao, ligada.id, DOMINGO, deps)

    assert resultado is ResultadoTurno.ADIADO
    assert ctn.chamadas == []
    assert deps.chatwoot.chamadas == []


def test_quitada_no_ctn_descarta_a_mensagem_pendente_com_o_valor_antigo(sessao, ligada, politica):
    deps = _deps(politica, _quitada())
    pendente = registrar_mensagem(
        sessao,
        ligada.id,
        OrigemMensagem.AGENTE,
        "Você deve R$ 100,20",
        None,
        True,
        SEGUNDA_10H,
        envio=EstadoEnvio.PENDENTE,
    )
    _cliente_diz(sessao, ligada, "oi", SEGUNDA_10H + timedelta(minutes=1), 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H + timedelta(minutes=1), deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert pendente.envio is EstadoEnvio.DESCARTADA
    assert all("R$" not in texto for texto in deps.chatwoot.enviadas(555))


def test_reset_da_equipe_funciona_numa_cobranca_quitada(sessao, ligada, politica):
    ctn = _quitada()
    da_equipe = politica.model_copy(
        update={"telefones_equipe": frozenset({ligada.cobranca.cliente.telefone})}
    )
    deps = _deps(da_equipe, ctn)
    _cliente_diz(sessao, ligada, "oi", SEGUNDA_10H, 1)
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF
    mais_tarde = SEGUNDA_10H + timedelta(minutes=5)
    _cliente_diz(sessao, ligada, deps.politica.comando_reinicio, mais_tarde, 2)

    resultado = processar_turno(sessao, ligada.id, mais_tarde, deps)

    assert resultado is ResultadoTurno.IGNORADO
    assert deps.politica.texto_reinicio in deps.chatwoot.enviadas(555)
    conversas = sessao.scalars(select(Conversa).order_by(Conversa.id)).all()
    assert len(conversas) == 2
    # A conversa nova é a 1ª vez dela: a quitada leva a mensagem do handoff de novo.
    nova = conversas[1]
    depois = mais_tarde + timedelta(minutes=1)
    _cliente_diz(sessao, nova, "oi de novo", depois, 3)
    assert processar_turno(sessao, nova.id, depois, deps) is ResultadoTurno.HANDOFF
    assert _saidas_para_o_humano(sessao) == [MotivoHumano.CONCILIACAO, MotivoHumano.CONCILIACAO]
