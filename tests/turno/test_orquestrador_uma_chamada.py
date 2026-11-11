"""O turno de uma chamada ligado ao orquestrador, de ponta a ponta com as falsas (D47, E1.5).

O Jev não existe neste caminho: o que levanta se for chamado prova que o desvio não o toca.
"""

import json
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.db.historico import registrar_mensagem
from agente.db.modelos import LaudoGravado, Mensagem, Pagamento, Transicao, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.integracoes.llm_turno import LLMTurnoFalso, LLMTurnoIndisponivel
from agente.integracoes.pix import PixFalso
from agente.integracoes.redator import RedatorFalso
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    FormaPagamento,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno import orquestrador
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, TEXTO_RESPOSTA_SEGURA
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno
from agente.turno.uma_chamada import ORIGEM_UMA_CHAMADA, SINAIS

from .test_orquestrador_gates import SEGUNDA_10H
from .test_orquestrador_pagamento_concorrente import _pagar_por_fora

REPLY_PIX = "Seu valor em aberto é {divida}. Para pagar no Pix, use o código: {pix_copia_e_cola}"
REPLY_LINK = "Seu valor com desconto é {valor_final}. Pague e cadastre o cartão: {link_ctn}"
REPLY_LINK_FINAL = "Fechado! Segue o link com o valor final de {valor_final}: {link_ctn}"
REPLY_NONE = ""
REPLY_TERCEIRO = "Oi! Acho que você não é a pessoa com quem eu queria falar. Obrigado pela atenção."
HANDOFF = "Entendi, você falou do desemprego. Passei a conversa para a nossa equipe."


class JevProibido:
    """O Jev que levanta se for chamado: o turno de uma chamada não o usa."""

    origem = "proibido"

    def __init__(self):
        self.chamadas = 0

    def avaliar(self, perguntas, estado):
        self.chamadas += 1
        raise AssertionError("o Jev não deve ser chamado no turno de uma chamada")


@dataclass
class LLMQueEspera(LLMTurnoFalso):
    """Enquanto o LLM responde, o pagamento chega."""

    ao_chamar: object = None

    def completar_json(self, sistema, usuario):
        if self.ao_chamar is not None:
            self.ao_chamar()
            self.ao_chamar = None
        return super().completar_json(sistema, usuario)


class CTNQueEspera(CTNFalso):
    """Enquanto o link é gerado, o pagamento chega."""

    ao_gerar = None

    def gerar_link_pagamento(self, *args, **kwargs):
        if self.ao_gerar is not None:
            self.ao_gerar()
            self.ao_gerar = None
        return super().gerar_link_pagamento(*args, **kwargs)


def _bruto(**mudancas) -> dict:
    bruto = {sinal: False for sinal in SINAIS}
    bruto |= {"tom": "neutro", "acao": "INFORM_DEBT", "reply": REPLY_PIX, "handoff_reply": ""}
    return bruto | mudancas


def _deps(politica, respostas=(), *, llm=None, modo=Modo.ATIVO, ctn=None, **mudancas):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=JevProibido(),
        ctn=ctn or CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)]),
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        redator=RedatorFalso(registrar=True),
        llm_turno=llm or LLMTurnoFalso(respostas=list(respostas), registrar=True),
        **mudancas,
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


class Cliente:
    """Manda uma mensagem do cliente e roda o turno, um minuto depois do anterior."""

    def __init__(self, sessao, conversa):
        self.sessao, self.conversa, self.n = sessao, conversa, 0

    def __call__(self, texto, deps):
        self.n += 1
        agora = SEGUNDA_10H + timedelta(minutes=self.n)
        registrar_mensagem(
            self.sessao,
            self.conversa.id,
            OrigemMensagem.CLIENTE,
            texto,
            f"cw-{self.n}",
            False,
            agora,
        )
        return processar_turno(self.sessao, self.conversa.id, agora, deps)


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


def test_com_llm_turno_o_jev_nao_e_chamado_e_o_turno_fecha_enviado(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto()])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.jev.chamadas == 0
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


