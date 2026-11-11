"""A rotina das baixas no laço do serviço: a cada 10 minutos e só com o CTN real."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.historico import registrar_mensagem
from agente.db.modelos import BaixaCTNRegistro, Base, Cliente, Cobranca, Conversa
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import BaixaCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.politica import LiberacaoPiloto, Regua
from agente.tipos import (
    EstadoConversa,
    FormaHistorico,
    Modo,
    OrigemMensagem,
    StatusCobranca,
)
from agente.turno.orquestrador import Dependencias

from ..conftest import RAIZ

AGORA = datetime(2026, 11, 9, 15, tzinfo=UTC)


class CTNQueConta(CTNFalso):
    def __init__(self, **resto):
        super().__init__(**resto)
        self.leituras = 0

    def baixas_desde(self, coletado_desde, vistos=()):
        self.leituras += 1
        return super().baixas_desde(coletado_desde, vistos)


def _baixa():
    return BaixaCTN(
        id_externo="b-1",
        matricula="M-9999",
        mes_ref="2026/10",
        valor_centavos=3340,
        forma=FormaHistorico.BOLETO,
        pago_em=AGORA - timedelta(hours=1),
        coletado_em=AGORA - timedelta(minutes=10),
    )


def _app(politica, ctn, *, modo=Modo.SOMBRA, chatwoot=None, **config):
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    relogio = [AGORA]
    deps = Dependencias(
        politica=politica,
        chatwoot=chatwoot or ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        llm_turno=LLMTurnoFalso(),
        ctn=ctn,
    )
    configuracao = Config(database_url="sqlite://", politica_path=RAIZ / "politica.toml", **config)
    app = criar_app(
        fabrica,
        politica,
        configuracao,
        relogio=lambda: relogio[0],
        iniciar_trabalhador=False,
        dependencias=deps,
        ctn=ctn,
    )
    return app.state.trabalhador, fabrica, relogio


def test_com_o_ctn_real_o_laco_le_as_baixas_a_cada_10_minutos(politica):
    ctn = CTNQueConta(baixas_externas=[_baixa()])
    trabalhador, fabrica, relogio = _app(politica, ctn, ctn_api_key="chave-inventada")

    trabalhador.rodar_uma_vez()
    relogio[0] = AGORA + timedelta(minutes=5)
    trabalhador.rodar_uma_vez()
    assert ctn.leituras == 1

    relogio[0] = AGORA + timedelta(minutes=10)
    trabalhador.rodar_uma_vez()
    assert ctn.leituras == 2
    with fabrica() as sessao:
        [registro] = sessao.scalars(select(BaixaCTNRegistro)).all()
    assert registro.id_externo == "b-1"
    assert registro.efeito == "sem_cobranca"


def test_sem_o_ctn_real_a_rotina_das_baixas_nao_roda(politica):
    ctn = CTNQueConta(baixas_externas=[_baixa()])
    trabalhador, fabrica, _relogio = _app(politica, ctn)

    trabalhador.rodar_uma_vez()

    assert ctn.leituras == 0
    with fabrica() as sessao:
        assert sessao.scalars(select(BaixaCTNRegistro)).all() == []


@pytest.mark.parametrize("ligado", [True, False])
def test_a_rotina_das_baixas_usa_o_ctn_que_o_app_recebe(politica, ligado):
    ctn = CTNQueConta()
    config = {"ctn_api_key": "chave-inventada"} if ligado else {}
    trabalhador, _fabrica, _relogio = _app(politica, ctn, **config)
    trabalhador.rodar_uma_vez()
    assert ctn.leituras == (1 if ligado else 0)


# ---- o modo ativo e o piloto: o aviso da baixa externa sai dentro da janela e não fora ----

NO_PILOTO = "+5583999990001"
FORA_DO_PILOTO = "+5583999990002"


def _cliente_quitado_por_fora(fabrica, telefone, mensagem_ha):
    """Uma cobrança ativa de 3 meses, em conversa ligada ao Chatwoot (555), que o CTN já baixou."""
    with fabrica() as sessao:
        cobranca = Cobranca(
            cliente=Cliente(matricula_ctn="M-9999", nome="Cliente Fictício", telefone=telefone),
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="nov",
        )
        conversa = Conversa(
            cobranca=cobranca,
            criada_em=AGORA - timedelta(days=3),
            chatwoot_conversa_id=555,
            estado=EstadoConversa.AGUARDANDO_PAGAMENTO,
        )
        sessao.add(conversa)
        sessao.flush()
        registrar_mensagem(
            sessao, conversa.id, OrigemMensagem.CLIENTE, "oi", "cw-1", False, AGORA - mensagem_ha
        )
        sessao.commit()


def _ctn_que_quitou():
    return CTNQueConta(
        baixas_externas=[_baixa()],
        mensalidades_abertas={"M-9999": []},
        rejeitadas_lidas={"M-9999": 3},
        hoje=AGORA.date(),
    )


def _rodar_ativo(politica, telefone_do_cliente, mensagem_ha):
    piloto = politica.model_copy(
        update={"liberacao": LiberacaoPiloto(modo="piloto", telefones=frozenset({NO_PILOTO}))}
    )
    chatwoot = ChatwootFalso()
    trabalhador, fabrica, _relogio = _app(
        piloto,
        _ctn_que_quitou(),
        modo=Modo.ATIVO,
        chatwoot=chatwoot,
        ctn_api_key="chave-inventada",
    )
    _cliente_quitado_por_fora(fabrica, telefone_do_cliente, mensagem_ha)

    trabalhador.rodar_uma_vez()

    with fabrica() as sessao:
        [registro] = sessao.scalars(select(BaixaCTNRegistro)).all()
        cobranca = sessao.get(Cobranca, registro.cobranca_id)
        status = cobranca.status
    return piloto, chatwoot, registro.efeito, status


def test_no_piloto_e_dentro_das_24h_o_aviso_da_baixa_externa_sai(politica):
    piloto, chatwoot, efeito, status = _rodar_ativo(politica, NO_PILOTO, timedelta(hours=1))

    assert status is StatusCobranca.PAGA
    assert efeito == "quitou"
    assert chatwoot.enviadas(555) == [piloto.texto_baixa_externa]


def test_no_piloto_mas_fora_das_24h_o_aviso_da_baixa_externa_nao_sai(politica):
    _piloto, chatwoot, efeito, status = _rodar_ativo(politica, NO_PILOTO, timedelta(hours=30))

    assert status is StatusCobranca.PAGA
    assert efeito == "quitou_fora_da_janela"
    assert chatwoot.enviadas(555) == []


def test_fora_do_piloto_o_aviso_da_baixa_externa_nao_sai_mesmo_dentro_da_janela(politica):
    _piloto, chatwoot, efeito, status = _rodar_ativo(politica, FORA_DO_PILOTO, timedelta(hours=1))

    assert status is StatusCobranca.PAGA
    assert efeito == "quitou_sombra"
    assert chatwoot.enviadas(555) == []
