"""O pedido de texto quando o áudio não é lido (tarefa 8.6, D43).

A rajada sem nada legível (nenhum texto e nenhum áudio transcrito) leva a fixa PEDIR_TEXTO, sem Jev
e sem LLM, fora do teto; na 2ª vez da conversa, o humano assume com o motivo AUDIO.
"""

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import mensagens_no_teto, registrar_mensagem
from agente.db.modelos import Mensagem, Turno
from agente.integracoes.chatwoot import AnexoBaixado, ChatwootFalso, FalhaDownload
from agente.integracoes.transcritor import TranscritorFalso
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
    TipoFixa,
)
from agente.turno.catalogo import TEXTO_CANAL_EXTERNO, DadosFixa, mensagem_fixa
from agente.turno.gates import Adiar, Ignorar, Seguir, avaliar_envio
from agente.turno.orquestrador import processar_turno
from agente.turno.transcricao import pedido_de_texto
from tests.ogg_opus import ogg_opus

from .test_orquestrador_gates import SEGUNDA_10H
from .test_orquestrador_pagamento_concorrente import _estado_no_banco, _pagar_por_fora
from .test_transcricao import SEGUNDA_19H, _audio, _cliente, _deps

LONGO = "Seu áudio passou de 2 minutos. Pode mandar um mais curto ou escrever, por favor?"
FALHOU = "Não consegui ouvir seu áudio. Pode escrever em texto, por favor?"


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _audio_longo(deps):
    deps.chatwoot.anexo = AnexoBaixado(ogg_opus(deps.politica.audio_max_segundos + 30), "audio/ogg")


def _rotulos(sessao):
    return [t.acao_principal for t in sessao.scalars(select(Turno).order_by(Turno.id))]


def _do_agente(sessao):
    consulta = select(Mensagem).where(Mensagem.origem == OrigemMensagem.AGENTE)
    return sessao.scalars(consulta.order_by(Mensagem.id)).all()


class TestAPrimeiraVez:
    def test_so_audio_longo_sai_a_fixa_fora_do_teto_sem_jev_nem_llm(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("oi"))
        _audio_longo(deps)
        _cliente(sessao, ligada, anexos=[_audio()])

        resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert resultado is ResultadoTurno.FIXA
        assert deps.chatwoot.enviadas(555) == [LONGO]
        [mensagem] = _do_agente(sessao)
        assert (mensagem.conta_no_teto, mensagem.envio) == (False, EstadoEnvio.ENVIADA)
        assert mensagens_no_teto(sessao, ligada.id) == 0
        assert deps.jev.chamadas == [] and deps.redator.pedidos == []
        turno = sessao.scalars(select(Turno)).one()
        assert (turno.acao_principal, turno.resultado) == ("FIXA:PEDIR_TEXTO", ResultadoTurno.FIXA)
        assert turno.texto_enviado == LONGO
        assert ligada.estado is EstadoConversa.INICIADO

    def test_audio_que_falha_pede_o_texto_do_nao_consegui_ouvir(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso(motivo="timeout"))
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.enviadas(555) == [FALHOU]
        assert deps.jev.chamadas == []

    def test_download_que_falha_tambem_pede_o_texto(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("oi"))
        deps.chatwoot.anexo = FalhaDownload("status")
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.enviadas(555) == [FALHOU]

    def test_a_transcricao_desligada_cai_no_pedido(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.enviadas(555) == [FALHOU]
        assert deps.chatwoot.baixados == []

    def test_um_longo_e_um_que_falhou_na_mesma_rajada_pedem_o_texto_do_longo(
        self, sessao, ligada, politica
    ):
        deps = _deps(politica, transcritor=TranscritorFalso("oi"))
        _audio_longo(deps)
        _cliente(sessao, ligada, anexos=[_audio()], mid="cw-1")
        _cliente(sessao, ligada, anexos=[_audio(url=None)], mid="cw-2")

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.enviadas(555) == [LONGO]

    def test_o_pedido_fica_gravado_como_mensagem_do_agente_e_nao_como_fixa(
        self, sessao, ligada, politica
    ):
        # A origem FIXA é a do canal externo (a 6ª): o pedido não pode fazê-la parecer enviada.
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        [mensagem] = _do_agente(sessao)
        assert mensagem.origem is OrigemMensagem.AGENTE and mensagem.texto == FALHOU

    def test_depois_do_pedido_o_cliente_escreve_e_o_turno_segue_normal(
        self, sessao, ligada, politica
    ):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, texto="quanto eu devo?", momento=agora, mid="cw-2")

        assert processar_turno(sessao, ligada.id, agora, deps) is ResultadoTurno.ENVIADO

        assert len(deps.chatwoot.enviadas(555)) == 2
        assert len(deps.jev.chamadas) >= 1
        assert mensagens_no_teto(sessao, ligada.id) == 1