def test_sem_llm_turno_o_jev_e_chamado_como_hoje(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    deps.llm_turno = None
    deps.jev = JevFalso([*aprovar_tudo()], registrar=True)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.jev.chamadas
    assert _turno(sessao).redacao.get("caminho") is None


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
    assert len(jev1) == 11
    assert {laudo.origem for laudo in _laudos(sessao, BatchJev.JEV2) + jev1} == {ORIGEM_UMA_CHAMADA}
    assert next(laudo for laudo in jev1 if laudo.pergunta == "tom").valor == "impaciente"
    assert (jev1[0].probabilidade, jev1[0].confianca) == (1.0, 1.0)
    [acao] = _laudos(sessao, BatchJev.JEV2)
    assert (acao.pergunta, acao.valor) == ("acao", "INFORM_DEBT")
    assert _laudos(sessao, BatchJev.JEV3) == []


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


# Quem quer pagar, paga


def test_r3_no_degrau_3_quem_aceita_o_desconto_recebe_o_link_no_valor_do_degrau(
    sessao, ligada, cliente, politica
):
    ligada.degrau_atual = 3
    deps = _deps(politica, [_bruto(acao="SEND_CTN_LINK", reply=REPLY_LINK_FINAL)])

    assert cliente("Quero pagar com esse desconto", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 10,20" in texto and f"https://ctn.invalid/pagar/{link}" in texto
    assert sessao.scalars(select(Pagamento)).one().valor_centavos == 1020
    assert _turno(sessao).acao_principal == "SEND_CTN_LINK"
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_no_ultimo_degrau_a_objecao_com_o_link_sai_enviado_e_nao_vai_ao_humano(
    sessao, ligada, cliente, politica
):
    ligada.degrau_atual = 3
    deps = _deps(politica, [_bruto(acao="SEND_CTN_LINK", reply=REPLY_LINK_FINAL)])

    resultado = cliente("Ainda tá caro", deps)

    assert resultado is ResultadoTurno.ENVIADO
    assert ligada.motivo_humano is None
    assert ligada.objecoes_sem_desconto == 0
    assert _turno(sessao).acao_principal == "SEND_CTN_LINK"


def test_o_desconto_sobe_o_degrau_e_manda_o_link_com_o_valor_final(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(acao="OFFER_DISCOUNT", reply=REPLY_LINK)])

    assert cliente("tá caro", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 70,20" in texto and f"https://ctn.invalid/pagar/{link}" in texto
    assert ligada.degrau_atual == 1
    assert ligada.forma_enviada is FormaPagamento.LINK_CTN
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("OFFER_DISCOUNT", ["SEND_CTN_LINK"])
    assert turno.numeros["degrau"] == 1


# O texto do handoff


def test_o_texto_valido_do_handoff_e_a_mensagem_ao_cliente_e_o_botao_continua(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(vulnerabilidade=True, handoff_reply=HANDOFF)])

    assert cliente("perdi o emprego", deps) is ResultadoTurno.HANDOFF

    assert deps.chatwoot.enviadas(555) == [f"{HANDOFF}\n\n{TEXTO_CANAL_EXTERNO}"]
    [botao] = deps.chatwoot.chamadas[-1][3]
    assert botao.valor == deps.politica.link_canal_externo
    assert ligada.motivo_humano is MotivoHumano.CAUTELA
    turno = _turno(sessao)
    assert (turno.resultado, turno.acao_principal) == (ResultadoTurno.HANDOFF, "HUMAN:CAUTELA")
    assert turno.redacao["saida"] == "HANDOFF" and turno.redacao["causa"] == "sinal:vulnerabilidade"
    assert turno.numeros == {}
    assert _do_agente(sessao) == []


def test_o_texto_invalido_do_handoff_leva_a_fixa_de_hoje(sessao, ligada, cliente, politica):
    deps = _deps(
        politica,
        [_bruto(vulnerabilidade=True, handoff_reply="Ligue para 0800 123 e fale conosco.")],
    )

    cliente("perdi o emprego", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]


def test_o_pedido_de_humano_vai_ao_canal_externo_com_o_motivo_novo(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(quer_humano=True, handoff_reply=HANDOFF)])

    cliente("quero falar com uma pessoa", deps)

    assert ligada.motivo_humano is MotivoHumano.PEDIU_HUMANO
    assert deps.chatwoot.enviadas(555) == [f"{HANDOFF}\n\n{TEXTO_CANAL_EXTERNO}"]
    assert next(c[2] for c in deps.chatwoot.chamadas if c[0] == "etiquetar") == ["pediu_humano"]
    assert _turno(sessao).acao_principal == "HUMAN:PEDIU_HUMANO"


# A D44: golpe nunca recebe link


