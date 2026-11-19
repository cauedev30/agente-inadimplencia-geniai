"""O turno de uma chamada ligado ao orquestrador, de ponta a ponta com as falsas (D47, E1.5)."""

import json
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import registrar_mensagem
from agente.db.modelos import LaudoGravado, Mensagem, Pagamento, Transicao, Turno
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.llm_turno import LLMTurnoFalso, LLMTurnoIndisponivel
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    FormaPagamento,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno import orquestrador
from agente.turno.catalogo import (
    POLITICAS,
    TEXTO_ARQUIVO_RECEBIDO,
    TEXTO_CANAL_EXTERNO,
    Situacao,
)
from agente.turno.orquestrador import processar_turno
from agente.turno.uma_chamada import ORIGEM_UMA_CHAMADA, SINAIS

from .auxiliares import (
    REPLY_LINK,
    REPLY_LINK_FINAL,
    REPLY_NONE,
    Cliente,
    _bruto,
    _deps,
)
from .test_orquestrador_gates import SEGUNDA_10H
from .test_orquestrador_pagamento_concorrente import _pagar_por_fora


@dataclass
class LLMQueEspera(LLMTurnoFalso):
    """Enquanto o LLM responde, o pagamento chega."""

    ao_chamar: object = None

    def completar_json(self, sistema, usuario, esquema):
        if self.ao_chamar is not None:
            self.ao_chamar()
            self.ao_chamar = None
        return super().completar_json(sistema, usuario, esquema)


class CTNQueEspera(CTNFalso):
    """Enquanto o link é gerado, o pagamento chega."""

    ao_gerar = None

    def gerar_link_pagamento(self, *args, **kwargs):
        if self.ao_gerar is not None:
            self.ao_gerar()
            self.ao_gerar = None
        return super().gerar_link_pagamento(*args, **kwargs)


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


@pytest.fixture
def cliente(sessao, ligada):
    return Cliente(sessao, ligada)


def _turno(sessao):
    return sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()


def _do_agente(sessao):
    consulta = select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    return sessao.scalars(consulta.order_by(Mensagem.id)).all()


def _laudos(sessao, batch):
    consulta = select(LaudoGravado).where(LaudoGravado.batch == batch).order_by(LaudoGravado.id)
    return sessao.scalars(consulta).all()


# O caminho


def test_o_turno_fecha_enviado_com_o_texto_do_luna_e_o_pix_do_backend(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto()])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    [pix] = deps.pix.cobrancas.values()
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 100,20" in texto and pix.br_code in texto
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("INFORM_DEBT", ["GENERATE_PIX"])
    assert turno.texto_enviado == texto
    assert turno.redacao["caminho"] == "uma_chamada" and turno.redacao["saida"] == "LLM"
    assert turno.numeros["valor_pix_centavos"] == 10020
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (texto, True)
    assert sessao.scalars(select(Pagamento)).one().referencia == pix.txid


REPLY_SO_A_DIVIDA = "Seu valor em aberto é {divida}."
REPLY_PIX_COM_DESCONTO = (
    "Seu valor com desconto é {valor_final}.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
)


def test_o_pix_escrito_pelo_luna_com_a_ref_sai_com_os_meses_em_aberto(
    sessao, ligada, cliente, politica
):
    ligada.cobranca.meses_em_aberto = ["2026/09", "2026/10"]
    reply = (
        "Aqui está o Pix!\nValor: {valor_pix}\nREF {referencia}\n"
        "Para pagar, use o Pix CNPJ: {pix_copia_e_cola}"
    )
    deps = _deps(politica, [_bruto(acao="GENERATE_PIX", reply=reply)])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    assert "\nREF 09/26, 10/26\n" in texto


def test_o_2o_quanto_devo_sai_sem_o_pix_e_sem_pagamento_novo(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(), _bruto(reply=REPLY_SO_A_DIVIDA)])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO
    assert cliente("e quanto é mesmo?", deps) is ResultadoTurno.ENVIADO

    [pix] = deps.pix.cobrancas.values()
    [primeira, segunda] = deps.chatwoot.enviadas(555)
    assert pix.br_code in primeira
    assert "R$ 100,20" in segunda and pix.br_code not in segunda
    assert len(sessao.scalars(select(Pagamento)).all()) == 1
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("INFORM_DEBT", [])
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    entrada = json.loads(deps.llm_turno.pedidos[1][1])
    assert entrada["meio_vigente"] == "PIX"


