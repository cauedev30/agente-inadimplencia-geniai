"""O botão "Pagar com Pix" da abertura (tarefa 6.2, D26): vai direto para o GENERATE_PIX."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.abertura.disparar import BOTAO_PAGAR_PIX
from agente.api.chatwoot_webhook import ler_evento, receber_evento
from agente.config import Config
from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import LaudoGravado, Mensagem, Pagamento, Turno
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso
from agente.integracoes.pix import PixFalso
from agente.integracoes.redator import RedatorFalso
from agente.pagamento.expirar import expirar_pix
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    FormaPagamento,
    Modo,
    OrigemMensagem,
    ResultadoTurno,
    StatusPagamento,
    TipoFixa,
)
from agente.turno.fila import mensagens_pendentes
from agente.turno.gates import Fixa, PagarPix, Seguir, avaliar_gates
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno

from .test_orquestrador_gates import SEGUNDA_10H

TOQUE = BOTAO_PAGAR_PIX.texto


def _deps(politica, modo=Modo.ATIVO):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=JevFalso(aprovar_tudo(), registrar=True),
        pix=PixFalso(agora=lambda: SEGUNDA_10H),
        redator=RedatorFalso(registrar=True),
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


# --- o gate ---


def _gate(sessao, conversa, politica, *textos):
    for i, texto in enumerate(textos):
        registrar_mensagem(
            sessao, conversa.id, OrigemMensagem.CLIENTE, texto, f"g-{i}", False, SEGUNDA_10H
        )
    mensagens = mensagens_pendentes(sessao, conversa.id)
    return avaliar_gates(conversa, mensagens, SEGUNDA_10H, politica, sessao)


def test_o_toque_no_botao_e_o_gate_pagar_pix(sessao, ligada, politica):
    assert _gate(sessao, ligada, politica, TOQUE) == PagarPix()


def test_o_toque_nao_depende_de_caixa_nem_de_espacos(sessao, ligada, politica):
    assert _gate(sessao, ligada, politica, f"  {TOQUE.upper()} ") == PagarPix()


def test_o_toque_misturado_com_outra_mensagem_segue_para_o_jev(sessao, ligada, politica):
    assert _gate(sessao, ligada, politica, "tá caro", TOQUE) == Seguir()


def test_no_teto_o_toque_recebe_a_fixa_e_nao_o_pix(sessao, ligada, politica):
    for _ in range(politica.teto_resolucao):
        registrar_mensagem(
            sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
        )
    assert _gate(sessao, ligada, politica, TOQUE) == Fixa(TipoFixa.CANAL_EXTERNO)


# --- o payload do Chatwoot ---


def _evento(**extra):
    return {
        "event": "message_created",
        "id": 900,
        "content": "",
        "message_type": "incoming",
        "private": False,
        "conversation": {"id": 555},
        "sender": {"type": "contact", "phone_number": "+5583900000001"},
        **extra,
    }


def test_o_payload_do_botao_vira_o_texto_do_botao():
    evento = ler_evento(_evento(content_attributes={"payload": BOTAO_PAGAR_PIX.valor}))
    assert evento.texto == TOQUE


def test_o_texto_do_botao_vale_sem_o_payload():
    assert ler_evento(_evento(content=TOQUE)).texto == TOQUE


def test_outro_payload_nao_vira_toque():
    evento = ler_evento(_evento(content="oi", content_attributes={"payload": "OUTRO"}))
    assert evento.texto == "oi"


def test_o_toque_pelo_webhook_agenda_o_turno_e_cai_no_gate(sessao, ligada, politica):
    dados = _evento(content_attributes={"payload": BOTAO_PAGAR_PIX.valor})

    assert (
        receber_evento(sessao, dados, politica, Config(database_url="sqlite://"), SEGUNDA_10H)
        == "agendado"
    )

    mensagens = mensagens_pendentes(sessao, ligada.id)
    assert avaliar_gates(ligada, mensagens, SEGUNDA_10H, politica, sessao) == PagarPix()


# --- o turno ---


def test_tocar_no_botao_gera_o_pix_integral_e_conta_no_teto(sessao, ligada, cliente, politica):
    deps = _deps(politica)

    assert cliente(TOQUE, deps) is ResultadoTurno.ENVIADO

    [pix] = deps.pix.cobrancas.values()
    assert pix.valor_centavos == ligada.cobranca.divida_centavos  # integral (D34)
    [texto] = deps.chatwoot.enviadas(555)
    assert pix.br_code in texto
    pagamento = sessao.scalars(select(Pagamento)).one()
    assert (pagamento.forma, pagamento.status) == (FormaPagamento.PIX, StatusPagamento.PENDENTE)
    assert mensagens_no_teto(sessao, ligada.id) == 1
    assert ligada.estado is EstadoConversa.AGUARDANDO_PAGAMENTO
    assert ligada.forma_enviada is FormaPagamento.PIX


def test_o_toque_nao_passa_pelo_jev_1_nem_pelo_jev_2(sessao, ligada, cliente, politica):
    deps = _deps(politica)

    cliente(TOQUE, deps)

    perguntas = {nome for nomes, _ in deps.jev.chamadas for nome in nomes}
    assert "tom" not in perguntas and "acao" not in perguntas
    assert perguntas  # o Jev 3 valida o texto
    turno = sessao.scalars(select(Turno)).one()
    assert turno.acao_principal == "GENERATE_PIX" and turno.acoes_anexadas == []
    batches = {laudo.batch for laudo in sessao.scalars(select(LaudoGravado))}
    assert batches == {BatchJev.JEV3}


def test_tocar_duas_vezes_reaproveita_o_pix_que_ainda_vale(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    cliente(TOQUE, deps)

    assert cliente(TOQUE, deps) is ResultadoTurno.ENVIADO

    assert len(deps.pix.cobrancas) == 1
    assert len(sessao.scalars(select(Pagamento)).all()) == 1
    primeira, segunda = deps.chatwoot.enviadas(555)
    [pix] = deps.pix.cobrancas.values()
    assert pix.br_code in primeira and pix.br_code in segunda
    assert mensagens_no_teto(sessao, ligada.id) == 2


def test_o_pix_vencido_nao_e_reaproveitado(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    cliente(TOQUE, deps)
    pagamento = sessao.scalars(select(Pagamento)).one()
    pagamento.expira_em = SEGUNDA_10H  # venceu antes do 2º toque
    sessao.commit()

    cliente(TOQUE, deps)

    assert len(deps.pix.cobrancas) == 2
    assert len(sessao.scalars(select(Pagamento)).all()) == 2


def test_o_pix_expirado_pelo_laco_da_lugar_a_um_novo(sessao, ligada, cliente, politica):
    deps = _deps(politica)
    cliente(TOQUE, deps)
    expirar_pix(sessao, SEGUNDA_10H + timedelta(hours=25))

    cliente(TOQUE, deps)

    assert len(deps.pix.cobrancas) == 2
    estados = [p.status for p in sessao.scalars(select(Pagamento).order_by(Pagamento.id))]
    assert estados == [StatusPagamento.EXPIRADO, StatusPagamento.PENDENTE]


def test_em_sombra_o_toque_nao_gera_pix_nem_envia(sessao, ligada, cliente, politica):
    deps = _deps(politica, Modo.SOMBRA)

    assert cliente(TOQUE, deps) is ResultadoTurno.SOMBRA

    assert deps.pix.cobrancas == {}
    assert deps.chatwoot.enviadas(555) == []
    turno = sessao.scalars(select(Turno)).one()
    assert turno.acao_principal == "GENERATE_PIX"
    assert turno.numeros["valor_pix_centavos"] == ligada.cobranca.divida_centavos
    do_agente = select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    assert sessao.scalars(do_agente).all() == []