def test_desconfia_golpe_fica_no_whatsapp_sem_link(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(desconfia_golpe=True, handoff_reply=HANDOFF)])

    assert cliente("isso parece golpe, como sei que é de vocês?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.DESCONFIANCA
    assert deps.chatwoot.enviadas(555) == [HANDOFF]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert deps.ctn.links == {} and deps.pix.cobrancas == {}


def test_quer_retencao_com_desconfia_golpe_leva_retencao_e_tambem_sem_link(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto(quer_retencao=True, desconfia_golpe=True)])

    cliente("quero cancelar, isso é uma fraude", deps)

    assert ligada.motivo_humano is MotivoHumano.RETENCAO
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_humano_no_whatsapp]
    assert deps.chatwoot.chamadas[-1][3] == []


def test_golpe_na_mensagem_com_o_llm_dizendo_nao_vai_sem_link(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto()])

    assert cliente("isso é golpe?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.DESCONFIANCA
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_humano_no_whatsapp]
    assert deps.chatwoot.chamadas[-1][3] == []
    assert len(deps.llm_turno.pedidos) == 1


def test_procon_e_golpe_com_o_llm_fora_vai_sem_link(sessao, ligada, cliente, politica):
    deps = _deps(politica, [LLMTurnoIndisponivel("fora do ar")])

    assert cliente("vou no Procon, isso é golpe", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.DESCONFIANCA
    assert deps.chatwoot.enviadas(555) == [deps.politica.texto_humano_no_whatsapp]
    assert deps.chatwoot.chamadas[-1][3] == []
    turno = _turno(sessao)
    assert turno.redacao["causa"] == "palavra_chave:golpe" and "erro" in turno.redacao


# As falhas


def test_a_palavra_chave_com_o_llm_fora_leva_o_handoff_com_a_fixa(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [LLMTurnoIndisponivel("fora do ar")])

    assert cliente("vou chamar meu advogado", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.JURIDICO
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).acao_principal == "HUMAN:JURIDICO"


def test_o_llm_fora_sem_palavra_chave_leva_redator_indisponivel(sessao, ligada, cliente, politica):
    deps = _deps(politica, [LLMTurnoIndisponivel("fora do ar")])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.REDATOR_INDISPONIVEL
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert _laudos(sessao, BatchJev.JEV1) == []
    turno = _turno(sessao)
    assert turno.acao_principal == "HUMAN:REDATOR_INDISPONIVEL" and "erro" in turno.redacao


# Duas tentativas reprovadas


def _reprovada(acao="INFORM_DEBT"):
    return _bruto(acao=acao, reply="Fica R$ 70 no Pix {pix_copia_e_cola}")


def test_duas_reprovadas_dao_a_resposta_segura_e_depois_o_humano_sem_nada_gravado(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_reprovada(), _reprovada(), _reprovada(), _reprovada()])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    turno = _turno(sessao)
    assert turno.texto_enviado == TEXTO_RESPOSTA_SEGURA
    assert turno.redacao["saida"] == "RESPOSTA_SEGURA" and len(turno.redacao["tentativas"]) == 2
    assert turno.acao_principal == "NAO_EXECUTADA:INFORM_DEBT"
    assert turno.numeros == {"executado": False}
    assert sessao.scalars(select(Pagamento)).all() == []
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert (ligada.degrau_atual, ligada.forma_enviada, ligada.estado) == (
        0,
        None,
        EstadoConversa.INICIADO,
    )
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (TEXTO_RESPOSTA_SEGURA, True)

    assert cliente("e aí?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.VALIDACAO
    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA, TEXTO_CANAL_EXTERNO]
    assert _turno(sessao).numeros == {"executado": False}


def test_o_desconto_reprovado_nao_gasta_o_degrau(sessao, ligada, cliente, politica):
    ruim = _bruto(acao="OFFER_DISCOUNT", reply="Fica R$ 70 no link {link_ctn}")
    deps = _deps(politica, [ruim, ruim])

    cliente("tá caro", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    assert ligada.degrau_atual == 0
    assert _turno(sessao).acao_principal == "NAO_EXECUTADA:OFFER_DISCOUNT"


def test_o_json_invalido_duas_vezes_e_nada_executado_sem_acao(sessao, ligada, cliente, politica):
    deps = _deps(politica, ["isso não é json", "{ainda não}"])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    assert _turno(sessao).acao_principal == "NAO_EXECUTADA:SEM_ACAO"
    assert _laudos(sessao, BatchJev.JEV1) == [] and _laudos(sessao, BatchJev.JEV2) == []


# Sem envio e o terceiro


def test_none_fecha_sem_envio_e_o_iniciado_vai_a_engajado(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(acao="NONE", reply=REPLY_NONE)])

    assert cliente("ok", deps) is ResultadoTurno.SEM_ENVIO

    assert deps.chatwoot.enviadas(555) == [] and _do_agente(sessao) == []
    assert ligada.estado is EstadoConversa.ENGAJADO
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.numeros) == ("NONE", {})
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}