def test_com_o_pix_vencido_o_2o_quanto_devo_reanexa_o_pix(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(), _bruto()])
    cliente("quanto eu devo?", deps)
    antigo = sessao.scalars(select(Pagamento)).one()
    antigo.expira_em = SEGUNDA_10H - timedelta(days=2)
    sessao.flush()

    assert cliente("e quanto é mesmo?", deps) is ResultadoTurno.ENVIADO

    assert len(deps.pix.cobrancas) == 2
    [_, segunda] = deps.chatwoot.enviadas(555)
    assert antigo.br_code not in segunda and "R$ 100,20" in segunda
    assert len(sessao.scalars(select(Pagamento)).all()) == 2
    assert json.loads(deps.llm_turno.pedidos[1][1])["meio_vigente"] is None


def test_as_novas_da_rajada_vao_em_novas_e_as_anteriores_em_conversa(
    sessao, ligada, cliente, politica
):
    anterior = registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.CLIENTE, "oi", "cw-0", False, SEGUNDA_10H
    )
    registrar_mensagem(sessao, ligada.id, OrigemMensagem.AGENTE, "Olá!", None, True, SEGUNDA_10H)
    sessao.flush()
    ligada.ultima_tomada_id = anterior.id
    deps = _deps(politica, [_bruto()])
    registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.CLIENTE, "quanto eu devo?", "cw-1", False, SEGUNDA_10H
    )
    registrar_mensagem(
        sessao, ligada.id, OrigemMensagem.CLIENTE, "e o pix?", "cw-2", False, SEGUNDA_10H
    )

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ENVIADO

    [(_, usuario)] = deps.llm_turno.pedidos
    entrada = json.loads(usuario)
    assert entrada["novas"] == ["quanto eu devo?", "e o pix?"]
    assert entrada["conversa"] == [["CLIENTE", "oi"], ["AGENTE", "Olá!"]]
    assert entrada["mensagens_usadas"] == 1


# Os laudos


def test_os_laudos_vao_em_jev1_e_jev2_com_a_origem_do_turno_novo(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(tom="impaciente", acao="INFORM_DEBT")])

    cliente("quanto eu devo?", deps)

    jev1 = _laudos(sessao, BatchJev.JEV1)
    assert [laudo.pergunta for laudo in jev1] == [*SINAIS, "tom"]
    assert len(jev1) == 12
    assert {laudo.origem for laudo in _laudos(sessao, BatchJev.JEV2) + jev1} == {ORIGEM_UMA_CHAMADA}
    assert next(laudo for laudo in jev1 if laudo.pergunta == "tom").valor == "impaciente"
    assert (jev1[0].probabilidade, jev1[0].confianca) == (1.0, 1.0)
    [acao] = _laudos(sessao, BatchJev.JEV2)
    assert (acao.pergunta, acao.valor) == ("acao", "INFORM_DEBT")


def test_a_palavra_chave_que_bateu_vira_laudo_do_jev1(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto()])

    cliente("vou procurar um advogado", deps)

    palavra = next(
        laudo for laudo in _laudos(sessao, BatchJev.JEV1) if laudo.pergunta == "palavra_chave"
    )
    assert (palavra.valor, palavra.probabilidade, palavra.confianca) == ("advogado", 1.0, 1.0)
    transicao = sessao.scalars(select(Transicao).order_by(Transicao.id.desc())).first()
    assert sessao.get(LaudoGravado, transicao.laudo_id).pergunta == "palavra_chave"


