"""O áudio do cliente vale como texto no turno (tarefa 8.4, D43).

Os gates rodam antes; depois do `Seguir` e antes do Jev 1, o áudio é baixado, medido e transcrito,
e o Jev lê o texto transcrito como leria a mensagem escrita. Tudo com as falsas.
"""

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Base, Cliente, Cobranca, Conversa, Mensagem, Turno
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import AnexoBaixado, ChatwootFalso, FalhaDownload
from agente.integracoes.disparador import DisparadorFalso
from agente.integracoes.jev import JevFalso, responder
from agente.integracoes.redator import RedatorFalso, RedatorIndisponivel
from agente.integracoes.transcritor import Transcricao, TranscritorFalso
from agente.politica import Regua
from agente.tipos import (
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.jev3 import aprovar_tudo
from agente.turno.liberacao import liberar_tudo
from agente.turno.orquestrador import Dependencias, processar_turno
from agente.turno.transcricao import rotulo_do_audio, texto_da_mensagem
from tests.ogg_opus import ogg_opus

from .test_orquestrador_gates import JP, SEGUNDA_10H

SEGUNDA_19H = datetime(2026, 10, 26, 19, tzinfo=JP)  # depois das 18h: a janela abre na terça
URL_AUDIO = "https://chatwoot.invalid/rails/active_storage/blobs/xyz/audio.ogg"
FALA = "palavras-faladas-pelo-cliente"
JA_PAGUEI = responder(
    "alega_pagamento", True, se=lambda estado: "já paguei" in estado.historico[-1][1]
)


def _deps(politica, *, transcritor, modo=Modo.ATIVO, regras=(), redator=None):
    return Dependencias(
        politica=liberar_tudo(politica),
        chatwoot=ChatwootFalso(),
        disparador=DisparadorFalso(),
        time_humano=4,
        modo=modo,
        jev=JevFalso([*regras, *aprovar_tudo()], registrar=True),
        redator=redator or RedatorFalso(registrar=True),
        transcritor=transcritor,
    )


@pytest.fixture
def ligada(conversa):
    conversa.chatwoot_conversa_id = 555
    return conversa


def _audio(url=URL_AUDIO, content_type="audio/ogg"):
    return {"tipo": "audio", "url": url, "tamanho": 4000, "content_type": content_type}


def _cliente(sessao, conversa, texto="", anexos=(), momento=SEGUNDA_10H, mid="cw-1"):
    return registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.CLIENTE,
        texto,
        mid,
        False,
        momento,
        anexos=list(anexos),
    )


def _anexo(sessao, mensagem):
    sessao.refresh(mensagem)
    return mensagem.anexos[0]


def test_o_audio_ja_paguei_leva_ao_gatilho_de_conciliacao(sessao, ligada, politica):
    deps = _deps(politica, transcritor=TranscritorFalso("já paguei ontem"), regras=[JA_PAGUEI])
    _cliente(sessao, ligada, anexos=[_audio()])

    resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert resultado is ResultadoTurno.HANDOFF
    assert ligada.motivo_humano is MotivoHumano.CONCILIACAO
    estado = deps.jev.chamadas[0][1]
    assert estado.historico[-1] == ("CLIENTE", '[áudio transcrito: "já paguei ontem"]')


def _rodar_em_banco_novo(politica, fala, **mensagem):
    """Uma conversa nova, a mensagem dada e um turno: o que o cliente ver acontecer."""
    fabrica = criar_sessao("sqlite://")
    Base.metadata.create_all(fabrica.kw["bind"])
    with fabrica() as sessao:
        cliente = Cliente(matricula_ctn="M-1", nome="Ana Fictícia", telefone="+5583900000001")
        cobranca = Cobranca(
            cliente=cliente,
            regua=Regua.R3,
            mensalidades_abertas=3,
            divida_centavos=10020,
            propenso_cartao=False,
            campanha="teste",
        )
        conversa = Conversa(cobranca=cobranca, chatwoot_conversa_id=555)
        sessao.add(conversa)
        sessao.commit()
        _cliente(sessao, conversa, **mensagem)
        regras = [
            responder("alega_pagamento", True, se=lambda e: "já paguei" in e.historico[-1][1]),
            responder("objecao_valor", True, se=lambda e: "caro" in e.historico[-1][1]),
        ]
        deps = _deps(politica, transcritor=TranscritorFalso(fala), regras=regras)
        resultado = processar_turno(sessao, conversa.id, SEGUNDA_10H, deps)
        turno = sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()
        return resultado, turno.acao_principal, conversa.estado, conversa.motivo_humano


