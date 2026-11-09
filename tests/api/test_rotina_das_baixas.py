"""A rotina das baixas no laço do serviço: a cada 10 minutos e só com o CTN real."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from agente.api.app import criar_app
from agente.config import Config
from agente.db.modelos import BaixaCTNRegistro, Base
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.ctn import BaixaCTN, CTNFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.tipos import FormaHistorico, Modo
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


def _app(politica, ctn, **config):
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    relogio = [AGORA]
    deps = Dependencias(
        politica=politica,
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=Modo.SOMBRA,
        jev=JevFalso(),
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