def test_o_sinal_que_levou_ao_humano_e_o_laudo_da_transicao_e_da_nota(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(contesta_divida=True)])

    cliente("eu não devo isso", deps)

    transicao = sessao.scalars(select(Transicao).order_by(Transicao.id.desc())).first()
    causa = sessao.get(LaudoGravado, transicao.laudo_id)
    assert (causa.batch, causa.pergunta, causa.valor) == (BatchJev.JEV1, "contesta_divida", True)
    nota = next(c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada")
    assert "CONTESTACAO" in nota and "contesta_divida" in nota


def test_o_sem_resposta_vai_ao_atendente_com_a_etiqueta_e_o_laudo_da_causa(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="ANSWER", sem_resposta=True, reply="")])

    assert cliente("quais meses estão em aberto?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.SEM_RESPOSTA
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert next(c[2] for c in deps.chatwoot.chamadas if c[0] == "etiquetar") == ["sem_resposta"]
    turno = _turno(sessao)
    assert turno.acao_principal == "HUMAN:SEM_RESPOSTA"
    assert (turno.redacao["saida"], turno.redacao["causa"]) == ("HANDOFF", "sinal:sem_resposta")
    transicao = sessao.scalars(select(Transicao).order_by(Transicao.id.desc())).first()
    causa = sessao.get(LaudoGravado, transicao.laudo_id)
    assert (causa.batch, causa.pergunta, causa.valor) == (BatchJev.JEV1, "sem_resposta", True)
    nota = next(c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada")
    assert "SEM_RESPOSTA" in nota and "sem_resposta" in nota
    assert deps.pix.cobrancas == {} and sessao.scalars(select(Pagamento)).all() == []


# Quem quer pagar, paga


def test_r3_depois_da_oferta_quem_aceita_o_desconto_recebe_o_link_no_valor_final(
    sessao, ligada, cliente, politica
):
    ligada.degrau_atual = 1
    deps = _deps(politica, [_bruto(acao="SEND_CTN_LINK", reply=REPLY_LINK_FINAL)])

    assert cliente("Quero pagar com esse desconto", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 90,00" in texto and f"https://ctn.invalid/pagar/{link}" in texto
    assert sessao.scalars(select(Pagamento)).one().valor_centavos == 9000
    assert _turno(sessao).acao_principal == "SEND_CTN_LINK"
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_depois_da_oferta_a_objecao_com_o_link_sai_enviado_e_nao_vai_ao_humano(
    sessao, ligada, cliente, politica
):
    ligada.degrau_atual = 1
    deps = _deps(politica, [_bruto(acao="SEND_CTN_LINK", reply=REPLY_LINK_FINAL)])

    resultado = cliente("Ainda tá caro", deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert ligada.motivo_humano is None
    assert ligada.objecoes_sem_desconto == 0
    assert _turno(sessao).acao_principal == "SEND_CTN_LINK"


def test_o_desconto_sobe_o_degrau_e_manda_o_link_com_o_valor_final(
    sessao, ligada, cliente, politica
):
    ligada.cobranca.propenso_cartao = True
    deps = _deps(politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK)])

    assert cliente("tá caro", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 90,00" in texto and f"https://ctn.invalid/pagar/{link}" in texto
    assert ligada.degrau_atual == 1
    assert ligada.forma_enviada is FormaPagamento.LINK_CTN
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("OFFER_DISCOUNT", ["SEND_CTN_LINK"])
    assert turno.numeros["degrau"] == 1


def test_o_desconto_de_quem_nao_paga_pix_todo_mes_vai_no_pix_de_noventa(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_PIX_COM_DESCONTO)])

    assert cliente("tá caro", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [pix] = deps.pix.cobrancas.values()
    assert "R$ 90,00" in texto and pix.br_code in texto
    assert sessao.scalars(select(Pagamento)).one().valor_centavos == pix.valor_centavos == 9000
    assert deps.ctn.links == {}
    assert ligada.degrau_atual == 1 and ligada.forma_enviada is FormaPagamento.PIX
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("OFFER_DISCOUNT", ["GENERATE_PIX"])


# A resposta (ANSWER)

REPLY_ANSWER_DESCONTO = (
    "O desconto vale no Pix e no link: de {divida} por {valor_final}. É só escolher como pagar."
)
REPLY_ANSWER_SEM_MEIO = "Não dá para parcelar: o valor é {divida}, à vista."
REPLY_ANSWER_COM_PIX = REPLY_ANSWER_SEM_MEIO + "\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"


def test_o_caso_do_desconto_so_no_cartao_a_answer_responde_sem_mandar_ao_atendente(
    sessao, ligada, cliente, politica
):
    """O caso do Cauê: "Tem desconto?" e depois "então esse desconto só existe se eu cadastrar
    meu cartão?". A 2ª é dúvida da cobrança: o Luna responde, sem anexo, e a conversa segue."""
    ligada.cobranca.propenso_cartao = True
    deps = _deps(
        politica,
        [
            _bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK),
            _bruto(acao="ANSWER", reply=REPLY_ANSWER_DESCONTO),
        ],
    )

    assert cliente("Tem desconto?", deps) is ResultadoTurno.ENVIADO
    pagamentos_antes = len(sessao.scalars(select(Pagamento)).all())
    assert (
        cliente("então esse desconto só existe se eu cadastrar meu cartão?", deps)
        is ResultadoTurno.ENVIADO
    )

    [_, segunda] = deps.chatwoot.enviadas(555)
    assert "R$ 90,00" in segunda and "R$ 100,20" in segunda
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("ANSWER", [])
    assert turno.texto_enviado == segunda and turno.redacao["saida"] == "LLM"
    assert ligada.degrau_atual == 1
    assert len(sessao.scalars(select(Pagamento)).all()) == pagamentos_antes == 1
    assert len(deps.ctn.links) == 1 and deps.pix.cobrancas == {}
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert ligada.motivo_humano is None
    assert EstadoConversa.ATENDIMENTO_HUMANO not in {
        t.para for t in sessao.scalars(select(Transicao)).all()
    }
    mensagens = _do_agente(sessao)
    assert len(mensagens) == 2 and all(m.conta_no_teto for m in mensagens)
    assert mensagens[1].texto == segunda
    entrada = json.loads(deps.llm_turno.pedidos[1][1])
    assert entrada["meio_vigente"] == "LINK_CTN"


def test_a_1a_resposta_por_answer_sem_meio_enviado_leva_o_pix_e_cria_o_pagamento(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="ANSWER", reply=REPLY_ANSWER_COM_PIX)])

    assert cliente("dá pra parcelar?", deps) is ResultadoTurno.ENVIADO

    [pix] = deps.pix.cobrancas.values()
    [texto] = deps.chatwoot.enviadas(555)
    assert "R$ 100,20" in texto and pix.br_code in texto
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("ANSWER", ["GENERATE_PIX"])
    assert sessao.scalars(select(Pagamento)).one().forma is FormaPagamento.PIX
    assert ligada.forma_enviada is FormaPagamento.PIX
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_a_1a_resposta_por_answer_do_propenso_leva_o_link(sessao, ligada, cliente, politica):
    ligada.cobranca.propenso_cartao = True
    reply = "Não dá para parcelar: o valor é {divida}, à vista. Pague e cadastre o cartão: "
    deps = _deps(politica, [_bruto(acao="ANSWER", reply=reply + "{link_ctn}")])

    assert cliente("dá pra parcelar?", deps) is ResultadoTurno.ENVIADO

    [link] = deps.ctn.links
    [texto] = deps.chatwoot.enviadas(555)
    assert f"https://ctn.invalid/pagar/{link}" in texto
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("ANSWER", ["SEND_CTN_LINK"])
    assert sessao.scalars(select(Pagamento)).one().forma is FormaPagamento.LINK_CTN


