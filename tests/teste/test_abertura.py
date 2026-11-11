"""A abertura no 1º contato do testador (D56, P2, P8): o 1º "Oi" recebe a identificação do caso e
o texto de abertura, sem o LLM; a partir da 2ª mensagem o turno é o de sempre."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.db.modelos import Conversa, Mensagem, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import CTNIndisponivel
from agente.integracoes.ctn_teste import CTNDeCasos
from agente.integracoes.disparador import FalhaNoEnvio
from agente.teste.comandos import texto_da_abertura
from agente.tipos import OrigemMensagem, ResultadoTurno
from agente.turno.fila import mensagens_pendentes
from agente.turno.orquestrador import processar_turno
from tests.teste.auxiliares import CHATWOOT_ID, Mandante, montar_deps
from tests.turno.test_orquestrador_gates import SEGUNDA_10H

TEXTO_DO_CASO_1 = "[TESTE] Você é o caso 1: R3, pagava no cartão. Mande /caso para ver os outros."
TEXTO_DA_ABERTURA_1 = "Oi, Ana! Há 3 mensalidades em aberto, R$ 100,20."


def _mensagens(sessao, conversa):
    return list(
        sessao.scalars(
            select(Mensagem).where(Mensagem.conversa_id == conversa.id).order_by(Mensagem.id)
        )
    )


def _origens(sessao, conversa):
    return [m.origem for m in _mensagens(sessao, conversa)]


def _ultimo_turno(sessao):
    return sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()


class ChatwootQueFalhaNoEnvio(ChatwootFalso):
    """O `n`-ésimo envio (a partir de 1) falha, uma vez só."""

    def __init__(self, n):
        super().__init__()
        self.n, self.envios = n, 0

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        self.envios += 1
        if self.envios == self.n:
            raise FalhaNoEnvio("chatwoot fora")
        return super().enviar_mensagem(conversa_id, texto, botoes)


def test_o_texto_da_abertura_traz_nome_valor_e_mensalidades(conversa_de_teste, politica_de_teste):
    assert texto_da_abertura(conversa_de_teste.cobranca, politica_de_teste) == TEXTO_DA_ABERTURA_1


def test_o_texto_da_abertura_sem_mensalidades_no_modelo_e_sem_chaves_soltas(
    conversa_de_teste, politica_de_teste
):
    teste = politica_de_teste.teste.model_copy(
        update={"texto_abertura": '{nome}, {valor}. Escreva: "Pagar com Pix" {x}'}
    )
    politica = politica_de_teste.model_copy(update={"teste": teste})
    assert (
        texto_da_abertura(conversa_de_teste.cobranca, politica)
        == 'Ana, R$ 100,20. Escreva: "Pagar com Pix" {x}'
    )


def test_o_oi_do_1o_contato_recebe_a_identificacao_e_a_abertura_sem_o_llm(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("Oi")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    assert deps.chatwoot.enviadas(CHATWOOT_ID) == [TEXTO_DO_CASO_1, TEXTO_DA_ABERTURA_1]
    oi, identificacao, abertura = _mensagens(sessao, conversa_de_teste)
    assert (oi.origem, oi.texto) == (OrigemMensagem.CLIENTE, "Oi")
    assert (identificacao.origem, identificacao.conta_no_teto) == (OrigemMensagem.TESTE, False)
    assert (abertura.origem, abertura.conta_no_teto) == (OrigemMensagem.ABERTURA, False)
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:ABERTURA", ResultadoTurno.FIXA)
    assert turno.mensagens_ids == [oi.id]
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []
    assert conversa_de_teste.turno_em is None
    assert deps.llm_turno.pedidos == []


def test_a_2a_mensagem_segue_ao_turno_normal_e_o_llm_ve_a_abertura(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("Oi")
    testador.roda(deps)

    testador.manda("quero pagar")
    testador.roda(deps)

    assert deps.llm_turno.pedidos, "o turno normal deveria rodar na 2ª mensagem"
    [(_, usuario), *_] = deps.llm_turno.pedidos
    historico = json.loads(usuario)["conversa"]
    assert [OrigemMensagem.ABERTURA.value, TEXTO_DA_ABERTURA_1] in historico
    assert all("[TESTE]" not in texto for _, texto in historico)
    # A abertura não se repete.
    assert _origens(sessao, conversa_de_teste).count(OrigemMensagem.ABERTURA) == 1


def test_a_cobranca_que_ja_abriu_em_outra_conversa_nao_abre_de_novo(
    sessao, testador, conversa_de_teste, deps
):
    antiga = Conversa(cobranca=conversa_de_teste.cobranca)
    sessao.add(antiga)
    sessao.flush()
    sessao.add(
        Mensagem(
            conversa_id=antiga.id,
            origem=OrigemMensagem.ABERTURA,
            texto="Oi, Ana!",
            conta_no_teto=False,
            momento=SEGUNDA_10H,
        )
    )
    sessao.commit()
    testador.manda("Oi")

    testador.roda(deps)

    assert "[TESTE] Você é o caso" not in " ".join(deps.chatwoot.enviadas(CHATWOOT_ID))
    assert deps.llm_turno.pedidos, "sem a abertura, o turno é o normal"


def test_o_caso_novo_abre_de_novo_no_proximo_oi(sessao, testador, conversa_de_teste, deps):
    testador.manda("Oi")
    testador.roda(deps)
    testador.manda("/caso 2")
    testador.roda(deps)
    nova = sessao.scalars(
        select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
    ).one()
    depois = Mandante(sessao, nova)
    depois.n = testador.n

    depois.manda("Oi")
    depois.roda(deps)

    enviadas = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert enviadas[-2].startswith("[TESTE] Você é o caso 2:")
    assert enviadas[-1].startswith("Oi, Bruno!")
    assert deps.llm_turno.pedidos == []


def test_fora_da_janela_o_turno_adia_e_a_abertura_sai_na_abertura_da_janela(
    sessao, conversa_de_teste, deps
):
    noite = Mandante(sessao, conversa_de_teste, inicio=SEGUNDA_10H.replace(hour=20))
    noite.manda("Oi")

    noite.roda(deps)

    assert deps.chatwoot.enviadas(CHATWOOT_ID) == []
    assert _origens(sessao, conversa_de_teste) == [OrigemMensagem.CLIENTE]
    sessao.refresh(conversa_de_teste)
    assert conversa_de_teste.turno_em is not None

    abertura_da_janela = SEGUNDA_10H.replace(hour=8) + timedelta(days=1)
    resultado = processar_turno(sessao, conversa_de_teste.id, abertura_da_janela, deps)

    assert resultado is ResultadoTurno.FIXA
    assert deps.chatwoot.enviadas(CHATWOOT_ID) == [TEXTO_DO_CASO_1, TEXTO_DA_ABERTURA_1]


def test_com_humano_na_conversa_nao_abre(sessao, testador, conversa_de_teste, deps):
    conversa_de_teste.human_owned = True
    sessao.commit()
    testador.manda("Oi")

    testador.roda(deps)

    assert deps.chatwoot.enviadas(CHATWOOT_ID) == []
    assert OrigemMensagem.ABERTURA not in _origens(sessao, conversa_de_teste)


def test_a_conferencia_que_falha_nao_abre(sessao, politica_de_teste, conversa_de_teste, testador):
    class CasosFora(CTNDeCasos):
        def consultar_mensalidades(self, matricula):
            raise CTNIndisponivel("fora")

    casos = CasosFora(
        politica_de_teste.teste.casos,
        politica_de_teste.mensalidade_centavos,
        lambda: SEGUNDA_10H.date(),
    )
    deps = montar_deps(politica_de_teste, casos)
    # A conferência é de ontem: antes de abrir, o turno precisa ir ao CTN, e ele está fora.
    conversa_de_teste.cobranca.conferida_em = SEGUNDA_10H - timedelta(days=1)
    sessao.commit()
    testador.manda("Oi")

    testador.roda(deps)

    assert deps.chatwoot.enviadas(CHATWOOT_ID) == []
    assert OrigemMensagem.ABERTURA not in _origens(sessao, conversa_de_teste)
    assert [m.texto for m in mensagens_pendentes(sessao, conversa_de_teste.id)] == ["Oi"]


@pytest.mark.parametrize("falha_no_envio", [1, 2])
def test_o_chatwoot_que_falha_desfaz_a_abertura_e_o_oi_continua_pendente(
    sessao, politica_de_teste, casos, conversa_de_teste, testador, falha_no_envio
):
    deps = montar_deps(politica_de_teste, casos, ChatwootQueFalhaNoEnvio(falha_no_envio))
    testador.manda("Oi")

    assert testador.roda(deps) is ResultadoTurno.ERRO

    origens = _origens(sessao, conversa_de_teste)
    assert OrigemMensagem.ABERTURA not in origens and OrigemMensagem.TESTE not in origens
    assert [m.texto for m in mensagens_pendentes(sessao, conversa_de_teste.id)] == ["Oi"]