class TestASegundaVez:
    def test_o_segundo_audio_sem_leitura_vai_ao_humano_com_o_motivo_audio(
        self, sessao, ligada, politica
    ):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, anexos=[_audio()], momento=agora, mid="cw-2")

        resultado = processar_turno(sessao, ligada.id, agora, deps)

        assert resultado is ResultadoTurno.HANDOFF
        assert ligada.estado is EstadoConversa.ATENDIMENTO_HUMANO
        assert ligada.motivo_humano is MotivoHumano.AUDIO
        assert ("etiquetar", 555, ["audio"]) in deps.chatwoot.chamadas
        assert ("atribuir", 555, 4) in deps.chatwoot.chamadas
        assert deps.chatwoot.enviadas(555)[-1] == TEXTO_CANAL_EXTERNO
        assert deps.chatwoot.enviadas(555)[:1] == [FALHOU]
        assert _rotulos(sessao) == ["FIXA:PEDIR_TEXTO", "HUMAN:AUDIO"]
        assert deps.jev.chamadas == []

    def test_o_pedido_nao_conta_no_teto_e_a_6a_continua_a_sair(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        for _ in range(deps.politica.teto_resolucao):
            registrar_mensagem(
                sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
            )
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, texto="oi", momento=agora, mid="cw-2")

        resultado = processar_turno(sessao, ligada.id, agora, deps)

        # O pedido já saiu como AGENTE e fora do teto: a 6ª fixa não é tomada por "já saiu fixa".
        assert resultado is ResultadoTurno.FIXA
        assert ligada.motivo_humano is MotivoHumano.TETO
        assert deps.chatwoot.enviadas(555)[-1] == TEXTO_CANAL_EXTERNO

    def test_o_pedido_descartado_nao_conta_como_a_primeira_vez(self, sessao, ligada, politica):
        class ChatwootQueFalhaNoPedido(ChatwootFalso):
            def enviar_mensagem(self, conversa_id, texto, botoes=None):
                if texto == FALHOU:
                    raise httpx.ConnectError("caiu")
                return super().enviar_mensagem(conversa_id, texto, botoes)

        deps = _deps(politica, transcritor=None)
        deps.chatwoot = ChatwootQueFalhaNoPedido()
        _cliente(sessao, ligada, anexos=[_audio()])
        # O envio falha e o pedido fica PENDENTE; o atendente assume na espera e ele é descartado.
        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
        ligada.human_owned = True
        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO

        [pedido] = _do_agente(sessao)
        assert pedido.envio is EstadoEnvio.DESCARTADA
        primeiro = sessao.scalars(select(Turno).order_by(Turno.id)).first()
        assert primeiro.resultado is ResultadoTurno.DESCARTADO and primeiro.texto_enviado is None

        # O atendente devolveu a conversa ao robô: o próximo áudio ilegível é a "1ª vez" de novo.
        ligada.human_owned = False
        deps.chatwoot = ChatwootFalso()
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, anexos=[_audio()], momento=agora, mid="cw-2")
        processar_turno(sessao, ligada.id, agora, deps)
        assert deps.chatwoot.enviadas(555) == [FALHOU]