def test_a_answer_sem_a_chave_do_anexo_sai_com_a_linha_do_pix_escrita_pelo_codigo(
    sessao, ligada, cliente, politica
):
    sem_pix = _bruto(acao="ANSWER", reply=REPLY_ANSWER_SEM_MEIO)
    deps = _deps(politica, [sem_pix])

    assert cliente("dá pra parcelar?", deps) is ResultadoTurno.ENVIADO

    turno = _turno(sessao)
    [primeira] = turno.redacao["tentativas"]
    assert primeira["reprovadas"] == []
    assert len(deps.llm_turno.pedidos) == 1
    [pix] = deps.pix.cobrancas.values()
    assert pix.br_code in deps.chatwoot.enviadas(555)[0]


def test_a_answer_com_link_sem_anexo_reprovada_2_vezes_vai_ao_atendente_por_validacao(
    sessao, ligada, cliente, politica
):
    ruim = _bruto(acao="ANSWER", reply="Aqui está o link: {link_ctn}")
    deps = _deps(politica, [ruim, ruim])

    assert cliente("dá pra parcelar?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.VALIDACAO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    turno = _turno(sessao)
    assert turno.acao_principal == "NAO_EXECUTADA:ANSWER"
    assert "marcador_sem_valor:link_ctn" in turno.redacao["tentativas"][0]["reprovadas"]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert _do_agente(sessao) == []


# O handoff


def test_o_pedido_de_humano_vai_ao_canal_externo_com_o_motivo_novo(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(quer_humano=True)])

    cliente("quero falar com uma pessoa", deps)

    assert ligada.motivo_humano is MotivoHumano.PEDIU_HUMANO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert next(c[2] for c in deps.chatwoot.chamadas if c[0] == "etiquetar") == ["pediu_humano"]
    assert _turno(sessao).acao_principal == "HUMAN:PEDIU_HUMANO"


@pytest.mark.parametrize(
    "texto",
    [
        "você é humano?",
        "quero falar com um atendente",
        "falar com alguém",
        "como é o processo de pagamento?",
    ],
)
def test_o_pedido_de_humano_e_o_processo_de_pagamento_chegam_ao_luna(
    sessao, ligada, cliente, politica, texto
):
    deps = _deps(politica, [_bruto()])

    assert cliente(texto, deps) is ResultadoTurno.ENVIADO

    assert len(deps.llm_turno.pedidos) == 1
    assert ligada.motivo_humano is None