@pytest.mark.parametrize(
    "fala", ["quanto eu devo?", "tá caro, dá para fazer desconto?", "já paguei esse mês"]
)
def test_um_audio_tem_o_mesmo_efeito_que_o_texto_equivalente(politica, fala):
    como_audio = _rodar_em_banco_novo(politica, fala, anexos=[_audio()])
    como_texto = _rodar_em_banco_novo(politica, fala, texto=fala)

    assert como_audio == como_texto


def test_o_resultado_fica_no_proprio_anexo(sessao, ligada, politica):
    deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
    mensagem = _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert _anexo(sessao, mensagem) == {
        **_audio(),
        "resultado": "transcrito",
        "transcricao": "quanto eu devo?",
    }
    assert deps.chatwoot.baixados == [URL_AUDIO]


def test_o_audio_ja_transcrito_nao_e_transcrito_de_novo(sessao, ligada, politica):
    transcritor = TranscritorFalso("quanto eu devo?")
    deps = _deps(politica, transcritor=transcritor)
    deps.jev.falha = ValueError("erro de código no meio do turno")
    mensagem = _cliente(sessao, ligada, anexos=[_audio()])

    # O erro desfaz o turno e agenda outra tentativa: o áudio não é transcrito outra vez.
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ERRO
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H + timedelta(hours=1), deps) is (
        ResultadoTurno.ERRO
    )

    assert len(transcritor.chamadas) == 1
    assert len(deps.chatwoot.baixados) == 1
    assert _anexo(sessao, mensagem)["resultado"] == "transcrito"


