from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import select

import agente.cli
from agente.abertura.disparar import disparar_aberturas
from agente.db.historico import mensagens_no_teto
from agente.db.modelos import Cliente, Cobranca, Conversa, Mensagem
from agente.db.sessao import criar_sessao
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.disparador import DisparadorFalso
from agente.politica import LiberacaoPiloto, Regua
from agente.tipos import Botao, OrigemMensagem, StatusCobranca
from agente.turno.liberacao import liberar_tudo

from ..conftest import alembic_config, ligar_o_disparo

JP = ZoneInfo("America/Fortaleza")
SEGUNDA_10H = datetime(2026, 10, 26, 10, tzinfo=JP)
DOMINGO = datetime(2026, 11, 1, 10, tzinfo=JP)


@pytest.fixture
def politica(politica):
    """O disparo só abre as cobranças liberadas: estes testes valem com a carteira toda no ativo."""
    return liberar_tudo(politica)


def _cobranca(sessao, matricula, campanha="outubro", status=StatusCobranca.ATIVA):
    telefone = f"+558399999{int(matricula.split('-')[1]):04d}"
    cliente = Cliente(matricula_ctn=matricula, nome="Maria Fictícia da Silva", telefone=telefone)
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha=campanha,
        status=status,
    )
    sessao.add(cobranca)
    sessao.commit()
    return cobranca


def test_fora_da_janela_nao_dispara(sessao, politica):
    _cobranca(sessao, "F-1")
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", DOMINGO)

    assert relatorio.fora_da_janela is True
    assert relatorio.enviadas == 0
    assert disparador.envios == []
    assert sessao.scalars(select(Conversa)).all() == []


def test_dispara_a_abertura_com_o_botao_pagar_com_pix(sessao, politica):
    cobranca = _cobranca(sessao, "F-1")
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    assert relatorio.enviadas == 1
    [envio] = disparador.envios
    assert envio.telefone == "+5583999990001"
    assert envio.template == politica.template_abertura
    assert envio.variaveis == {"nome": "Maria", "valor": "R$ 100,20"}
    assert envio.botoes == [Botao("resposta", "Pagar com Pix", "PAGAR_PIX")]
    conversa = sessao.scalars(select(Conversa)).one()
    assert conversa.cobranca_id == cobranca.id
    mensagem = sessao.scalars(select(Mensagem)).one()
    assert mensagem.origem is OrigemMensagem.ABERTURA
    assert mensagens_no_teto(sessao, conversa.id) == 0


def test_rodar_de_novo_nao_duplica(sessao, politica):
    _cobranca(sessao, "F-1")
    disparador = DisparadorFalso()
    disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    assert (relatorio.enviadas, relatorio.puladas) == (0, 1)
    assert len(disparador.envios) == 1


def test_so_cobranca_ativa_da_campanha(sessao, politica):
    _cobranca(sessao, "F-1", campanha="setembro")
    _cobranca(sessao, "F-2", status=StatusCobranca.ENCERRADA)
    disparador = DisparadorFalso()

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    assert relatorio.enviadas == 0
    assert disparador.envios == []


def test_falha_de_um_cliente_nao_para_os_outros(sessao, politica):
    _cobranca(sessao, "F-1")
    _cobranca(sessao, "F-2")
    disparador = DisparadorFalso(falhar_para={"+5583999990001"})

    relatorio = disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H)

    assert relatorio.enviadas == 1
    assert [matricula for matricula, _ in relatorio.falhas] == ["F-1"]
    # Quem falhou fica sem conversa e volta na próxima rodada.
    disparador.falhar_para.clear()
    assert disparar_aberturas(sessao, politica, disparador, "outubro", SEGUNDA_10H).enviadas == 1


@pytest.mark.parametrize(
    ("centavos", "texto"),
    [(10020, "R$ 100,20"), (1020, "R$ 10,20"), (5, "R$ 0,05"), (123456789, "R$ 1.234.567,89")],
)
def test_formatar_reais(centavos, texto):
    assert formatar_reais(centavos) == texto


def test_comando_disparar(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    monkeypatch.setenv("AGENTE_DATABASE_URL", url)
    ligar_o_disparo(tmp_path, monkeypatch)
    with criar_sessao(url)() as sessao:
        _cobranca(sessao, "F-1")

    class _Relogio(datetime):
        @classmethod
        def now(cls, tz=None):
            return SEGUNDA_10H

    monkeypatch.setattr(agente.cli, "datetime", _Relogio)

    assert agente.cli.main(["disparar", "--campanha", "outubro"]) == 0
    assert "enviadas: 1" in capsys.readouterr().out


def test_em_sombra_nada_e_disparado_e_o_ctn_nao_e_consultado(sessao, politica):
    from agente.integracoes.ctn import CTNFalso
    from agente.politica import LiberacaoSombra

    _cobranca(sessao, "F-1")
    disparador = DisparadorFalso()
    em_sombra = politica.model_copy(update={"liberacao": LiberacaoSombra()})

    relatorio = disparar_aberturas(
        sessao, em_sombra, disparador, "outubro", SEGUNDA_10H, CTNFalso()
    )

    assert (relatorio.enviadas, relatorio.fora_da_liberacao) == (0, 1)
    assert disparador.envios == []
    assert sessao.scalars(select(Conversa)).all() == []


def test_com_piloto_so_o_telefone_do_piloto_recebe_a_abertura(sessao, politica):
    do_piloto = _cobranca(sessao, "F-1")
    fora = _cobranca(sessao, "F-2")
    disparador = DisparadorFalso()
    piloto = politica.model_copy(
        update={
            "liberacao": LiberacaoPiloto(
                modo="piloto", telefones=frozenset({do_piloto.cliente.telefone})
            )
        }
    )

    relatorio = disparar_aberturas(sessao, piloto, disparador, "outubro", SEGUNDA_10H)

    assert (relatorio.enviadas, relatorio.fora_da_liberacao) == (1, 1)
    assert [envio.telefone for envio in disparador.envios] == [do_piloto.cliente.telefone]
    assert fora.conversas == [] and fora.status is StatusCobranca.ATIVA
