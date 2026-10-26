"""Contrato do Jev. A falsa passa agora; a real (T10) herda de ContratoJev."""

import threading

import pytest

from agente.integracoes.jev import (
    EstadoJev,
    JevComPrazo,
    JevFalso,
    JevIndisponivel,
    Pergunta,
    conferir_laudos,
    responder,
)
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo

PERGUNTAS = [
    Pergunta("risco_juridico", "bool", "cita Procon, advogado, processo, ação"),
    Pergunta("tom", "score", "o tom do cliente", ("cordial", "neutro", "irritado")),
    Pergunta("acao", "escolha", "a ação principal", ("INFORM_DEBT", "NONE")),
]
ESTADO = EstadoJev(
    historico=(("CLIENTE", "vou procurar o Procon"),),
    regua=Regua.R3,
    divida_centavos=10020,
    estado_conversa=EstadoConversa.ENGAJADO,
    oferta_atual=None,
)


class ContratoJev:
    def test_toda_pergunta_tem_um_laudo_na_ordem(self, jev):
        laudos = jev.avaliar(PERGUNTAS, ESTADO)
        assert [laudo.pergunta for laudo in laudos] == [p.nome for p in PERGUNTAS]

    def test_o_valor_tem_o_tipo_da_pergunta(self, jev):
        juridico, tom, acao = jev.avaliar(PERGUNTAS, ESTADO)
        assert isinstance(juridico.valor, bool)
        assert tom.valor in PERGUNTAS[1].opcoes
        assert acao.valor in PERGUNTAS[2].opcoes

    def test_se_identifica_para_a_calibracao(self, jev):
        assert isinstance(jev.origem, str) and jev.origem

    def test_probabilidade_e_confianca_entre_0_e_1(self, jev):
        for laudo in jev.avaliar(PERGUNTAS, ESTADO):
            assert 0 <= laudo.probabilidade <= 1
            assert 0 <= laudo.confianca <= 1


