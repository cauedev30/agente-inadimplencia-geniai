"""O turno ativo completo (tarefa 5.3), de ponta a ponta com as falsas.

Gates, Jev 1, Jev 2, executar, redigir, interpolar, Jev 3, envio e transição de estado.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import LaudoGravado, Mensagem, Pagamento, Transicao, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import ClienteCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso, JevIndisponivel, responder
from agente.integracoes.pix import PixFalso
from agente.integracoes.redator import RedatorFalso, RedatorIndisponivel
from agente.politica import Regua
from agente.simulacao import DependenciasSimulacao, carregar_casos, rodar_simulacao
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    FormaPagamento,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, TEXTO_RESPOSTA_SEGURA
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

from ..conftest import RAIZ
from .test_orquestrador_gates import SEGUNDA_10H

OBJECAO = [responder("objecao_valor", True), responder("acao", "OFFER_DISCOUNT")]
REPROVA_TOM = [responder("tom_adequado", False)]


def _deps(politica, regras=(), redator=None, jev=None):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=Modo.ATIVO,
        jev=jev or JevFalso([*regras, *aprovar_tudo()], registrar=True),
        ctn=CTNFalso([ClienteCTN("M-0001", "Cliente Fictício", "+5583900000001", None)]),
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        redator=redator or RedatorFalso(registrar=True),
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


def test_r3_propenso_o_link_vai_na_1a_mensagem(sessao, ligada, cliente, politica):
    ligada.cobranca.propenso_cartao = True
    deps = _deps(politica)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 100,20" in texto and f"https://ctn.invalid/pagar/{link}" in texto
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (texto, True)
    turno = _turno(sessao)
    assert (turno.acao_principal, turno.acoes_anexadas) == ("INFORM_DEBT", ["SEND_CTN_LINK"])
    assert turno.texto_enviado == texto
    assert turno.redacao["saida"] == "LLM"
    jev3 = sessao.scalars(select(LaudoGravado).where(LaudoGravado.batch == BatchJev.JEV3)).all()
    assert len(jev3) == 8
    assert (ligada.estado, ligada.forma_enviada) == (
        EstadoConversa.AGUARDANDO_PAGAMENTO,
        FormaPagamento.LINK_CTN,
    )


def test_r3_nao_propenso_o_pix_vai_na_1a_e_a_objecao_traz_o_desconto_com_o_link(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica)
    cliente("quanto eu devo?", deps)

    [pix] = deps.pix.cobrancas.values()
    [primeira] = deps.chatwoot.enviadas(555)
    assert pix.br_code in primeira and "R$ 100,20" in primeira
    assert sessao.scalars(select(Pagamento)).one().referencia == pix.txid

    chatwoot = deps.chatwoot
    deps = _deps(politica, OBJECAO)
    deps.chatwoot = chatwoot
    cliente("tá caro", deps)

    _, segunda = deps.chatwoot.enviadas(555)
    [link] = deps.ctn.links
    assert "R$ 70,20" in segunda and f"https://ctn.invalid/pagar/{link}" in segunda
    assert ligada.degrau_atual == 1
    assert ligada.forma_enviada is FormaPagamento.LINK_CTN
    assert len(_do_agente(sessao)) == 2


def test_reprovou_uma_vez_o_llm_reescreve_sabendo_o_que_reprovou(sessao, cliente, politica):
    # A falsa do Jev 3 reprova só a 1ª versão do texto.
    primeira = "Oi! O valor é {divida}, no Pix {pix_copia_e_cola}. Primeira versão."
    jev = JevFalso(
        [
            responder("bate_com_acao", False, se=lambda e: "Primeira" in (e.texto_candidato or "")),
            *aprovar_tudo(),
        ]
    )
    redator = RedatorFalso(respostas=[primeira], registrar=True)
    deps = _deps(politica, redator=redator, jev=jev)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    primeiro, segundo = redator.pedidos
    assert primeiro.reprovadas_antes is None
    assert segundo.reprovadas_antes == ("bate_com_acao",)
    [texto] = deps.chatwoot.enviadas(555)
    assert "Primeira" not in texto and "00020126" in texto
    assert [t["reprovadas"] for t in _turno(sessao).redacao["tentativas"]] == [
        ["bate_com_acao"],
        [],
    ]


def test_o_valor_cru_do_llm_volta_para_a_reescrita_sem_ir_ao_jev3(sessao, cliente, politica):
    redator = RedatorFalso(respostas=["Fica R$ 70 no Pix {pix_copia_e_cola}"], registrar=True)
    deps = _deps(politica, redator=redator)

    cliente("quanto eu devo?", deps)

    assert redator.pedidos[1].reprovadas_antes == ("valor_em_digitos", "marcador_faltando:divida")
    jev3 = [nomes for nomes, _ in deps.jev.chamadas if "bate_com_acao" in nomes]
    assert len(jev3) == 1
    assert "R$ 70 " not in deps.chatwoot.enviadas(555)[0]


def test_duas_reprovacoes_no_jev3_levam_ao_catalogo(sessao, ligada, cliente, politica):
    redator = RedatorFalso(registrar=True)
    deps = _deps(politica, REPROVA_TOM, redator=redator)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    assert [p.reprovadas_antes for p in redator.pedidos] == [None, ("tom_adequado",)]
    [mensagem] = _do_agente(sessao)
    assert (mensagem.texto, mensagem.conta_no_teto) == (TEXTO_RESPOSTA_SEGURA, True)
    turno = _turno(sessao)
    assert turno.texto_enviado == TEXTO_RESPOSTA_SEGURA
    assert turno.redacao["saida"] == "RESPOSTA_SEGURA"
    assert len(turno.redacao["tentativas"]) == 2
    jev3 = sessao.scalars(select(LaudoGravado).where(LaudoGravado.batch == BatchJev.JEV3)).all()
    assert len(jev3) == 16
    # O texto com os números não saiu: nada do backend fica valendo.
    assert sessao.scalars(select(Pagamento)).all() == []
    assert (ligada.forma_enviada, ligada.estado) == (None, EstadoConversa.INICIADO)


def test_o_desconto_reprovado_nao_gasta_o_degrau(sessao, ligada, cliente, politica):
    deps = _deps(politica, [*OBJECAO, *REPROVA_TOM])

    cliente("tá caro", deps)

    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA]
    assert (ligada.degrau_atual, ligada.objecoes_sem_desconto) == (0, 0)


def test_a_resposta_segura_sai_uma_vez_e_depois_vai_para_o_humano(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, REPROVA_TOM)
    cliente("quanto eu devo?", deps)

    resultado = cliente("e aí?", deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert (ligada.estado, ligada.motivo_humano) == (
        EstadoConversa.ATENDIMENTO_HUMANO,
        MotivoHumano.VALIDACAO,
    )
    assert deps.chatwoot.enviadas(555) == [TEXTO_RESPOSTA_SEGURA, TEXTO_CANAL_EXTERNO]
    nota = next(c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada")
    assert "tom_adequado" in nota
    transicao = sessao.scalars(select(Transicao).order_by(Transicao.id.desc())).first()
    causa = sessao.get(LaudoGravado, transicao.laudo_id)
    assert (causa.batch, causa.pergunta) == (BatchJev.JEV3, "tom_adequado")


def test_a_6a_sai_fixa_depois_de_5_mensagens(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    for _ in range(politica.teto_resolucao):
        assert cliente("quanto eu devo?", deps) is ResultadoTurno.ENVIADO
    redator = RedatorFalso(registrar=True)
    deps.redator = redator

    assert cliente("e agora?", deps) is ResultadoTurno.FIXA

    enviadas = deps.chatwoot.enviadas(555)
    assert len(enviadas) == politica.teto_resolucao + 1
    assert enviadas[-1] == TEXTO_CANAL_EXTERNO
    assert redator.pedidos == []
    assert ligada.motivo_humano is MotivoHumano.TETO
    assert sum(m.conta_no_teto for m in _do_agente(sessao)) == politica.teto_resolucao


def test_o_gatilho_de_retencao_nao_redige_nada(sessao, ligada, cliente, politica):
    redator = RedatorFalso(registrar=True)
    deps = _deps(politica, [responder("quer_retencao", True, 0.92)], redator=redator)

    assert cliente("quero cancelar", deps) is ResultadoTurno.HANDOFF

    assert redator.pedidos == []
    assert _do_agente(sessao) == []
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert deps.pix.cobrancas == {} and deps.ctn.links == {}
    assert ligada.motivo_humano is MotivoHumano.RETENCAO


class Jev3Fora(JevFalso):
    def avaliar(self, perguntas, estado):
        if perguntas[0].nome == "bate_com_acao":
            raise JevIndisponivel("o Jev passou de 2000 ms")
        return super().avaliar(perguntas, estado)


def test_jev3_indisponivel_vai_para_o_humano_sem_enviar_o_texto(sessao, ligada, cliente, politica):
    deps = _deps(politica, jev=Jev3Fora())

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.JEV_INDISPONIVEL
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert "2000 ms" in _turno(sessao).redacao["erro"]


def test_redator_indisponivel_vai_para_o_humano(sessao, ligada, cliente, politica):
    redator = RedatorFalso(falha=RedatorIndisponivel("o LLM respondeu 503"))
    deps = _deps(politica, redator=redator)

    assert cliente("quanto eu devo?", deps) is ResultadoTurno.HANDOFF

    assert ligada.motivo_humano is MotivoHumano.REDATOR_INDISPONIVEL
    assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]
    assert sessao.scalars(select(Pagamento)).all() == []
    assert ligada.forma_enviada is None


def test_none_nao_envia_nada(sessao, ligada, cliente, politica):
    redator = RedatorFalso(registrar=True)
    deps = _deps(politica, [responder("acao", "NONE")], redator=redator)

    assert cliente("ok, vi aqui", deps) is ResultadoTurno.SEM_ENVIO

    assert deps.chatwoot.chamadas == []
    assert redator.pedidos == []
    assert _turno(sessao).acao_principal == "NONE"
    assert ligada.estado is EstadoConversa.ENGAJADO


def test_fora_de_escopo_redige_sem_numero(sessao, ligada, cliente, politica):
    deps = _deps(politica, [responder("acao", "OUT_OF_SCOPE")])

    assert cliente("que horas abre a clínica?", deps) is ResultadoTurno.ENVIADO

    [texto] = deps.chatwoot.enviadas(555)
    assert "R$" not in texto
    assert (ligada.estado, ligada.forma_enviada) == (EstadoConversa.ENGAJADO, None)


IMPACIENTE = [responder("tom", "impaciente")]


def test_em_risco_o_agente_encurta_e_tem_um_turno_para_recuperar(sessao, ligada, cliente, politica):
    redator = RedatorFalso(registrar=True)
    deps = _deps(politica, IMPACIENTE, redator=redator)

    assert cliente("de novo isso?", deps) is ResultadoTurno.ENVIADO

    assert ligada.estado is EstadoConversa.EM_RISCO
    assert redator.pedidos[0].estado == "EM_RISCO"
    transicao = sessao.scalars(select(Transicao)).one()
    assert sessao.get(LaudoGravado, transicao.laudo_id).pergunta == "tom"

    # Sem melhora no turno seguinte, a conversa sai para o humano, sem redigir.
    assert cliente("já falei, não quero", deps) is ResultadoTurno.HANDOFF
    assert len(redator.pedidos) == 1
    assert ligada.motivo_humano is MotivoHumano.SEM_MELHORA


def test_em_risco_o_cliente_que_melhora_segue_com_o_robo(sessao, ligada, cliente, politica):
    primeiro = _deps(politica, [responder("repete_sem_avancar", True)])
    cliente("de novo isso?", primeiro)
    assert ligada.estado is EstadoConversa.EM_RISCO

    # O mesmo Chatwoot: o id de cada mensagem é único.
    segundo = _deps(politica)
    segundo.chatwoot = primeiro.chatwoot
    assert cliente("tá bom, me manda", segundo) is ResultadoTurno.ENVIADO

    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO


def test_os_roteiros_da_46_passam_em_modo_ativo_com_as_falsas(politica):
    casos = carregar_casos(RAIZ / "tests" / "conversas")

    relatorio = rodar_simulacao(casos, DependenciasSimulacao(politica))

    assert relatorio.divergencias == []
    # Todo turno que não saiu para o humano nem foi NONE mandou um texto ao cliente.
    acoes = [t.esperado.acao for caso in casos for t in caso.turnos if t.esperado.acao]
    assert len(relatorio.textos) == sum(acao.value != "NONE" for acao in acoes)
    assert all("{" not in texto for texto in relatorio.textos)


def test_none_numa_objecao_sem_desconto_tambem_conta_para_o_humano(
    sessao, ligada, cliente, politica
):
    ligada.cobranca.regua = Regua.NR
    deps = _deps(politica, [responder("objecao_valor", True), responder("acao", "NONE")])

    assert cliente("tá caro", deps) is ResultadoTurno.SEM_ENVIO
    assert ligada.objecoes_sem_desconto == 1

    # A objeção seguinte chega ao limite da política: vai para o humano, sem redigir.
    assert cliente("tá caro mesmo", deps) is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.OBJECAO_SEM_DESCONTO


@pytest.mark.parametrize(
    ("regua", "objecao"), [(Regua.NR, False), (Regua.R3, True), (Regua.R3, False)]
)
def test_none_sem_objecao_ou_na_regua_com_desconto_nao_conta(
    sessao, ligada, cliente, politica, regua, objecao
):
    ligada.cobranca.regua = regua
    deps = _deps(politica, [responder("objecao_valor", objecao), responder("acao", "NONE")])

    assert cliente("ok, vi aqui", deps) is ResultadoTurno.SEM_ENVIO

    assert ligada.objecoes_sem_desconto == 0


def _desfeito(sessao, deps, cliente, texto="quanto eu devo?"):
    cliente(texto, deps)
    turno = _turno(sessao)
    assert turno.numeros == {"executado": False}
    assert turno.acao_principal == "NAO_EXECUTADA:INFORM_DEBT"
    return turno


def test_a_resposta_segura_nao_registra_o_que_o_savepoint_desfez(sessao, ligada, cliente, politica):
    turno = _desfeito(sessao, _deps(politica, REPROVA_TOM), cliente)

    assert turno.redacao["saida"] == "RESPOSTA_SEGURA"
    assert turno.texto_enviado == TEXTO_RESPOSTA_SEGURA
    assert sessao.scalars(select(Pagamento)).all() == []


def test_o_handoff_da_validacao_nao_registra_o_que_o_savepoint_desfez(
    sessao, ligada, cliente, politica
):
    deps = _deps(politica, REPROVA_TOM)
    cliente("quanto eu devo?", deps)

    turno = _desfeito(sessao, deps, cliente, "e aí?")

    assert (turno.resultado, turno.redacao["saida"]) == (ResultadoTurno.HANDOFF, "HANDOFF")


def test_o_handoff_do_redator_fora_nao_registra_o_que_o_savepoint_desfez(
    sessao, ligada, cliente, politica
):
    redator = RedatorFalso(falha=RedatorIndisponivel("o LLM respondeu 503"))

    turno = _desfeito(sessao, _deps(politica, redator=redator), cliente)

    assert turno.resultado is ResultadoTurno.HANDOFF
    # O que o Jev decidiu continua nos laudos; o turno diz só que nada foi executado.
    assert turno.acoes_anexadas == ["GENERATE_PIX"]