def test_quem_pede_um_atendente_e_o_luna_marca_quer_humano_termina_em_pediu_humano(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(quer_humano=True, acao="NONE", reply="")])

    assert cliente("quero um atendente", deps) is ResultadoTurno.HANDOFF

    assert len(deps.llm_turno.pedidos) == 1
    assert ligada.motivo_humano is MotivoHumano.PEDIU_HUMANO
    assert _turno(sessao).redacao["causa"] == "sinal:quer_humano"
    assert _turno(sessao).acao_principal == "HUMAN:PEDIU_HUMANO"


@pytest.mark.parametrize("texto", ["vou no Procon", "meu advogado", "vou processar vocês"])
def test_o_juridico_vai_ao_atendente_sem_chamar_o_luna(sessao, ligada, cliente, politica, texto):
    deps = _deps(politica, [_bruto()])

    assert cliente(texto, deps) is ResultadoTurno.HANDOFF

    assert deps.llm_turno.pedidos == []
    assert ligada.motivo_humano is MotivoHumano.JURIDICO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).redacao["causa"].startswith("palavra_chave:")


# A D44: golpe fica no WhatsApp, com a fixa do golpe


def test_desconfia_golpe_fica_no_whatsapp_com_a_fixa_do_golpe(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(desconfia_golpe=True)])

    assert cliente("como sei que é mesmo de vocês?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.DESCONFIANCA
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_humano_no_whatsapp]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert deps.ctn.links == {} and deps.pix.cobrancas == {}


def test_quer_retencao_com_desconfia_golpe_leva_retencao_e_o_canal_externo_sem_botao(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(quer_retencao=True, desconfia_golpe=True)])

    cliente("quero cancelar, isso é uma fraude", deps)

    assert ligada.motivo_humano is MotivoHumano.RETENCAO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert deps.chatwoot.chamadas[-1][3] == []


def test_a_palavra_chave_vai_ao_atendente_sem_chamar_o_luna(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto()])

    assert cliente("isso é golpe?", deps) is ResultadoTurno.HANDOFF

    assert deps.llm_turno.pedidos == []
    assert ligada.motivo_humano is MotivoHumano.DESCONFIANCA
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_humano_no_whatsapp]
    assert deps.chatwoot.chamadas[-1][3] == []
    golpe = next(
        laudo for laudo in _laudos(sessao, BatchJev.JEV1) if laudo.pergunta != "palavra_chave"
    )
    assert (golpe.pergunta, golpe.valor) == ("desconfia_golpe", True)
    assert _turno(sessao).redacao["causa"] == "palavra_chave:golpe"


# As falhas


def test_o_llm_fora_sem_palavra_chave_espera_e_tenta_de_novo_sem_handoff(
    sessao, ligada, cliente, politica
):
    # AUD-03: a falha passageira do Luna não vai ao humano na hora.
    deps = _deps(politica, [LLMTurnoIndisponivel("fora do ar")])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ERRO

    assert ligada.estado is not EstadoConversa.ATENDIMENTO_HUMANO and ligada.falhas_turno == 1
    assert ligada.turno_em is not None
    assert deps.chatwoot.enviadas(555) == []
    assert _turno(sessao).redacao["erro"] == "LLMTurnoIndisponivel"


# Duas tentativas reprovadas


def _reprovada(acao="INFORM_DEBT"):
    return _bruto(acao=acao, reply="Fica R$ 70 no Pix {pix_copia_e_cola}")


def test_duas_reprovadas_levam_ao_atendente_por_validacao_sem_nada_gravado(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_reprovada(), _reprovada()])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.VALIDACAO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    turno = _turno(sessao)
    assert turno.redacao["saida"] == "HANDOFF" and len(turno.redacao["tentativas"]) == 2
    assert turno.acao_principal == "NAO_EXECUTADA:INFORM_DEBT"
    assert turno.numeros == {"executado": False}
    assert sessao.scalars(select(Pagamento)).all() == []
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert (ligada.degrau_atual, ligada.forma_enviada) == (0, None)
    assert _do_agente(sessao) == []