def test_o_terceiro_recebe_o_texto_sem_valor_nem_link_nem_pix(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(terceiro=True, reply=REPLY_TERCEIRO)])

    assert cliente("não sou a Maria, é outra pessoa", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [REPLY_TERCEIRO]
    turno = _turno(sessao)
    assert turno.acao_principal == "TERCEIRO" and turno.acoes_anexadas == []
    assert turno.redacao["causa"] == "sinal:terceiro"
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert sessao.scalars(select(Pagamento)).all() == []
    assert ligada.estado is EstadoConversa.ENGAJADO


def test_o_terceiro_com_o_reply_vazio_nao_envia(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(terceiro=True, reply="")])

    assert cliente("não sou a Maria", deps) is ResultadoTurno.SEM_ENVIO

    assert deps.chatwoot.enviadas(555) == []
    assert _turno(sessao).acao_principal == "TERCEIRO"


# O acolhimento do NONE (D47, decisão 18)

ACOLHIMENTO = "Poxa, sinto muito que esteja assim. O que aconteceu? Me conta como posso ajudar."


def test_none_com_texto_acolhe_e_fecha_enviado_com_o_rotulo_none(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(tom="impaciente", acao="NONE", reply=ACOLHIMENTO)])

    assert cliente("to chateado", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [ACOLHIMENTO]
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.texto_enviado) == ("NONE", ACOLHIMENTO)
    assert turno.redacao["saida"] == "LLM" and turno.acoes_anexadas == []
    assert [m.texto for m in _do_agente(sessao)] == [ACOLHIMENTO]


def test_o_acolhimento_nao_grava_pagamento_degrau_forma_nem_link(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(acao="NONE", reply=ACOLHIMENTO)])

    cliente("to chateado", deps)

    assert sessao.scalars(select(Pagamento)).all() == []
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert (ligada.degrau_atual, ligada.forma_enviada) == (0, None)
    assert "R$" not in _turno(sessao).texto_enviado and "http" not in _turno(sessao).texto_enviado


def test_o_acolhimento_leva_o_iniciado_a_engajado(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(acao="NONE", reply=ACOLHIMENTO)])

    cliente("to chateado", deps)

    assert ligada.estado is EstadoConversa.ENGAJADO


def test_o_pagamento_durante_a_chamada_do_acolhimento_leva_a_ignorado(
    sessao, ligada, cliente, politica
):
    llm = LLMQueEspera(respostas=[_bruto(acao="NONE", reply=ACOLHIMENTO)], registrar=True)
    llm.ao_chamar = lambda: _pagar_por_fora(sessao, ligada)
    deps = _deps(politica, llm=llm)

    assert cliente("to chateado", deps) is ResultadoTurno.IGNORADO

    assert deps.chatwoot.chamadas == [] and _do_agente(sessao) == []
    assert _turno(sessao).texto_enviado is None


