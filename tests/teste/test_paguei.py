"""O pagamento de teste (D56, P3 e P4): `/paguei` confirma o Pix ou o link pendente pelo caminho do
webhook, e `/paguei <n>` paga n meses por fora pelo caminho das baixas do CTN."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.db.modelos import BaixaCTNRegistro, Conversa, Mensagem, Pagamento, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import FalhaNoEnvio
from agente.pagamento import confirmar
from agente.pagamento.baixas_ctn import (
    ABATEU,
    CONFERIDA_JUNTO,
    DependenciasDasBaixas,
    aplicar_baixas_simuladas,
)
from agente.pagamento.concluir import concluir_pagamentos
from agente.tipos import (
    EstadoConversa,
    FormaPagamento,
    OrigemMensagem,
    Pendencia,
    ResultadoTurno,
    StatusCobranca,
    StatusPagamento,
)
from tests.teste.auxiliares import CHATWOOT_ID, Mandante, montar_deps
from tests.turno.test_orquestrador_gates import SEGUNDA_10H

SIMULANDO_PIX = "[TESTE] Simulando a confirmação do banco: Pix de R$ 100,20."


def _conversa_do_chatwoot(sessao):
    return sessao.scalars(
        select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
    ).one()


def _ultimo_turno(sessao):
    return sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()


def _pagamento(sessao, cobranca, *, forma=FormaPagamento.PIX, referencia="tx-1", valor=None):
    pagamento = Pagamento(
        cobranca_id=cobranca.id,
        forma=forma,
        valor_centavos=valor if valor is not None else cobranca.divida_centavos,
        referencia=referencia,
        br_code="00020126-fictício" if forma is FormaPagamento.PIX else None,
        expira_em=SEGUNDA_10H + timedelta(hours=1),
    )
    sessao.add(pagamento)
    sessao.commit()
    # A abertura já carregou `cobranca.pagamentos` (o Pix integral dela, D65): recarrega.
    sessao.refresh(cobranca)
    return pagamento


def _no_caso(sessao, testador, deps, numero):
    """Troca para o caso `numero` e faz o 1º contato (a abertura); devolve quem fala na conversa
    nova, do Chatwoot."""
    testador.manda(f"/caso {numero}")
    testador.roda(deps)
    nova = Mandante(sessao, _conversa_do_chatwoot(sessao))
    nova.n = testador.n
    nova.manda("Oi")
    nova.roda(deps)
    return nova


@pytest.fixture(autouse=True)
def _relogio_da_confirmacao(monkeypatch):
    """A `confirmar_pagamento` agenda e grava no relógio dela: o do turno, nos testes."""
    monkeypatch.setattr(confirmar, "_agora", lambda: SEGUNDA_10H + timedelta(minutes=5))


@pytest.fixture
def conversa_aberta(sessao, testador, deps):
    """O caso 1, depois do 1º contato (a abertura saiu e o cliente já falou)."""
    testador.manda("Oi")
    testador.roda(deps)
    return testador


# ---- /paguei: o Pix ou o link pendente ----


def test_paguei_confirma_o_pix_pendente_pelo_caminho_do_webhook(
    sessao, conversa_aberta, conversa_de_teste, deps
):
    cobranca = conversa_de_teste.cobranca
    pix = _pagamento(sessao, cobranca)
    conversa_aberta.manda("/paguei")

    assert conversa_aberta.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(pix)
    sessao.refresh(cobranca)
    assert pix.status is StatusPagamento.PAGO
    assert cobranca.status is StatusCobranca.PAGA
    assert conversa_de_teste.estado is EstadoConversa.ENCERRADO
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == SIMULANDO_PIX
    assert Pendencia.MENSAGEM.value in pix.pendencias
    assert Pendencia.BAIXA.value in pix.pendencias
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:PAGUEI", ResultadoTurno.FIXA)
    assert deps.llm_turno.pedidos == []

    # A mensagem de confirmação do agente e a baixa saem pela rotina, como no webhook do Pix.
    enviadas = len(deps.chatwoot.enviadas(CHATWOOT_ID))
    assert concluir_pagamentos(sessao, deps, SEGUNDA_10H + timedelta(minutes=6)) == 1
    sessao.refresh(pix)
    assert pix.pendencias == [] and pix.baixa_ctn_em is not None
    confirmacao = deps.chatwoot.enviadas(CHATWOOT_ID)[enviadas:]
    assert len(confirmacao) == 1 and "R$ 100,20" in confirmacao[0]


def test_paguei_confirma_o_link_pendente(sessao, conversa_aberta, conversa_de_teste, deps):
    cobranca = conversa_de_teste.cobranca
    link = _pagamento(sessao, cobranca, forma=FormaPagamento.LINK_CTN, referencia="link-1")
    conversa_aberta.manda("/paguei")

    conversa_aberta.roda(deps)

    sessao.refresh(link)
    assert link.status is StatusPagamento.PAGO
    assert cobranca.status is StatusCobranca.PAGA
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == (
        "[TESTE] Simulando a confirmação do banco: link de R$ 100,20."
    )


def test_paguei_confirma_o_pendente_mais_recente(sessao, conversa_aberta, conversa_de_teste, deps):
    cobranca = conversa_de_teste.cobranca
    velho = _pagamento(sessao, cobranca, referencia="tx-velho")
    novo = _pagamento(sessao, cobranca, forma=FormaPagamento.LINK_CTN, referencia="link-novo")
    conversa_aberta.manda("/paguei")

    conversa_aberta.roda(deps)

    sessao.refresh(velho)
    sessao.refresh(novo)
    assert (novo.status, velho.status) == (StatusPagamento.PAGO, StatusPagamento.PENDENTE)


def test_paguei_depois_da_oferta_paga_noventa_e_quita_sem_pendencia_de_valor(
    sessao, conversa_aberta, conversa_de_teste, deps
):
    """O Pix integral da abertura segue pendente; o `/paguei` confirma o de R$ 90,00 (D64)."""
    cobranca = conversa_de_teste.cobranca
    integral = _pagamento(sessao, cobranca, referencia="tx-integral")
    com_desconto = _pagamento(sessao, cobranca, referencia="tx-90", valor=9000)
    conversa_aberta.manda("/paguei")

    conversa_aberta.roda(deps)

    sessao.refresh(com_desconto)
    sessao.refresh(integral)
    sessao.refresh(cobranca)
    assert com_desconto.status is StatusPagamento.PAGO
    assert com_desconto.divergente_centavos is None
    assert integral.status is StatusPagamento.PENDENTE
    assert cobranca.status is StatusCobranca.PAGA
    assert conversa_de_teste.estado is EstadoConversa.ENCERRADO
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == (
        "[TESTE] Simulando a confirmação do banco: Pix de R$ 90,00."
    )


def test_paguei_logo_depois_da_abertura_confirma_o_pix_integral_dela(
    sessao, conversa_aberta, conversa_de_teste, deps
):
    """A abertura registra o Pix integral (D65): o `/paguei` do 1º minuto o confirma."""
    cobranca = conversa_de_teste.cobranca
    [pix] = cobranca.pagamentos
    conversa_aberta.manda("/paguei")

    conversa_aberta.roda(deps)

    sessao.refresh(pix)
    assert pix.status is StatusPagamento.PAGO and pix.valor_centavos == cobranca.divida_centavos
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == SIMULANDO_PIX


def test_paguei_sem_pendente_so_orienta(sessao, conversa_aberta, conversa_de_teste, deps):
    for pagamento in conversa_de_teste.cobranca.pagamentos:
        pagamento.status = StatusPagamento.EXPIRADO
    sessao.commit()
    conversa_aberta.manda("/paguei")

    assert conversa_aberta.roda(deps) is ResultadoTurno.FIXA

    resposta = deps.chatwoot.enviadas(CHATWOOT_ID)[-1]
    assert resposta.startswith("[TESTE]") and "/paguei <n>" in resposta
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA


def test_paguei_nao_olha_o_pagamento_da_cobranca_trocada(sessao, testador, conversa_de_teste, deps):
    antiga = conversa_de_teste.cobranca
    pix = _pagamento(sessao, antiga)
    nova = _no_caso(sessao, testador, deps, 2)
    nova.manda("/paguei")

    nova.roda(deps)

    sessao.refresh(pix)
    assert pix.status is StatusPagamento.PENDENTE
    # Só o Pix da abertura da cobrança nova é visto (o caso 2 é R3: R$ 100,20).
    [da_nova] = _conversa_do_chatwoot(sessao).cobranca.pagamentos
    assert da_nova.status is StatusPagamento.PAGO and da_nova.id != pix.id
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1] == SIMULANDO_PIX


def test_paguei_num_caso_encerrado_so_orienta(sessao, conversa_aberta, conversa_de_teste, deps):
    _pagamento(sessao, conversa_de_teste.cobranca)
    conversa_aberta.manda("/paguei")
    conversa_aberta.roda(deps)
    antes = len(deps.chatwoot.enviadas(CHATWOOT_ID))

    conversa_aberta.manda("/paguei")
    conversa_aberta.roda(deps)

    depois = deps.chatwoot.enviadas(CHATWOOT_ID)[antes:]
    assert len(depois) == 1 and "caso terminou" in depois[0]


# ---- a ordem: o turno do comando primeiro, o pagamento depois, a resposta por último ----


def test_o_pagamento_que_falha_nao_desfaz_o_turno_do_comando(
    sessao, conversa_aberta, conversa_de_teste, deps, monkeypatch
):
    def quebra(*_args, **_kwargs):
        raise RuntimeError("banco fora")

    monkeypatch.setattr("agente.teste.comandos.confirmar_pagamento", quebra)
    pix = _pagamento(sessao, conversa_de_teste.cobranca)
    conversa_aberta.manda("/paguei")

    assert conversa_aberta.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(pix)
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:PAGUEI", ResultadoTurno.FIXA)
    assert pix.status is StatusPagamento.PENDENTE
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA
    resposta = deps.chatwoot.enviadas(CHATWOOT_ID)[-1]
    assert resposta.startswith("[TESTE] Não consegui") and "de novo" in resposta
    assert conversa_de_teste.falhas_turno == 0


class ChatwootQueNaoEnviaOSimulando(ChatwootFalso):
    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        if texto.startswith("[TESTE] Simulando"):
            raise FalhaNoEnvio("chatwoot fora")
        return super().enviar_mensagem(conversa_id, texto, botoes)


def test_a_resposta_que_falha_depois_do_pagamento_nao_desfaz_nada(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    deps = montar_deps(politica_de_teste, casos, ChatwootQueNaoEnviaOSimulando())
    testador.manda("Oi")
    testador.roda(deps)
    pix = _pagamento(sessao, conversa_de_teste.cobranca)
    testador.manda("/paguei")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(pix)
    assert pix.status is StatusPagamento.PAGO
    assert conversa_de_teste.cobranca.status is StatusCobranca.PAGA
    assert conversa_de_teste.falhas_turno == 0
    # A confirmação do agente sai mesmo assim, pela rotina.
    enviadas = len(deps.chatwoot.enviadas(CHATWOOT_ID))
    assert concluir_pagamentos(sessao, deps, SEGUNDA_10H + timedelta(minutes=6)) == 1
    assert len(deps.chatwoot.enviadas(CHATWOOT_ID)) == enviadas + 1


class ChatwootFora(ChatwootFalso):
    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        raise FalhaNoEnvio("chatwoot fora")


def test_o_chatwoot_que_falha_na_resposta_de_erro_do_comando_desfaz_o_turno(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    """Sem pendente, a orientação é parte do turno: se ela falha, o turno é desfeito e refeito."""
    deps = montar_deps(politica_de_teste, casos, ChatwootFora())
    testador.manda("/paguei")

    assert testador.roda(deps) is ResultadoTurno.ERRO

    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA
    assert [
        m.texto
        for m in sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.TESTE))
    ] == []


# ---- /paguei <n>: meses pagos por fora ----


def test_paguei_2_no_caso_3_abate_e_a_regua_cai(sessao, testador, conversa_de_teste, deps):
    nova = _no_caso(sessao, testador, deps, 3)
    cobranca = _conversa_do_chatwoot(sessao).cobranca
    assert cobranca.mensalidades_abertas == 4
    pix_da_divida_antiga = _pagamento(sessao, cobranca, valor=13360)
    antes = len(deps.chatwoot.enviadas(CHATWOOT_ID))
    nova.manda("/paguei 2")

    assert nova.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(cobranca)
    assert cobranca.status is StatusCobranca.ATIVA
    assert cobranca.mensalidades_abertas == 2
    assert cobranca.divida_centavos == 6680
    assert cobranca.regua.value == "R2"
    # O Pix gerado com a dívida antiga, se pago, cobriria o mês pago por fora de novo.
    sessao.refresh(pix_da_divida_antiga)
    assert pix_da_divida_antiga.status is StatusPagamento.EXPIRADO
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[antes:] == [
        "[TESTE] Simulando 2 mês(es) pago(s) por fora (boleto)."
    ]
    baixas = list(sessao.scalars(select(BaixaCTNRegistro).order_by(BaixaCTNRegistro.id)))
    assert [b.efeito for b in baixas] == [ABATEU, CONFERIDA_JUNTO]
    assert all(b.cobranca_id == cobranca.id for b in baixas)
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:PAGUEI", ResultadoTurno.FIXA)
    assert deps.llm_turno.pedidos == []


def test_paguei_5_no_caso_4_quita_e_o_aviso_sai(
    sessao, testador, conversa_de_teste, deps, politica_de_teste
):
    nova = _no_caso(sessao, testador, deps, 4)
    conversa = _conversa_do_chatwoot(sessao)
    cobranca = conversa.cobranca
    assert cobranca.mensalidades_abertas == 5
    antes = len(deps.chatwoot.enviadas(CHATWOOT_ID))
    nova.manda("/paguei 5")

    assert nova.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(cobranca)
    sessao.refresh(conversa)
    assert cobranca.status is StatusCobranca.PAGA
    assert cobranca.encerrada_motivo == "baixa_externa"
    assert conversa.estado is EstadoConversa.ENCERRADO
    enviadas = deps.chatwoot.enviadas(CHATWOOT_ID)[antes:]
    assert politica_de_teste.texto_baixa_externa in enviadas
    assert enviadas[-1] == "[TESTE] Simulando 5 mês(es) pago(s) por fora (boleto)."


def test_paguei_n_fora_da_faixa_explica_e_nao_paga(sessao, conversa_aberta, casos, deps):
    for texto in ("/paguei 4", "/paguei 0"):
        conversa_aberta.manda(texto)
        conversa_aberta.roda(deps)

        resposta = deps.chatwoot.enviadas(CHATWOOT_ID)[-1]
        assert resposta.startswith("[TESTE]") and "1 a 3" in resposta

    assert len(casos.mensalidades_em_aberto("SIM-1-7777")) == 3
    assert sessao.scalars(select(BaixaCTNRegistro)).first() is None


def test_dois_paguei_1_na_rajada_pagam_dois_meses_cada_um_com_a_sua_chave(
    sessao, conversa_aberta, conversa_de_teste, casos, deps
):
    conversa_aberta.manda("/paguei 1", "/paguei 1")

    conversa_aberta.roda(deps)

    assert len(casos.mensalidades_em_aberto("SIM-1-7777")) == 1
    cobranca = conversa_de_teste.cobranca
    sessao.refresh(cobranca)
    assert cobranca.mensalidades_abertas == 1


def test_paguei_n_depois_de_trocar_na_mesma_rajada_nao_paga_na_cobranca_encerrada(
    sessao, conversa_aberta, conversa_de_teste, casos, deps
):
    conversa_aberta.manda("/paguei 1", "/caso 2")

    conversa_aberta.roda(deps)

    # O `/caso 2` encerrou a cobrança do caso 1 antes de o pagamento rodar: nada foi pago nela.
    assert len(casos.mensalidades_em_aberto("SIM-1-7777")) == 3
    assert sessao.scalars(select(BaixaCTNRegistro)).first() is None


# ---- aplicar_baixas_simuladas ----


def test_aplicar_baixas_simuladas_e_idempotente_pelo_id_externo(
    sessao, conversa_aberta, conversa_de_teste, casos, deps, politica_de_teste
):
    deps_baixas = DependenciasDasBaixas(
        politica=politica_de_teste,
        chatwoot=deps.chatwoot,
        time_humano=deps.time_humano,
        ctn=deps.ctn,
    )
    agora = conversa_aberta.agora()
    baixas = casos.pagar_meses("SIM-1-7777", 1, agora, "k1")

    primeiro = aplicar_baixas_simuladas(sessao, deps_baixas, baixas, agora)
    segundo = aplicar_baixas_simuladas(sessao, deps_baixas, baixas, agora)

    assert (primeiro.lidas, primeiro.abatidas) == (1, 1)
    assert (segundo.lidas, segundo.abatidas, segundo.aplicadas) == (0, 0, 0)
    registros = list(sessao.scalars(select(BaixaCTNRegistro)))
    assert len(registros) == 1
    sessao.refresh(conversa_de_teste.cobranca)
    assert conversa_de_teste.cobranca.mensalidades_abertas == 2