def test_o_texto_junto_com_o_audio_chega_inteiro_ao_jev(sessao, ligada, politica):
    deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
    _cliente(sessao, ligada, texto="Oi, boa tarde", anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    estado = deps.jev.chamadas[0][1]
    assert estado.historico[-1] == (
        "CLIENTE",
        'Oi, boa tarde [áudio transcrito: "quanto eu devo?"]',
    )


def test_dois_audios_da_mesma_rajada_sao_transcritos_na_ordem(sessao, ligada, politica):
    transcritor = TranscritorFalso(respostas=["primeiro", "segundo"])
    deps = _deps(politica, transcritor=transcritor)
    _cliente(sessao, ligada, anexos=[_audio()], mid="cw-1")
    _cliente(sessao, ligada, anexos=[_audio()], mid="cw-2")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    historico = deps.jev.chamadas[0][1].historico
    assert [texto for _, texto in historico] == [
        '[áudio transcrito: "primeiro"]',
        '[áudio transcrito: "segundo"]',
    ]


class _ChegaOutroAudio:
    """Um transcritor em que o webhook grava outro áudio enquanto o primeiro é transcrito."""

    def __init__(self, sessao, conversa, *respostas):
        self._falso = TranscritorFalso(respostas=list(respostas))
        self._sessao = sessao
        self._conversa = conversa
        self._chegou = False

    @property
    def chamadas(self):
        return self._falso.chamadas

    def transcrever(self, dados, content_type):
        if not self._chegou:
            self._chegou = True
            _cliente(self._sessao, self._conversa, anexos=[_audio()], mid="cw-2")
        return self._falso.transcrever(dados, content_type)


def test_o_audio_que_chega_durante_a_transcricao_e_ouvido_antes_do_turno(sessao, ligada, politica):
    transcritor = _ChegaOutroAudio(sessao, ligada, "quanto eu devo?", "já paguei")
    deps = _deps(politica, transcritor=transcritor)
    primeiro = _cliente(sessao, ligada, anexos=[_audio()], mid="cw-1")

    # O turno não lê o 2º áudio sem transcrição: devolve a rajada e volta já.
    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.ADIADO
    assert deps.jev.chamadas == [] and ligada.ultima_tomada_id is None
    assert ligada.turno_em is not None and ligada.turno_em <= SEGUNDA_10H

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    historico = deps.jev.chamadas[0][1].historico
    assert [texto for _, texto in historico] == [
        '[áudio transcrito: "quanto eu devo?"]',
        '[áudio transcrito: "já paguei"]',
    ]
    segundo = sessao.scalars(
        select(Mensagem).where(Mensagem.id > primeiro.id, Mensagem.origem == OrigemMensagem.CLIENTE)
    ).one()
    assert segundo.anexos[0]["resultado"] == "transcrito" and len(transcritor.chamadas) == 2


def test_o_audio_que_chega_durante_a_transcricao_nao_gasta_o_pedido_de_texto(
    sessao, ligada, politica
):
    # O 1º áudio falha; o 2º, que ainda nem foi baixado, não pode ser dado como não ouvido.
    transcritor = _ChegaOutroAudio(
        sessao, ligada, Transcricao("falhou", None, "erro"), "quanto eu devo?"
    )
    deps = _deps(politica, transcritor=transcritor)
    _cliente(sessao, ligada, anexos=[_audio()], mid="cw-1")

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert [texto for _, texto in deps.jev.chamadas[0][1].historico] == [
        "[áudio — não foi possível ouvir]",
        '[áudio transcrito: "quanto eu devo?"]',
    ]
    assert not sessao.scalars(select(Turno).where(Turno.acao_principal == "FIXA:PEDIR_TEXTO")).all()


def test_o_audio_antigo_aparece_no_historico_dos_turnos_seguintes(sessao, ligada, politica):
    deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
    _cliente(sessao, ligada, anexos=[_audio()])
    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
    agora = SEGUNDA_10H + timedelta(minutes=5)
    _cliente(sessao, ligada, texto="e o desconto?", momento=agora, mid="cw-2")

    processar_turno(sessao, ligada.id, agora, deps)

    ultimo = deps.jev.chamadas[-1][1].historico
    assert ("CLIENTE", '[áudio transcrito: "quanto eu devo?"]') in ultimo
    assert ultimo[-1] == ("CLIENTE", "e o desconto?")


class TestOsGatesRodamAntes:
    def test_fora_da_janela_so_transcreve_quando_o_turno_roda(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("quanto eu devo?")
        deps = _deps(politica, transcritor=transcritor)
        _cliente(sessao, ligada, anexos=[_audio()], momento=SEGUNDA_19H)

        assert processar_turno(sessao, ligada.id, SEGUNDA_19H, deps) is ResultadoTurno.ADIADO
        assert transcritor.chamadas == [] and deps.chatwoot.baixados == []

        processar_turno(sessao, ligada.id, ligada.turno_em, deps)
        assert len(transcritor.chamadas) == 1

    def test_humano_na_conversa_nao_transcreve(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        ligada.human_owned = True
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
        assert transcritor.chamadas == [] and deps.chatwoot.baixados == []

    def test_cobranca_inativa_nao_transcreve(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        ligada.cobranca.status = StatusCobranca.PAGA
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.IGNORADO
        assert transcritor.chamadas == []

    def test_fora_das_24h_nao_transcreve(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        antigo = SEGUNDA_10H - timedelta(hours=25)
        _cliente(sessao, ligada, anexos=[_audio()], momento=antigo)

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.FIXA
        assert transcritor.chamadas == []

    def test_o_teto_nao_transcreve(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        for _ in range(deps.politica.teto_resolucao):
            registrar_mensagem(
                sessao, ligada.id, OrigemMensagem.AGENTE, "resposta", None, True, SEGUNDA_10H
            )
        _cliente(sessao, ligada, anexos=[_audio()])

        assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.FIXA
        assert transcritor.chamadas == []


def test_em_sombra_tambem_transcreve_e_nada_e_enviado(sessao, ligada, politica):
    transcritor = TranscritorFalso("quanto eu devo?")
    deps = _deps(politica, transcritor=transcritor, modo=Modo.SOMBRA)
    _cliente(sessao, ligada, anexos=[_audio()])

    assert processar_turno(sessao, ligada.id, SEGUNDA_10H, deps) is ResultadoTurno.SOMBRA

    assert len(transcritor.chamadas) == 1
    assert deps.jev.chamadas[0][1].historico[-1][1] == '[áudio transcrito: "quanto eu devo?"]'
    assert deps.chatwoot.enviadas(555) == []
    assert ligada.estado is EstadoConversa.INICIADO


def test_em_sombra_com_humano_na_conversa_transcreve_para_o_jev_ler(sessao, ligada, politica):
    transcritor = TranscritorFalso("quanto eu devo?")
    deps = _deps(politica, transcritor=transcritor, modo=Modo.SOMBRA)
    ligada.human_owned = True
    _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert len(transcritor.chamadas) == 1


class TestSemTranscricao:
    def test_desligada_o_audio_nao_e_baixado_e_fica_como_nao_ouvido(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=None)
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.baixados == []
        assert "resultado" not in _anexo(sessao, mensagem)
        historico = deps.jev.chamadas[0][1].historico
        assert historico[-1] == ("CLIENTE", "oi [áudio — não foi possível ouvir]")

    def test_o_download_que_falha_marca_falhou_e_nao_transcreve(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        deps.chatwoot.anexo = FalhaDownload("status")
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert transcritor.chamadas == []
        assert _anexo(sessao, mensagem)["resultado"] == "falhou"
        assert "transcricao" not in _anexo(sessao, mensagem)

    def test_anexo_sem_endereco_marca_falhou(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("oi"))
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio(url=None)])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert deps.chatwoot.baixados == []
        assert _anexo(sessao, mensagem)["resultado"] == "falhou"

    def test_a_transcricao_que_falha_marca_falhou_com_o_motivo(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso(motivo="timeout"))
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _anexo(sessao, mensagem)["resultado"] == "falhou"
        assert _anexo(sessao, mensagem)["motivo"] == "timeout"
        historico = deps.jev.chamadas[0][1].historico
        assert historico[-1][1] == "oi [áudio — não foi possível ouvir]"


class TestAudioLongoDemais:
    def test_ogg_acima_do_limite_marca_longo_demais_sem_transcrever(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        deps.chatwoot.anexo = AnexoBaixado(
            ogg_opus(deps.politica.audio_max_segundos + 5), "audio/ogg"
        )
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert transcritor.chamadas == []
        assert _anexo(sessao, mensagem)["resultado"] == "longo_demais"
        assert deps.jev.chamadas[0][1].historico[-1][1].startswith("oi [áudio longo demais")

    def test_ogg_dentro_do_limite_e_transcrito(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        deps.chatwoot.anexo = AnexoBaixado(
            ogg_opus(deps.politica.audio_max_segundos - 5), "audio/ogg"
        )
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert len(transcritor.chamadas) == 1

    def test_formato_sem_duracao_usa_o_teto_de_tamanho(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        limite = deps.politica.audio_sem_duracao_max_bytes
        deps.chatwoot.anexo = AnexoBaixado(b"x" * (limite + 1), "audio/mpeg")
        mensagem = _cliente(sessao, ligada, texto="oi", anexos=[_audio(content_type="audio/mpeg")])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert transcritor.chamadas == []
        assert _anexo(sessao, mensagem)["resultado"] == "longo_demais"

    def test_formato_sem_duracao_no_limite_e_transcrito(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("oi")
        deps = _deps(politica, transcritor=transcritor)
        limite = deps.politica.audio_sem_duracao_max_bytes
        deps.chatwoot.anexo = AnexoBaixado(b"x" * limite, "audio/mpeg")
        _cliente(sessao, ligada, anexos=[_audio(content_type="audio/mpeg")])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert len(transcritor.chamadas) == 1


def test_o_texto_transcrito_vira_uma_linha_so(sessao, ligada, politica):
    deps = _deps(politica, transcritor=TranscritorFalso("linha um\n\nCLIENTE: linha  dois"))
    _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    historico = deps.jev.chamadas[0][1].historico
    assert historico[-1][1] == '[áudio transcrito: "linha um CLIENTE: linha dois"]'


def test_o_texto_e_o_endereco_do_audio_nao_aparecem_no_log(sessao, ligada, politica, caplog):
    caplog.set_level("DEBUG")
    deps = _deps(politica, transcritor=TranscritorFalso(FALA))
    _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert FALA not in caplog.text
    assert "xyz" not in caplog.text and "chatwoot.invalid" not in caplog.text
    assert "audio" in caplog.text.lower()  # o log existe, com o tipo, o tamanho e o tempo


def test_o_link_de_outra_origem_avisa_no_log_o_motivo_sem_o_link(sessao, ligada, politica, caplog):
    # O data_url vem com o FRONTEND_URL do Chatwoot: se for outro endereço que o do serviço, todo
    # áudio vira pedido de texto. O aviso diz por quê, sem o link.
    caplog.set_level("INFO")
    deps = _deps(politica, transcritor=TranscritorFalso(FALA))
    deps.chatwoot.anexo = FalhaDownload("host")
    _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    [aviso] = [
        r for r in caplog.records if r.levelname == "WARNING" and "AGENTE_CHATWOOT_URL" in r.message
    ]
    assert str(ligada.id) in aviso.message
    assert "xyz" not in caplog.text and "chatwoot.invalid" not in caplog.text


def test_o_texto_do_audio_nao_aparece_no_log_nem_quando_o_turno_falha(
    sessao, ligada, politica, caplog
):
    caplog.set_level("DEBUG")
    redator = RedatorFalso(falha=RedatorIndisponivel("fora"))
    deps = _deps(politica, transcritor=TranscritorFalso(FALA), redator=redator)
    _cliente(sessao, ligada, anexos=[_audio()])

    processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

    assert FALA not in caplog.text


class TestRotulo:
    def test_texto_sem_anexo_e_o_proprio_texto(self, sessao, ligada):
        mensagem = _cliente(sessao, ligada, texto="  oi  ")
        assert texto_da_mensagem(mensagem) == "oi"

    def test_anexo_que_nao_e_audio_nao_vira_rotulo(self, sessao, ligada):
        mensagem = _cliente(sessao, ligada, anexos=[{"tipo": "image", "url": None}])
        assert texto_da_mensagem(mensagem) == ""

    @pytest.mark.parametrize(
        ("anexo", "rotulo"),
        [
            ({"resultado": "transcrito", "transcricao": "oi"}, '[áudio transcrito: "oi"]'),
            ({"resultado": "longo_demais"}, "[áudio longo demais — não ouvido]"),
            ({"resultado": "falhou"}, "[áudio — não foi possível ouvir]"),
            ({}, "[áudio — não foi possível ouvir]"),
        ],
    )
    def test_o_rotulo_de_cada_resultado(self, anexo, rotulo):
        assert rotulo_do_audio({"tipo": "audio", **anexo}) == rotulo

    def test_o_historico_de_mensagem_do_agente_nao_muda(self, sessao, ligada):
        mensagem = registrar_mensagem(
            sessao, ligada.id, OrigemMensagem.AGENTE, "Olá!", None, True, SEGUNDA_10H
        )
        assert isinstance(mensagem, Mensagem) and texto_da_mensagem(mensagem) == "Olá!"


class ChatwootSemNotaDeTranscricao(ChatwootFalso):
    """A nota privada da transcrição falha; as outras chamadas funcionam."""

    def nota_privada(self, conversa_id, texto):
        if texto.startswith(NOTA):
            raise httpx.ConnectError(f"o Chatwoot caiu com o texto: {texto}")
        super().nota_privada(conversa_id, texto)


NOTA = "Transcrição do áudio (agente): "


def _notas(deps):
    return [
        c[2] for c in deps.chatwoot.chamadas if c[0] == "nota_privada" and c[2].startswith(NOTA)
    ]


class TestNotaPrivadaComATranscricao:
    def test_a_nota_sai_uma_vez_por_audio(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso(respostas=["primeiro", "segundo"]))
        _cliente(sessao, ligada, anexos=[_audio()], mid="cw-1")
        _cliente(sessao, ligada, anexos=[_audio()], mid="cw-2")

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _notas(deps) == [f"{NOTA}primeiro", f"{NOTA}segundo"]
        assert all(c[1] == 555 for c in deps.chatwoot.chamadas if c[0] == "nota_privada")

    def test_o_turno_seguinte_nao_repete_a_nota(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        agora = SEGUNDA_10H + timedelta(minutes=5)
        _cliente(sessao, ligada, texto="e o desconto?", momento=agora, mid="cw-2")

        processar_turno(sessao, ligada.id, agora, deps)

        assert _notas(deps) == [f"{NOTA}quanto eu devo?"]

    def test_a_nota_vem_antes_da_resposta(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        tipos = [c[0] for c in deps.chatwoot.chamadas]
        assert tipos.index("nota_privada") < tipos.index("enviar_mensagem")

    def test_so_o_audio_transcrito_leva_nota(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso(motivo="timeout"))
        _cliente(sessao, ligada, texto="oi", anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _notas(deps) == []

    def test_a_falha_do_chatwoot_na_nota_nao_derruba_o_turno(
        self, sessao, ligada, politica, caplog
    ):
        deps = _deps(politica, transcritor=TranscritorFalso(FALA))
        deps.chatwoot = ChatwootSemNotaDeTranscricao()
        mensagem = _cliente(sessao, ligada, anexos=[_audio()])

        resultado = processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert resultado is ResultadoTurno.ENVIADO
        assert len(deps.chatwoot.enviadas(555)) == 1
        assert _anexo(sessao, mensagem)["resultado"] == "transcrito"
        # A falha vai para o log, sem o texto que o cliente falou.
        assert "nota" in caplog.text.lower() and FALA not in caplog.text

    def test_a_falha_da_nota_nao_transcreve_o_audio_de_novo(self, sessao, ligada, politica):
        transcritor = TranscritorFalso("quanto eu devo?")
        deps = _deps(politica, transcritor=transcritor)
        deps.chatwoot = ChatwootSemNotaDeTranscricao()
        _cliente(sessao, ligada, anexos=[_audio()])
        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)
        agora = SEGUNDA_10H + timedelta(minutes=5)
        _cliente(sessao, ligada, texto="oi", momento=agora, mid="cw-2")

        processar_turno(sessao, ligada.id, agora, deps)

        assert len(transcritor.chamadas) == 1

    def test_sem_conversa_no_chatwoot_nao_ha_nota_e_o_turno_segue(self, sessao, conversa, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"), modo=Modo.SOMBRA)
        _cliente(sessao, conversa, anexos=[_audio()])

        assert processar_turno(sessao, conversa.id, SEGUNDA_10H, deps) is ResultadoTurno.SOMBRA

        assert deps.chatwoot.chamadas == []

    def test_em_sombra_a_nota_sai_por_padrao(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"), modo=Modo.SOMBRA)
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _notas(deps) == [f"{NOTA}quanto eu devo?"]
        assert deps.chatwoot.enviadas(555) == []

    def test_em_sombra_a_nota_pode_ser_desligada(self, sessao, ligada, politica):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"), modo=Modo.SOMBRA)
        deps.politica = deps.politica.model_copy(update={"nota_transcricao_em_sombra": False})
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _notas(deps) == []

    def test_no_modo_ativo_a_nota_sai_mesmo_com_o_parametro_da_sombra_desligado(
        self, sessao, ligada, politica
    ):
        deps = _deps(politica, transcritor=TranscritorFalso("quanto eu devo?"))
        deps.politica = deps.politica.model_copy(update={"nota_transcricao_em_sombra": False})
        _cliente(sessao, ligada, anexos=[_audio()])

        processar_turno(sessao, ligada.id, SEGUNDA_10H, deps)

        assert _notas(deps) == [f"{NOTA}quanto eu devo?"]

    def test_a_nota_e_um_parametro_da_politica_ligado_por_padrao(self, politica):
        assert politica.nota_transcricao_em_sombra is True