def test_o_desconto_reprovado_nao_gasta_o_degrau(sessao, ligada, cliente, politica):
    ruim = _bruto(acao="OFFER_DISCOUNT", reply="Fica R$ 70 no link {link_ctn}")
    deps = _deps(politica, [ruim, ruim])

    cliente("tá caro", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert ligada.degrau_atual == 0
    assert _turno(sessao).acao_principal == "NAO_EXECUTADA:OFFER_DISCOUNT"


# Sem envio e o terceiro


def test_none_fecha_sem_envio_e_o_iniciado_vai_a_engajado(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(acao="NONE", reply=REPLY_NONE)])

    assert cliente("ok", deps) is ResultadoTurno.SEM_ENVIO

    assert deps.chatwoot.enviadas(555) == [] and _do_agente(sessao) == []
    assert ligada.estado is EstadoConversa.ENGAJADO
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.numeros) == ("NONE", {})
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}


def test_none_vazio_fora_da_cortesia_vai_ao_atendente_sem_resposta(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="NONE", reply="")])

    assert cliente("to cansado disso tudo", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.SEM_RESPOSTA
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).acao_principal == "HUMAN:SEM_RESPOSTA"


def test_o_terceiro_recebe_a_frase_fixa_sem_o_texto_do_luna(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(reply="Texto do Luna que não pode sair.", terceiro=True)])

    assert cliente("quem fala é o filho dele", deps) is ResultadoTurno.ENVIADO

    frase = POLITICAS[Situacao.TERCEIRO].texto
    assert deps.chatwoot.enviadas(555) == [frase]
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (frase, True)
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.texto_enviado) == ("TERCEIRO", frase)
    assert turno.redacao["saida"] == "FRASE_FIXA" and turno.numeros == {}
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert sessao.scalars(select(Pagamento)).all() == []


REPLY_FORA = (
    "Por aqui eu só trato da sua mensalidade de {divida}.\n"
    "Para pagar, use o Pix CNPJ: {pix_copia_e_cola}"
)


def test_o_fora_do_assunto_na_1a_resposta_sai_com_o_texto_do_luna_e_o_pix(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="OUT_OF_SCOPE", reply=REPLY_FORA)])

    assert cliente("qual o horário da clínica?", deps) is ResultadoTurno.ENVIADO

    [pix] = deps.pix.cobrancas.values()
    [texto] = deps.chatwoot.enviadas(555)
    assert texto.startswith("Por aqui eu só trato da sua mensalidade de R$ 100,20.")
    assert pix.br_code in texto
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("OUT_OF_SCOPE", ["GENERATE_PIX"])
    assert turno.redacao["saida"] == "LLM" and turno.texto_enviado == texto
    assert sessao.scalars(select(Pagamento)).one().referencia == pix.txid
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_o_fora_do_assunto_com_o_pix_vigente_sai_sem_repetir_o_pix(
    sessao, ligada, cliente, politica
):
    reply = "Por aqui eu só trato da sua mensalidade. O Pix que enviei continua valendo."
    deps = _deps(politica, [_bruto(), _bruto(acao="OUT_OF_SCOPE", reply=reply)])
    cliente("quanto eu devo?", deps)

    assert cliente("qual o horário da clínica?", deps) is ResultadoTurno.ENVIADO

    [_, segunda] = deps.chatwoot.enviadas(555)
    assert segunda == reply
    assert len(sessao.scalars(select(Pagamento)).all()) == 1
    assert (_turno(sessao).acao_principal, _turno(sessao).acoes_anexadas) == ("OUT_OF_SCOPE", [])


def test_o_fora_do_assunto_reprovado_duas_vezes_sai_com_a_frase_fixa_sem_anexo_nem_handoff(
    sessao, ligada, cliente, politica
):
    ruim = _bruto(acao="OUT_OF_SCOPE", reply="A clínica abre às 8h.")
    deps = _deps(politica, [ruim, ruim])

    assert cliente("qual o horário da clínica?", deps) is ResultadoTurno.ENVIADO

    frase = POLITICAS[Situacao.FORA_DO_ASSUNTO].texto
    assert deps.chatwoot.enviadas(555) == [frase]
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (frase, True)
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("OUT_OF_SCOPE", [])
    assert (turno.resultado, turno.texto_enviado) == (ResultadoTurno.ENVIADO, frase)
    assert turno.redacao["saida"] == "FRASE_FIXA" and turno.numeros == {}
    assert len(turno.redacao["tentativas"]) == 2
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert sessao.scalars(select(Pagamento)).all() == []
    assert ligada.motivo_humano is None and ligada.estado is not EstadoConversa.ATENDIMENTO_HUMANO


# O estado


def test_com_link_ou_pix_o_estado_aguarda_o_pagamento_e_nunca_vai_a_em_risco(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(tom="impaciente")])

    cliente("e aí, quanto é?", deps)

    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    estados = {t.para for t in sessao.scalars(select(Transicao)).all()}
    assert EstadoConversa.EM_RISCO not in estados