class TestSeHaOQueLer:
    def test_audio_transcrito_segue_o_turno_normal(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert len(deps.jev.chamadas) >= 1
        assert LONGO not in deps.chatwoot.enviadas(555)
        assert FALHOU not in deps.chatwoot.enviadas(555)

    def test_audio_com_texto_que_falha_o_jev_le_o_texto_e_nao_sai_pedido(
        self, sessao, ligada, politica
    ):
        deps = _deps(politica, transcritor=TranscritorFalso(motivo="timeout"))
        _cliente(sessao, ligada, texto="quanto eu devo?", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        historico = deps.jev.chamadas[0][1].historico
        assert historico[-1][1] == "quanto eu devo? [áudio — não foi possível ouvir]"
        assert FALHOU not in deps.chatwoot.enviadas(555)

    def test_imagem_junto_do_audio_segue_para_o_jev_como_na_v1(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio(), {"tipo": "image", "url": None}])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert len(deps.jev.chamadas) >= 1
        assert FALHOU not in deps.chatwoot.enviadas(555)

    def test_so_imagem_continua_como_hoje(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[{"tipo": "image", "url": None}])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert len(deps.jev.chamadas) >= 1
        assert FALHOU not in deps.chatwoot.enviadas(555)


class TestOsGatesVemAntes:
    def test_no_teto_sai_a_6a_e_nao_o_pedido(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        for _ in range(deps.politica.teto_resolucao):
            registrar_mensagem(
                sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
            )
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.FIXA

        assert deps.chatwoot.enviadas(555) == [TEXTO_CANAL_EXTERNO]

    def test_fora_da_janela_o_pedido_espera_a_abertura(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()], momento=SEGUNDA_19H)

        assert processar_turno(sessao, ligada.id, SEGUNDA_19H, deps) is ResultadoTurno.ADIADO
        assert deps.chatwoot.enviadas(555) == []

        processar_turno(sessao, ligada.id, ligada.turno_em, deps)
        assert deps.chatwoot.enviadas(555) == [FALHOU]

    def test_humano_na_conversa_nao_recebe_pedido(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        ligada.human_owned = True
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
        assert deps.chatwoot.enviadas(555) == []


class TestEmSombra:
    def test_a_1a_vez_so_grava_o_que_teria_feito(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None, modo=Modo.SOMBRA)
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.SOMBRA

        assert deps.chatwoot.chamadas == []
        assert _rotulos(sessao) == ["FIXA:PEDIR_TEXTO"]
        assert _do_agente(sessao) == []

    def test_a_2a_vez_preve_o_humano_sem_fazer_o_handoff(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None, modo=Modo.SOMBRA)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, anexos=[_audio()], momento=agora, mid="cw-2")

        processar_turno(sessao, ligada.id, agora, deps)

        assert _rotulos(sessao) == ["FIXA:PEDIR_TEXTO", "HUMAN:AUDIO"]
        assert deps.chatwoot.chamadas == []
        assert ligada.estado is EstadoConversa.INICIADO


class TestOPagamentoNoMeioDoTurno:
    """O webhook do Pix confirma o pagamento depois dos gates e antes de o pedido ser gravado."""

    @pytest.fixture
    def pagamento_chega(self, sessao, ligada, monkeypatch):
        from agente.turno import orquestrador

        original = orquestrador.pedido_de_texto

        def pedido_e_pagamento(mensagens):
            _pagar_por_fora(sessao, ligada)
            return original(mensagens)

        monkeypatch.setattr(orquestrador, "pedido_de_texto", pedido_e_pagamento)

    def test_o_pedido_nao_sai_depois_da_confirmacao(
        self, sessao, ligada, politica, pagamento_chega
    ):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])

        resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert resultado is ResultadoTurno.IGNORADO
        assert deps.chatwoot.chamadas == [] and _do_agente(sessao) == []
        turno = sessao.scalars(select(Turno)).one()
        assert turno.resultado is ResultadoTurno.IGNORADO and turno.texto_enviado is None
        assert _estado_no_banco(sessao, ligada) == (EstadoConversa.ENCERRADO, StatusCobranca.PAGA)

    def test_o_handoff_do_audio_nao_sobrescreve_o_encerrado(
        self, sessao, ligada, politica, monkeypatch
    ):
        deps = _deps(politica, transcritor=None)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)  # a 1ª vez: o pedido sai
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, anexos=[_audio()], momento=agora, mid="cw-2")
        from agente.turno import orquestrador

        original = orquestrador.pedido_de_texto

        def pedido_e_pagamento(mensagens):
            _pagar_por_fora(sessao, ligada)
            return original(mensagens)

        monkeypatch.setattr(orquestrador, "pedido_de_texto", pedido_e_pagamento)
        antes = len(deps.chatwoot.chamadas)

        resultado = processar_turno(sessao, ligada.id, agora, deps)

        assert resultado is ResultadoTurno.IGNORADO
        assert deps.chatwoot.chamadas[antes:] == []  # nem nota, nem etiqueta, nem canal externo
        assert _estado_no_banco(sessao, ligada) == (EstadoConversa.ENCERRADO, StatusCobranca.PAGA)
        assert sessao.get_one(type(ligada), ligada.id).motivo_humano is None