def test_o_acolhimento_reprovado_duas_vezes_da_a_resposta_segura(sessao, ligada, cliente, politica):
    reprovada = _bruto(acao="NONE", reply="Sinto muito. Sua dívida é {divida}.")
    deps = _deps(politica, [reprovada, reprovada])

    assert cliente("to chateado", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    turno = _turno(sessao)
    assert turno.acao_principal == "NAO_EXECUTADA:NONE"
    assert turno.numeros == {"executado": False}
    assert sessao.scalars(select(Pagamento)).all() == []


def test_acolhimento_reprovado_e_none_vazio_depois_da_a_resposta_segura(
    sessao, ligada, cliente, politica
):
    # M-a: antes, o NONE vazio da 2a tentativa fechava em SEM_ENVIO e o cliente ficava sem resposta.
    reprovada = _bruto(acao="NONE", reply="Sinto muito. Sua dívida é {divida}.")
    deps = _deps(politica, [reprovada, _bruto(acao="NONE", reply="")])

    assert cliente("to chateado", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    turno = _turno(sessao)
    assert turno.redacao["saida"] == "RESPOSTA_SEGURA"
    assert turno.acao_principal == "NAO_EXECUTADA:NONE"
    assert turno.numeros == {"executado": False}


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


# A sombra


def test_em_sombra_grava_os_laudos_e_a_previa_sem_gerar_link_nem_pix(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, [_bruto()], modo=Modo.SOMBRA)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.SOMBRA

    assert deps.chatwoot.chamadas == []
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert sessao.scalars(select(Pagamento)).all() == [] and _do_agente(sessao) == []
    assert len(_laudos(sessao, BatchJev.JEV1)) == 11 and len(_laudos(sessao, BatchJev.JEV2)) == 1
    turno = _turno(sessao)
    assert turno.acao_principal == "INFORM_DEBT" and turno.texto_enviado is None
    assert turno.numeros == {"divida_centavos": 10020, "valor_pix_centavos": 10020}
    assert turno.redacao["saida"] == "SOMBRA" and turno.redacao["caminho"] == "uma_chamada"
    assert ligada.estado is EstadoConversa.INICIADO and ligada.degrau_atual == 0


def test_em_sombra_o_humano_so_grava_o_rotulo(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto(contesta_divida=True)], modo=Modo.SOMBRA)

    assert cliente("eu não devo", deps) is ResultadoTurno.SOMBRA

    assert deps.chatwoot.chamadas == []
    assert _turno(sessao).acao_principal == "HUMAN:CONTESTACAO"


# O teto e o recebedor


def test_com_5_no_teto_sai_a_6a_fixa_e_o_llm_nao_e_chamado(sessao, ligada, cliente, politica):
    deps = _deps(politica, [_bruto()] * politica.teto_resolucao)
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


# O botão "Pagar com Pix" (D26, D59): a mensagem fixa, sem o Luna nem o Jev

TOQUE = BOTAO_PAGAR_PIX.texto


def test_o_botao_com_llm_turno_manda_o_pix_fixo_sem_chamar_o_luna(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica)

    assert cliente(TOQUE, deps) is ResultadoTurno.ENVIADO

    assert deps.jev.chamadas == 0
    assert deps.llm_turno.pedidos == []
    [pix] = deps.pix.cobrancas.values()
    [texto] = deps.chatwoot.enviadas(555)
    assert pix.br_code in texto
    assert sessao.scalars(select(Pagamento)).one().referencia == pix.txid
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("GENERATE_PIX", ResultadoTurno.ENVIADO)
    assert turno.redacao["saida"] == "FIXA" and _laudos(sessao, BatchJev.JEV3) == []
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


# O savepoint quando o `executar` recusa o que a prévia aprovou (M4)

REPLY_DIVERGENTE = "Seu valor é {divida}. Pague no Pix {pix_copia_e_cola} ou pelo link {link_ctn}"


def test_o_texto_recusado_depois_do_executar_desfaz_a_acao_e_grava_o_motivo(
    sessao, ligada, cliente, politica, monkeypatch
):
    # A prévia falsa promete um link que o `executar` não gera para INFORM_DEBT + Pix.
    previa = orquestrador.previa_numeros

    def previa_que_promete_o_link(acoes, conversa, cobranca, politica, agora):
        return replace(previa(acoes, conversa, cobranca, politica, agora), link_ctn="https://x")

    monkeypatch.setattr(orquestrador, "previa_numeros", previa_que_promete_o_link)
    deps = _deps(politica, [_bruto(reply=REPLY_DIVERGENTE)])

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert (ligada.degrau_atual, ligada.forma_enviada) == (0, None)
    assert ligada.estado is EstadoConversa.INICIADO
    turno = _turno(sessao)
    assert turno.redacao["interpolacao_reprovada"] == ["marcador_sem_valor:link_ctn"]
    assert turno.redacao["saida"] == "RESPOSTA_SEGURA"
    assert (turno.acao_principal, turno.numeros) == (
        "NAO_EXECUTADA:INFORM_DEBT",
        {"executado": False},
    )
    [mensagem] = _do_agente(sessao)
    assert mensagem.texto == TEXTO_RESPOSTA_SEGURA