# A concorrência


def test_o_pagamento_durante_a_chamada_ao_llm_leva_a_ignorado_e_nada_sai(
    sessao, ligada, cliente, politica
):
    llm = LLMQueEspera(respostas=[_bruto()], registrar=True)
    llm.ao_chamar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, llm=llm)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.chamadas == []
    assert sessao.scalars(select(Pagamento)).all() == []
    assert deps.pix.cobrancas == {}
    assert _do_agente(sessao) == [] and sessao.scalars(select(Transicao)).all() == []
    turno = _turno(sessao)
    assert (turno.resultado, turno.texto_enviado) == (ResultadoTurno.IGNORADO, None)


def test_o_pagamento_durante_o_humano_nao_manda_o_canal_externo(sessao, ligada, cliente, politica):
    llm = LLMQueEspera(respostas=[_bruto(contesta_divida=True)], registrar=True)
    llm.ao_chamar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, llm=llm)

    assert cliente("eu não devo", deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.chamadas == []


def test_o_pagamento_durante_a_geracao_do_link_desfaz_a_acao_e_nada_sai(
    sessao, ligada, cliente, politica
):
    ligada.cobranca.propenso_cartao = True
    ctn = CTNQueEspera([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)])
    ctn.ao_gerar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK)], ctn=ctn)

    assert cliente("tá caro", deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.chamadas == []
    assert sessao.scalars(select(Pagamento)).all() == []
    assert _do_agente(sessao) == []
    turno = _turno(sessao)
    assert turno.acao_principal == "NAO_EXECUTADA:OFFER_DISCOUNT"
    assert turno.numeros == {"executado": False}
    # O degrau e a forma do savepoint voltaram junto com o Pagamento.
    assert (ligada.degrau_atual, ligada.forma_enviada) == (0, None)
    assert ligada.estado is EstadoConversa.INICIADO


# O teto e o recebedor


def test_com_5_no_teto_sai_a_6a_fixa_e_o_llm_nao_e_chamado(sessao, ligada, cliente, politica):
    # Só a 1ª resposta leva o Pix: dali em diante ele já vale e o texto vai sem ele.
    sem_pix = _bruto(reply=REPLY_SO_A_DIVIDA)
    deps = _deps(politica, [_bruto(), *[sem_pix] * (politica.teto_resolucao - 1)])
    for _ in range(politica.teto_resolucao):
        assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO
    chamadas = len(deps.llm_turno.pedidos)

    assert cliente("e agora?", deps) is ResultadoTurno.FIXA

    assert len(deps.llm_turno.pedidos) == chamadas
    assert deps.chatwoot.enviadas(555)[-1] == TEXTO_CANAL_EXTERNO
    assert ligada.motivo_humano is MotivoHumano.TETO


def test_com_o_recebedor_preenchido_a_frase_vai_no_fim_do_pix(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto()])
    deps.politica = deps.politica.model_copy(update={"pix_recebedor": "Cartão de Todos"})

    cliente("quanto eu devo?", deps)

    [texto] = deps.chatwoot.enviadas(555)
    assert texto.endswith("confira que o recebedor é Cartão de Todos.")
    assert _turno(sessao).texto_enviado == texto


# O botão "Pagar com Pix" (D26, D59): a mensagem fixa, sem o Luna

TOQUE = BOTAO_PAGAR_PIX.texto


def test_o_botao_manda_o_pix_fixo_sem_chamar_o_luna(sessao, ligada, cliente, politica):
    deps = _deps(politica)

    assert cliente(TOQUE, deps) is ResultadoTurno.ENVIADO

    assert deps.llm_turno.pedidos == []
    [pix] = deps.pix.cobrancas.values()
    [texto] = deps.chatwoot.enviadas(555)
    assert pix.br_code in texto
    assert sessao.scalars(select(Pagamento)).one().referencia == pix.txid
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("GENERATE_PIX", ResultadoTurno.ENVIADO)
    assert turno.redacao["saida"] == "FIXA"
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


# O savepoint quando o `executar` recusa o que a prévia aprovou (M4)

REPLY_DIVERGENTE = "Seu valor é {divida}. Ou pelo link {link_ctn}\nPix CNPJ: {pix_copia_e_cola}"