class TestJevFalso(ContratoJev):
    @pytest.fixture
    def jev(self):
        return JevFalso()

    def test_responde_pelas_regras(self):
        jev = JevFalso([responder("risco_juridico", True, 0.91, 0.80)])
        juridico, tom, _ = jev.avaliar(PERGUNTAS, ESTADO)
        assert juridico == Laudo("risco_juridico", True, 0.91, 0.80)
        # Sem regra, a falsa dá a resposta neutra.
        assert tom.valor == "cordial"

    def test_a_regra_pode_depender_do_estado(self):
        procon = responder(
            "risco_juridico", True, se=lambda estado: "Procon" in estado.historico[-1][1]
        )
        jev = JevFalso([procon])
        assert jev.avaliar(PERGUNTAS, ESTADO)[0].valor is True
        sem_procon = EstadoJev((("CLIENTE", "oi"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)
        assert jev.avaliar(PERGUNTAS, sem_procon)[0].valor is False

    def test_a_primeira_regra_que_responde_vence(self):
        jev = JevFalso([responder("tom", "irritado"), responder("tom", "neutro")])
        assert jev.avaliar(PERGUNTAS, ESTADO)[1].valor == "irritado"

    def test_a_origem_da_falsa_e_falso(self):
        assert JevFalso().origem == "falso"

    def test_grava_as_chamadas_so_quando_o_teste_liga(self):
        jev = JevFalso(registrar=True)
        jev.avaliar(PERGUNTAS, ESTADO)
        assert jev.chamadas == [(["risco_juridico", "tom", "acao"], ESTADO)]

    def test_por_padrao_nao_guarda_o_historico_do_cliente(self):
        # Em produção, a falsa não pode acumular dado pessoal na memória.
        jev = JevFalso()
        jev.avaliar(PERGUNTAS, ESTADO)
        assert jev.chamadas == []


class TestJevComPrazo(ContratoJev):
    @pytest.fixture
    def jev(self):
        return JevComPrazo(JevFalso(), timeout_ms=1000)

    def test_a_origem_e_a_do_jev_envolvido(self):
        assert JevComPrazo(JevFalso(origem="jev-teste"), timeout_ms=1000).origem == "jev-teste"

    def test_tempo_estourado_levanta_jev_indisponivel(self):
        jev = JevComPrazo(JevFalso(demora_s=0.5), timeout_ms=20)
        with pytest.raises(JevIndisponivel, match="20 ms"):
            jev.avaliar(PERGUNTAS, ESTADO)

    def test_erro_do_jev_vira_jev_indisponivel(self):
        jev = JevComPrazo(JevFalso(falha=ConnectionError("gateway fora")), timeout_ms=1000)
        with pytest.raises(JevIndisponivel, match="gateway fora"):
            jev.avaliar(PERGUNTAS, ESTADO)

    def test_laudo_faltando_vira_jev_indisponivel(self):
        class JevIncompleto:
            def avaliar(self, perguntas, estado):
                return JevFalso().avaliar(perguntas, estado)[:-1]

        with pytest.raises(JevIndisponivel, match="acao"):
            JevComPrazo(JevIncompleto(), timeout_ms=1000).avaliar(PERGUNTAS, ESTADO)

    def test_laudo_malformado_da_real_vira_jev_indisponivel(self):
        # Probabilidade que não é número, ou objeto que não é Laudo: nada de TypeError no turno.
        class JevMalformado:
            def __init__(self, laudos):
                self.laudos = laudos

            def avaliar(self, perguntas, estado):
                return self.laudos

        bom = JevFalso().avaliar(PERGUNTAS, ESTADO)
        sem_numero = [Laudo("risco_juridico", True, "alta", 0.9), *bom[1:]]
        for laudos in (sem_numero, ["sim", "sim", "sim"], None):
            with pytest.raises(JevIndisponivel):
                JevComPrazo(JevMalformado(laudos), timeout_ms=1000).avaliar(PERGUNTAS, ESTADO)

    def test_a_chamada_atrasada_roda_em_thread_daemon(self):
        # Uma chamada travada não pode segurar o desligamento do processo.
        visto = []
        solta = threading.Event()

        class JevTravado:
            def avaliar(self, perguntas, estado):
                visto.append(threading.current_thread().daemon)
                solta.wait(5)
                return []

        try:
            with pytest.raises(JevIndisponivel, match="20 ms"):
                JevComPrazo(JevTravado(), timeout_ms=20).avaliar(PERGUNTAS, ESTADO)
        finally:
            solta.set()
        assert visto == [True]


def test_conferir_recusa_valor_fora_das_opcoes():
    laudos = [
        Laudo("risco_juridico", True, 0.9, 0.9),
        Laudo("tom", "furioso", 0.9, 0.9),
        Laudo("acao", "NONE", 0.9, 0.9),
    ]
    with pytest.raises(JevIndisponivel, match="tom"):
        conferir_laudos(PERGUNTAS, laudos)


def test_conferir_recusa_bool_que_nao_e_bool():
    laudos = JevFalso().avaliar(PERGUNTAS, ESTADO)
    laudos[0] = Laudo("risco_juridico", "sim", 0.9, 0.9)
    with pytest.raises(JevIndisponivel, match="risco_juridico"):
        conferir_laudos(PERGUNTAS, laudos)


def test_conferir_recusa_probabilidade_fora_de_0_a_1():
    laudos = JevFalso().avaliar(PERGUNTAS, ESTADO)
    laudos[0] = Laudo("risco_juridico", True, 1.2, 0.9)
    with pytest.raises(JevIndisponivel, match="probabilidade"):
        conferir_laudos(PERGUNTAS, laudos)


def test_a_pergunta_aceita_so_o_valor_do_seu_tipo():
    juridico, tom, _ = PERGUNTAS
    assert juridico.aceita(True) and not juridico.aceita("sim") and not juridico.aceita(1)
    assert tom.aceita("neutro") and not tom.aceita("furioso")


def test_pergunta_de_escolha_exige_opcoes():
    with pytest.raises(ValueError, match="opções"):
        Pergunta("acao", "escolha", "a ação principal")
