"""A conferência no 1º turno do dia (spec do CTN real, seção 4, tarefa 2.4)."""

from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Mensagem
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso, MensalidadeAberta
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.integracoes.pix import PixFalso
from agente.integracoes.redator import RedatorFalso
from agente.tipos import Modo, MotivoHumano, OrigemMensagem, ResultadoTurno, StatusCobranca
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

from .test_orquestrador_gates import SEGUNDA_10H

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


def _ctn(escritas=None, abertas=((("2026/09", 3340), ("2026/10", 3340)))):
    ctn = CTNEspiao(
        escritas=escritas,
        clientes=[ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)],
        mensalidades_abertas={"M-0001": [MensalidadeAberta(m, v) for m, v in abertas]},
        hoje=date(2026, 10, 26),
    )
    return ctn


def _deps(politica, ctn, modo=Modo.ATIVO, conferir=True):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=JevFalso(list(aprovar_tudo()), registrar=True),
        ctn=ctn,
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        redator=RedatorFalso(registrar=True),
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


def test_ctn_fora_nao_bloqueia_o_turno_e_nao_marca_conferida(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.ENVIADO
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 100,20" in texto
    assert ligada.cobranca.conferida_em is None
    assert ligada.cobranca.divida_centavos == 10020


def test_ctn_fora_em_sombra_segue_com_o_retrato(sessao, ligada, politica):
    ctn = _ctn()
    ctn.fora_do_ar = True
    deps = _deps(politica, ctn, modo=Modo.SOMBRA)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert ligada.cobranca.conferida_em is None
    assert ligada.cobranca.divida_centavos == 10020


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


def test_quitada_no_ctn_nao_cobra_e_passa_ao_humano(sessao, ligada, politica):
    ctn = _ctn(abertas=())
    deps = _deps(politica, ctn)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    cobranca = ligada.cobranca
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.meses_em_aberto == []
    assert cobranca.divida_centavos == 10020
    # Nenhuma cobrança nem Pix: só o handoff, com o motivo da conciliação.
    texto_agente = sessao.scalars(
        select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    ).all()
    assert all("R$" not in m.texto for m in texto_agente)
    assert deps.pix.cobrancas == {}
    assert MotivoHumano.CONCILIACAO.value in " ".join(
        str(chamada) for chamada in deps.chatwoot.chamadas
    )


def test_quitada_no_ctn_em_sombra_nao_envia_nada(sessao, ligada, politica):
    ctn = _ctn(abertas=())
    deps = _deps(politica, ctn, modo=Modo.SOMBRA)
    _cliente_diz(sessao, ligada, "quanto eu devo?", SEGUNDA_10H, 1)

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.SOMBRA
    assert deps.chatwoot.chamadas == []