class TestOPedidoDaSombraNaoContaNoAtivo:
    def test_o_pedido_gravado_em_sombra_nao_e_a_1a_vez_do_ativo(self, sessao, ligada, politica):
        sombra = _deps(politica, transcritor=None, modo=Modo.SOMBRA)
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, sombra)
        assert _rotulos(sessao) == ["FIXA:PEDIR_TEXTO"]

        # A liberação gradual leva a conversa ao modo ativo: o cliente nunca viu o pedido.
        ativo = _deps(politica, transcritor=None)
        agora = SEGUNDA_10H + timedelta(minutes=2)
        _cliente(sessao, ligada, anexos=[_audio()], momento=agora, mid="cw-2")

        resultado = processar_turno(sessao, ligada.id, agora, ativo)

        assert resultado is ResultadoTurno.FIXA
        assert ativo.chatwoot.enviadas(555) == [FALHOU]
        assert (
            ligada.estado is not EstadoConversa.ATENDIMENTO_HUMANO and ligada.motivo_humano is None
        )


class TestAvaliarEnvioDoPedidoPendente:
    """O pedido gravado PENDENTE passa pelos mesmos gates das outras mensagens do agente."""

    @pytest.fixture
    def pendente(self, sessao, ligada):
        _cliente(sessao, ligada, anexos=[_audio()])
        return registrar_mensagem(
            sessao,
            ligada.id,
            OrigemMensagem.AGENTE,
            FALHOU,
            None,
            False,
            SEGUNDA_10H,
            envio=EstadoEnvio.PENDENTE,
        )

    def test_dentro_da_janela_segue(self, sessao, ligada, politica, pendente):
        assert avaliar_envio(ligada, pendente, SEGUNDA_10H, politica, sessao) == Seguir()

    def test_fora_da_janela_adia(self, sessao, ligada, politica, pendente):
        gate = avaliar_envio(ligada, pendente, SEGUNDA_19H, politica, sessao)
        assert isinstance(gate, Adiar)

    def test_depois_das_24h_nao_sai(self, sessao, ligada, politica, pendente):
        depois = SEGUNDA_10H + timedelta(hours=25)
        gate = avaliar_envio(ligada, pendente, depois, politica, sessao)
        assert isinstance(gate, Ignorar)

    def test_com_humano_na_conversa_nao_sai(self, sessao, ligada, politica, pendente):
        ligada.human_owned = True
        assert avaliar_envio(ligada, pendente, SEGUNDA_10H, politica, sessao) == Ignorar(
            "humano_na_conversa"
        )


class TestOCatalogo:
    def test_a_fixa_leva_o_texto_da_politica(self, politica):
        dados = DadosFixa("Ana", None, None, "", texto=politica.pedir_texto_audio_longo)

        mensagem = mensagem_fixa(TipoFixa.PEDIR_TEXTO, dados)

        assert mensagem.texto == LONGO and mensagem.botoes == []

    def test_sem_o_texto_e_um_erro(self):
        with pytest.raises(ValueError, match="texto"):
            mensagem_fixa(TipoFixa.PEDIR_TEXTO, DadosFixa("Ana", None, None, ""))

    def test_os_textos_padrao_da_politica(self, politica):
        assert politica.pedir_texto_audio_longo == LONGO
        assert politica.pedir_texto_audio_falhou == FALHOU


class TestPedidoDeTexto:
    @staticmethod
    def _mensagem(texto="", anexos=()):
        return Mensagem(
            conversa_id=1,
            origem=OrigemMensagem.CLIENTE,
            texto=texto,
            conta_no_teto=False,
            momento=datetime(2026, 10, 26),
            anexos=list(anexos),
        )

    @pytest.mark.parametrize(
        ("resultados", "esperado"),
        [
            (["longo_demais"], "longo_demais"),
            (["falhou"], "falhou"),
            ([None], "falhou"),
            (["longo_demais", "falhou"], "longo_demais"),
            (["falhou", "transcrito"], None),
        ],
    )
    def test_o_resultado_dos_audios_decide(self, resultados, esperado):
        anexos = [
            {"tipo": "audio", **({"resultado": r, "transcricao": "x"} if r else {})}
            for r in resultados
        ]
        assert pedido_de_texto([self._mensagem(anexos=anexos)]) == esperado

    def test_texto_ou_outro_anexo_ou_nenhum_audio_nao_pede(self):
        audio = {"tipo": "audio", "resultado": "falhou"}
        assert pedido_de_texto([self._mensagem("oi", [audio])]) is None
        assert pedido_de_texto([self._mensagem(anexos=[audio, {"tipo": "image"}])]) is None
        assert pedido_de_texto([self._mensagem("oi")]) is None
        assert pedido_de_texto([]) is None