def test_o_texto_recusado_depois_do_executar_desfaz_a_acao_e_grava_o_motivo(
    sessao, ligada, cliente, politica, monkeypatch
):
    # A prévia falsa promete um link que o `executar` não gera para INFORM_DEBT + Pix.
    previa = orquestrador.previa_numeros

    def previa_que_promete_o_link(acoes, conversa, cobranca, politica, agora):
        return replace(previa(acoes, conversa, cobranca, politica, agora), link_ctn="https://x")

    monkeypatch.setattr(orquestrador, "previa_numeros", previa_que_promete_o_link)
    deps = _deps(politica, [_bruto(reply=REPLY_DIVERGENTE)])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert (ligada.degrau_atual, ligada.forma_enviada) == (0, None)
    assert ligada.motivo_humano is MotivoHumano.VALIDACAO
    turno = _turno(sessao)
    assert turno.redacao["interpolacao_reprovada"] == ["marcador_sem_valor:link_ctn"]
    assert turno.redacao["saida"] == "HANDOFF"
    assert (turno.acao_principal, turno.numeros) == (
        "NAO_EXECUTADA:INFORM_DEBT",
        {"executado": False},
    )
    assert _do_agente(sessao) == []


# A passagem ao atendente: o `reply` do Luna no lugar da frase fixa

PASSAGEM = "Entendi, Ana. Um atendente da unidade vai conferir o seu pagamento, aqui mesmo."


def test_ja_paguei_com_passagem_valida_chega_ao_cliente_fora_do_teto(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(alega_pagamento=True, acao="NONE", reply=PASSAGEM)])

    assert cliente("já paguei", deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == [PASSAGEM]
    assert ligada.motivo_humano is MotivoHumano.CONCILIACAO
    assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
    turno = _turno(sessao)
    assert turno.redacao["passagem"] == "LLM" and turno.redacao["saida"] == "HANDOFF"
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (PASSAGEM, False)
    assert len(deps.llm_turno.pedidos) == 1


def test_passagem_com_digito_cai_na_fixa_sem_segunda_chamada(sessao, ligada, cliente, politica):
    ruim = _bruto(alega_pagamento=True, acao="NONE", reply="Um atendente confere os 100 reais.")
    deps = _deps(politica, [ruim, _bruto()])

    cliente("já paguei", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).redacao["passagem"] == "FIXA"
    assert _turno(sessao).redacao["tentativas"][0]["reprovadas"] == ["valor_em_digitos"]
    assert len(deps.llm_turno.pedidos) == 1
    [mensagem] = sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA))
    assert mensagem.conta_no_teto is False


@pytest.mark.parametrize(
    ("sinal", "fixa"),
    [("risco_juridico", TEXTO_CANAL_EXTERNO), ("desconfia_golpe", None)],
)
def test_juridico_e_golpe_mandam_a_fixa_mesmo_com_passagem_valida(
    sessao, ligada, cliente, politica, sinal, fixa
):
    # Sem palavra-chave na mensagem, o caminho é o sinal do Luna.
    deps = _deps(politica, [_bruto(**{sinal: True}, acao="NONE", reply=PASSAGEM)])

    cliente("não tenho certeza disso", deps)

    assert deps.chatwoot.enviadas(555) == [fixa or deps.politica.texto_humano_no_whatsapp]
    assert _turno(sessao).redacao["passagem"] == "FIXA"
    assert _do_agente(sessao) == []


def test_none_fora_da_cortesia_segue_com_a_fixa(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(acao="NONE", reply=PASSAGEM)])

    assert cliente("to cansado disso tudo", deps) is ResultadoTurno.HANDOFF

    # O NONE não é motivo de passagem do Luna: o `reply` não vale, sai a fixa.
    assert ligada.motivo_humano is MotivoHumano.SEM_RESPOSTA
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def test_a_palavra_chave_nao_leva_passagem(sessao, ligada, cliente, politica):
    deps = _deps(politica, [])

    cliente("vou chamar meu advogado", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).redacao["passagem"] == "FIXA"


def test_foto_sem_texto_recebe_a_frase_do_arquivo_como_fixa_fora_do_teto(sessao, ligada, politica):
    registrar_mensagem(
        sessao,
        ligada.id,
        OrigemMensagem.CLIENTE,
        "",
        "cw-foto",
        False,
        SEGUNDA_10H,
        anexos=[{"tipo": "image"}],
    )
    deps = _deps(politica, [])

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == [TEXTO_ARQUIVO_RECEBIDO]
    assert ligada.motivo_humano is MotivoHumano.CONCILIACAO
    assert deps.llm_turno.pedidos == []
    [mensagem] = sessao.scalars(select(Mensagem).where(Mensagem.origem == OrigemMensagem.FIXA))
    assert (mensagem.texto, mensagem.conta_no_teto) == (TEXTO_ARQUIVO_RECEBIDO, False)
