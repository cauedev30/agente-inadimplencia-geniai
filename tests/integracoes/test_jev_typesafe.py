"""O Jev da TypeSafe (T10, etapa 11). O contrato roda com um transporte falso; só o último teste
chama a API de verdade, com texto inventado, e é pulado sem a AGENTE_JEV_API_KEY."""

import json
import os
import threading
import time
from datetime import date

import httpx
import pytest

from agente.config import ConfigJev
from agente.dominio.desconto import Oferta
from agente.integracoes.jev import EstadoJev, JevComPrazo, JevIndisponivel, Pergunta
from agente.integracoes.jev_typesafe import (
    JevTypeSafe,
    estado_para_typesafe,
    laudo_da_resposta,
    pergunta_para_typesafe,
)
from agente.politica import Regua
from agente.tipos import EstadoConversa, Laudo
from agente.turno.jev1 import PERGUNTAS_JEV1
from agente.turno.jev2 import PERGUNTAS_JEV2

from .test_jev import ESTADO, PERGUNTAS, ContratoJev

# A chave do teste real, lida na importação: a suíte limpa as AGENTE_JEV_* do ambiente (conftest)
# para que exportar a chave não mande texto dos outros testes à rede.
CHAVE_REAL = os.environ.get("AGENTE_JEV_API_KEY")

# Texto que não pode sair em nenhuma mensagem de erro.
SEGREDO = "SEGREDO-DO-CLIENTE-123"
ECO = "ECO-DO-CORPO-DA-RESPOSTA"
ESTADO_COM_SEGREDO = EstadoJev(
    historico=(("CLIENTE", f"meu nome é {SEGREDO}"),),
    regua=Regua.R3,
    divida_centavos=10020,
    estado_conversa=EstadoConversa.ENGAJADO,
    oferta_atual=None,
)

BOOL = Pergunta("risco_juridico", "bool", "cita Procon")
ESCOLHA = Pergunta("acao", "escolha", "a ação principal", ("INFORM_DEBT", "NONE"))
SCORE = Pergunta("tom", "score", "o tom do cliente", ("cordial", "neutro", "irritado"))


def resposta_para(corpo: dict) -> dict:
    """Uma resposta válida da API para o pedido: noul 0,2; a 1ª opção; o 1º nível do score."""
    respostas = {}
    for nome, pergunta in corpo["questions"].items():
        if pergunta["type"] == "noul":
            respostas[nome] = {"type": "noul", "noul": 0.2}
        elif pergunta["type"] == "choice":
            primeira = next(iter(pergunta["criteria"]))
            respostas[nome] = {
                "type": "choice",
                "choice": primeira,
                "confidence": 0.9,
                "probabilities": {opcao: 0.1 for opcao in pergunta["criteria"]} | {primeira: 0.9},
            }
        else:
            niveis = len(pergunta["criteria"])
            respostas[nome] = {
                "type": "score",
                "score": 0.1,
                "confidence": 0.8,
                "probabilities": {str(i): 0.2 for i in range(niveis)} | {"0": 0.8},
            }
    return {"model": corpo["model"], "answers": respostas, "usage": {"input_tokens": 412}}


def jev_com(handler, **extra) -> JevTypeSafe:
    return JevTypeSafe(
        "chave-ficticia", timeout=1.0, transporte=httpx.MockTransport(handler), **extra
    )


def handler_ok(pedido: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=resposta_para(json.loads(pedido.content)))


class TestContrato(ContratoJev):
    @pytest.fixture
    def jev(self):
        with jev_com(handler_ok) as jev:
            yield jev


class TestPedido:
    def test_um_unico_post_com_todas_as_perguntas(self):
        pedidos = []

        def handler(pedido):
            pedidos.append(pedido)
            return handler_ok(pedido)

        with jev_com(handler) as jev:
            jev.avaliar(PERGUNTAS, ESTADO)

        assert len(pedidos) == 1
        pedido = pedidos[0]
        assert pedido.method == "POST"
        assert str(pedido.url) == "https://api.typesafe.ai/v1/systemone"
        assert pedido.headers["Authorization"] == "Bearer chave-ficticia"
        corpo = json.loads(pedido.content)
        assert corpo["model"] == "jev-1.14.0"
        assert list(corpo["questions"]) == ["risco_juridico", "tom", "acao"]
        assert [q["type"] for q in corpo["questions"].values()] == ["noul", "score", "choice"]
        assert corpo["state"] == estado_para_typesafe(ESTADO)

    def test_a_ordem_dos_criterios_do_score_e_a_das_opcoes(self):
        corpo = {}

        def handler(pedido):
            corpo.update(json.loads(pedido.content))
            return handler_ok(pedido)

        with jev_com(handler) as jev:
            jev.avaliar(PERGUNTAS, ESTADO)

        assert corpo["questions"]["tom"]["criteria"] == ["cordial", "neutro", "irritado"]
        assert list(corpo["questions"]["acao"]["criteria"]) == ["INFORM_DEBT", "NONE"]

    def test_modelo_e_url_configuraveis(self):
        pedidos = []

        def handler(pedido):
            pedidos.append(pedido)
            return handler_ok(pedido)

        with jev_com(handler, url="https://typesafe.invalid/", modelo="jev-1.14.0") as jev:
            jev.avaliar(PERGUNTAS, ESTADO)

        assert str(pedidos[0].url) == "https://typesafe.invalid/v1/systemone"
        assert json.loads(pedidos[0].content)["model"] == "jev-1.14.0"
        assert jev.origem == "typesafe:jev-1.14.0"

    def test_origem_padrao(self):
        with jev_com(handler_ok) as jev:
            assert jev.origem == "typesafe:jev-1.14.0"

    def test_o_gerenciador_de_contexto_fecha_o_cliente(self):
        with jev_com(handler_ok) as jev:
            pass
        assert jev._cliente.is_closed


class TestPerguntaParaTypesafe:
    def test_bool_vira_noul_so_com_instrucoes(self):
        assert pergunta_para_typesafe(BOOL) == {"type": "noul", "instructions": "cita Procon"}

    def test_escolha_vira_choice_com_as_opcoes(self):
        assert pergunta_para_typesafe(ESCOLHA) == {
            "type": "choice",
            "instructions": "a ação principal",
            "criteria": {"INFORM_DEBT": "INFORM_DEBT", "NONE": "NONE"},
        }

    def test_score_vira_score_com_as_opcoes_em_ordem(self):
        assert pergunta_para_typesafe(SCORE) == {
            "type": "score",
            "instructions": "o tom do cliente",
            "criteria": ["cordial", "neutro", "irritado"],
        }

    def test_escolha_com_criterios_leva_a_descricao_de_cada_opcao(self):
        pergunta = Pergunta(
            "acao", "escolha", "a ação", ("INFORM_DEBT", "NONE"), ("quer saber o valor", "só leu")
        )
        assert pergunta_para_typesafe(pergunta)["criteria"] == {
            "INFORM_DEBT": "quer saber o valor",
            "NONE": "só leu",
        }

    def test_score_com_criterios_leva_a_descricao_de_cada_nivel_em_ordem(self):
        pergunta = Pergunta(
            "tom", "score", "o tom", ("cordial", "neutro", "irritado"), ("gentil", "seco", "bravo")
        )
        assert pergunta_para_typesafe(pergunta)["criteria"] == ["gentil", "seco", "bravo"]

    def test_o_corpo_do_pedido_leva_as_descricoes_so_onde_ha_criterios(self):
        corpo = {}

        def handler(pedido):
            corpo.update(json.loads(pedido.content))
            return handler_ok(pedido)

        com_criterios = Pergunta("acao", "escolha", "a ação", ("A", "B"), ("quando A", "quando B"))
        with jev_com(handler) as jev:
            jev.avaliar((com_criterios, SCORE), ESTADO)

        assert corpo["questions"]["acao"]["criteria"] == {"A": "quando A", "B": "quando B"}
        assert corpo["questions"]["tom"]["criteria"] == ["cordial", "neutro", "irritado"]


class TestPerguntasDoSistemaTemCriterios:
    @pytest.mark.parametrize("nome", ["acao", "tom", "intencao_pagar"])
    def test_escolha_e_score_levam_criterios(self, nome):
        todas = {p.nome: p for p in (*PERGUNTAS_JEV1, *PERGUNTAS_JEV2)}
        pergunta = todas[nome]
        assert pergunta.criterios is not None
        assert len(pergunta.criterios) == len(pergunta.opcoes)


class TestEstadoParaTypesafe:
    def test_estado_basico(self):
        estado = estado_para_typesafe(ESTADO)
        assert estado == {
            "historico": [{"origem": "CLIENTE", "texto": "vou procurar o Procon"}],
            "regua": "R3",
            "divida_reais": 100.20,
            "estado_conversa": "ENGAJADO",
        }

    def test_leva_a_oferta_a_leitura_o_texto_as_acoes_e_os_numeros(self):
        estado = EstadoJev(
            historico=(("CLIENTE", "oi"), ("AGENTE", "olá")),
            regua=Regua.R3,
            divida_centavos=10020,
            estado_conversa=EstadoConversa.EM_NEGOCIACAO,
            oferta_atual=Oferta(degrau=1, desconto_centavos=2000, valor_final_centavos=8020),
            leitura=(Laudo("risco_juridico", False, 0.9, 0.9), Laudo("tom", "neutro", 0.7, 0.6)),
            texto_candidato="Posso oferecer um desconto.",
            acoes_do_turno=("GENERATE_PIX",),
            numeros={"divida_centavos": 10020},
        )
        resultado = estado_para_typesafe(estado)
        assert resultado["oferta"] == {
            "degrau": 1,
            "desconto_reais": 20.0,
            "valor_final_reais": 80.2,
        }
        assert resultado["leitura_jev1"] == [
            {"pergunta": "risco_juridico", "valor": False, "probabilidade": 0.9},
            {"pergunta": "tom", "valor": "neutro", "probabilidade": 0.7},
        ]
        assert resultado["texto_candidato"] == "Posso oferecer um desconto."
        assert resultado["acoes_do_turno"] == ["GENERATE_PIX"]
        assert resultado["numeros"] == {"divida_centavos": 10020}
        assert [m["origem"] for m in resultado["historico"]] == ["CLIENTE", "AGENTE"]

    def test_o_estado_e_serializavel_em_json(self):
        json.dumps(estado_para_typesafe(ESTADO))


class TestLaudoDaResposta:
    def test_noul_baixo_vira_nao_com_a_probabilidade_do_nao(self):
        laudo = laudo_da_resposta(BOOL, {"type": "noul", "noul": 0.3})
        assert laudo.valor is False
        assert laudo.probabilidade == pytest.approx(0.7)
        assert laudo.confianca == pytest.approx(0.7)

    def test_noul_alto_vira_sim_com_a_propria_probabilidade(self):
        laudo = laudo_da_resposta(BOOL, {"type": "noul", "noul": 0.98})
        assert laudo == Laudo("risco_juridico", True, 0.98, 0.98)

    def test_noul_em_0_5_e_sim(self):
        laudo = laudo_da_resposta(BOOL, {"type": "noul", "noul": 0.5})
        assert laudo.valor is True
        assert laudo.probabilidade == 0.5

    def test_choice_leva_a_probabilidade_da_escolhida_e_a_confianca(self):
        resposta = {
            "type": "choice",
            "choice": "NONE",
            "confidence": 0.82,
            "probabilities": {"INFORM_DEBT": 0.2, "NONE": 0.8},
        }
        assert laudo_da_resposta(ESCOLHA, resposta) == Laudo("acao", "NONE", 0.8, 0.82)

    def test_score_vira_a_opcao_de_maior_probabilidade(self):
        resposta = {
            "type": "score",
            "score": 1.7,
            "confidence": 0.6,
            "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
        }
        assert laudo_da_resposta(SCORE, resposta) == Laudo("tom", "irritado", 0.6, 0.6)

    def test_score_com_a_probabilidade_fora_de_ordem(self):
        resposta = {
            "confidence": 0.9,
            "probabilities": {"2": 0.05, "0": 0.05, "1": 0.9},
        }
        assert laudo_da_resposta(SCORE, resposta).valor == "neutro"

    def test_score_com_a_chave_igual_ao_nome_da_opcao(self):
        resposta = {
            "confidence": 0.7,
            "probabilities": {"cordial": 0.1, "neutro": 0.2, "irritado": 0.7},
        }
        assert laudo_da_resposta(SCORE, resposta) == Laudo("tom", "irritado", 0.7, 0.7)

    def test_noul_enorme_nao_estoura_e_vira_indisponivel(self):
        with pytest.raises(JevIndisponivel, match="risco_juridico.*probabilidade"):
            laudo_da_resposta(BOOL, {"type": "noul", "noul": 10**400})


class TestErros:
    @pytest.mark.parametrize("status", [400, 401, 422, 429, 500, 503])
    def test_http_de_erro_vira_indisponivel_so_com_o_codigo(self, status):
        def handler(pedido):
            return httpx.Response(status, text=f"{ECO} {SEGREDO}")

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert str(status) in str(erro.value)
        assert SEGREDO not in str(erro.value) and ECO not in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None

    def test_timeout_vira_indisponivel_com_o_tipo(self):
        def handler(pedido):
            raise httpx.ReadTimeout(f"demorou {SEGREDO}", request=pedido)

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert SEGREDO not in str(erro.value)
        assert "prazo" in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None

    def test_falha_de_rede_vira_indisponivel_com_o_tipo(self):
        def handler(pedido):
            raise httpx.ConnectError(f"sem rota {SEGREDO}", request=pedido)

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert "ConnectError" in str(erro.value)
        assert SEGREDO not in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None

    @pytest.mark.parametrize("conteudo", [f"{ECO} isto não é json", "[1, 2]", '"texto"'])
    def test_resposta_que_nao_e_um_objeto_json_vira_indisponivel(self, conteudo):
        def handler(pedido):
            return httpx.Response(200, content=conteudo)

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert ECO not in str(erro.value)
        assert erro.value.__cause__ is None

    def _com_resposta(self, alterar):
        def handler(pedido):
            dados = resposta_para(json.loads(pedido.content))
            alterar(dados["answers"])
            # Texto direto: o json= do httpx recusa o NaN que o teste quer mandar.
            return httpx.Response(200, content=json.dumps(dados))

        return handler

    def _falha(self, handler) -> str:
        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)
        mensagem = str(erro.value)
        assert SEGREDO not in mensagem and ECO not in mensagem
        return mensagem

    def test_resposta_sem_a_pergunta(self):
        mensagem = self._falha(self._com_resposta(lambda respostas: respostas.pop("tom")))
        assert "tom" in mensagem

    def test_sem_o_campo_answers(self):
        def handler(pedido):
            return httpx.Response(200, json={"model": "jev-1.14.0", "usage": {}})

        assert "answers" in self._falha(handler)

    def test_choice_fora_das_opcoes(self):
        def alterar(respostas):
            respostas["acao"]["choice"] = f"{ECO}"

        mensagem = self._falha(self._com_resposta(alterar))
        assert "acao" in mensagem

    def test_choice_sem_a_probabilidade_da_escolhida(self):
        def alterar(respostas):
            respostas["acao"]["probabilities"] = {"NONE": 1.0}

        assert "acao" in self._falha(self._com_resposta(alterar))

    @pytest.mark.parametrize(
        "valor",
        [
            "alto",
            None,
            True,
            [0.3],
            1.5,
            -0.1,
            float("nan"),
            pytest.param(10**400, id="inteiro-enorme"),
            pytest.param(-(10**400), id="inteiro-enorme-negativo"),
        ],
    )
    def test_noul_que_nao_e_probabilidade(self, valor):
        def alterar(respostas):
            respostas["risco_juridico"]["noul"] = valor

        assert "risco_juridico" in self._falha(self._com_resposta(alterar))

    def test_score_com_nivel_que_nao_existe(self):
        def alterar(respostas):
            respostas["tom"]["probabilities"] = {"7": 1.0}

        assert "tom" in self._falha(self._com_resposta(alterar))

    def test_confianca_fora_de_0_a_1(self):
        def alterar(respostas):
            respostas["tom"]["confidence"] = 3

        assert "tom" in self._falha(self._com_resposta(alterar))

    def test_resposta_da_pergunta_que_nao_e_objeto(self):
        def alterar(respostas):
            respostas["tom"] = f"{ECO}"

        assert "tom" in self._falha(self._com_resposta(alterar))

    def test_pelo_jev_com_prazo_a_mensagem_continua_sem_dado(self):
        def handler(pedido):
            return httpx.Response(500, text=f"{ECO} {SEGREDO}")

        with jev_com(handler) as real:
            jev = JevComPrazo(real, timeout_ms=2000)
            with pytest.raises(JevIndisponivel) as erro:
                jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert "500" in str(erro.value)
        assert SEGREDO not in str(erro.value) and ECO not in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None


class CorpoLento(httpx.SyncByteStream):
    """Um corpo que entrega um pedaço por vez, com uma pausa antes de cada um."""

    def __init__(self, pedacos: list[bytes], pausa: float):
        self.pedacos = pedacos
        self.pausa = pausa
        self.fechado = False

    def __iter__(self):
        for pedaco in self.pedacos:
            time.sleep(self.pausa)
            yield pedaco

    def close(self) -> None:
        self.fechado = True


def _em_pedacos(conteudo: bytes, quantos: int) -> list[bytes]:
    tamanho = -(-len(conteudo) // quantos)
    return [conteudo[i : i + tamanho] for i in range(0, len(conteudo), tamanho)]


class TestPrazoTotal:
    """O `timeout` do httpx vale por fase: o prazo total da chamada é do JevTypeSafe."""

    def test_servidor_que_pinga_aos_poucos_nao_mantem_a_chamada_viva(self):
        corpos = []

        def handler(pedido):
            dados = json.dumps(resposta_para(json.loads(pedido.content))).encode()
            corpo = CorpoLento(_em_pedacos(dados, 6), pausa=0.1)
            corpos.append(corpo)
            return httpx.Response(200, stream=corpo)

        jev = JevTypeSafe("chave-ficticia", timeout=0.25, transporte=httpx.MockTransport(handler))
        with jev:
            inicio = time.monotonic()
            with pytest.raises(JevIndisponivel) as erro:
                jev.avaliar(PERGUNTAS, ESTADO)
            duracao = time.monotonic() - inicio

        assert "prazo" in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None
        assert duracao < 0.5
        assert corpos[0].fechado
        assert jev.ultimo_uso is None and jev.uso_total == 0

    def test_corpo_normal_e_rapido_em_varios_pedacos_continua_funcionando(self):
        def handler(pedido):
            dados = json.dumps(resposta_para(json.loads(pedido.content))).encode()
            return httpx.Response(200, stream=CorpoLento(_em_pedacos(dados, 6), pausa=0.0))

        with jev_com(handler) as jev:
            laudos = jev.avaliar(PERGUNTAS, ESTADO)
            assert [laudo.pergunta for laudo in laudos] == [p.nome for p in PERGUNTAS]
            assert jev.ultimo_uso == 412 and jev.uso_total == 412

    def test_o_http_de_erro_nao_le_o_corpo(self):
        class NaoLer(httpx.SyncByteStream):
            lido = False

            def __iter__(self):
                NaoLer.lido = True
                yield f"{ECO} {SEGREDO}".encode()

        def handler(pedido):
            return httpx.Response(503, stream=NaoLer())

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert "503" in str(erro.value)
        assert not NaoLer.lido

    def test_o_timeout_por_fase_continua_igual_ao_prazo(self):
        with jev_com(handler_ok) as jev:
            assert jev._cliente.timeout == httpx.Timeout(1.0)


class CorpoMudo(httpx.SyncByteStream):
    """Entrega 1 pedaço e some: espera até o `close` (como um socket parado até ser fechado).
    Depois da espera, termina limpo ou levanta o erro dado, como o socket fechado faria."""

    def __init__(self, erro: Exception | None = None):
        self.erro = erro
        self.acordar = threading.Event()
        self.fechado = False

    def __iter__(self):
        yield b"{"
        self.acordar.wait(10)
        if self.erro is not None:
            raise self.erro

    def close(self) -> None:
        self.fechado = True
        self.acordar.set()


def _threads_timer() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if isinstance(t, threading.Timer)]


class TestVigiaDoPrazo:
    """O servidor que manda os cabeçalhos e 1 pedaço e depois fica mudo: ninguém confere o relógio
    entre pedaços, então um vigia fecha a resposta no prazo."""

    @pytest.mark.parametrize(
        "erro",
        [
            None,
            httpx.ReadError(f"fechado {SEGREDO}"),
            httpx.ReadTimeout(f"demorou {SEGREDO}"),
            httpx.StreamClosed(),
            RuntimeError(f"fechada {SEGREDO}"),
        ],
        ids=["termina-limpo", "ReadError", "ReadTimeout", "StreamClosed", "RuntimeError"],
    )
    def test_servidor_mudo_depois_de_um_pedaco_estoura_no_prazo(self, erro):
        corpos = []

        def handler(pedido):
            corpo = CorpoMudo(erro)
            corpos.append(corpo)
            return httpx.Response(200, stream=corpo)

        jev = JevTypeSafe("chave-ficticia", timeout=0.3, transporte=httpx.MockTransport(handler))
        with jev:
            inicio = time.monotonic()
            with pytest.raises(JevIndisponivel) as falha:
                jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)
            duracao = time.monotonic() - inicio

        assert str(falha.value) == "a TypeSafe passou do prazo"
        assert SEGREDO not in str(falha.value)
        assert falha.value.__context__ is None and falha.value.__cause__ is None
        assert duracao < 0.9
        assert corpos[0].fechado
        assert jev.ultimo_uso is None and jev.uso_total == 0

    def test_o_vigia_nao_fecha_o_cliente_e_a_chamada_seguinte_funciona(self):
        chamadas = []

        def handler(pedido):
            chamadas.append(pedido)
            if len(chamadas) == 1:
                return httpx.Response(200, stream=CorpoMudo())
            return handler_ok(pedido)

        jev = JevTypeSafe("chave-ficticia", timeout=0.2, transporte=httpx.MockTransport(handler))
        with jev:
            with pytest.raises(JevIndisponivel):
                jev.avaliar(PERGUNTAS, ESTADO)
            laudos = jev.avaliar(PERGUNTAS, ESTADO)

        assert [laudo.pergunta for laudo in laudos] == [p.nome for p in PERGUNTAS]

    def test_corpo_em_varios_pedacos_dentro_do_prazo_continua_passando(self):
        def handler(pedido):
            dados = json.dumps(resposta_para(json.loads(pedido.content))).encode()
            return httpx.Response(200, stream=CorpoLento(_em_pedacos(dados, 4), pausa=0.02))

        with jev_com(handler) as jev:
            laudos = jev.avaliar(PERGUNTAS, ESTADO)

        assert [laudo.pergunta for laudo in laudos] == [p.nome for p in PERGUNTAS]

    def test_o_vigia_e_cancelado_no_caminho_feliz(self):
        antes = len(_threads_timer())
        with jev_com(handler_ok) as jev:
            jev.avaliar(PERGUNTAS, ESTADO)
            # O Timer cancelado acorda e sai; o join dá um instante para a thread terminar.
            for timer in _threads_timer():
                timer.join(timeout=1.0)

        assert len(_threads_timer()) == antes

    def test_o_vigia_e_cancelado_quando_o_corpo_falha(self):
        def handler(pedido):
            return httpx.Response(200, content=f"{ECO} {SEGREDO}")

        antes = len(_threads_timer())
        with jev_com(handler) as jev, pytest.raises(JevIndisponivel):
            jev.avaliar(PERGUNTAS, ESTADO)
        for timer in _threads_timer():
            timer.join(timeout=1.0)

        assert len(_threads_timer()) == antes

    def test_json_invalido_nao_carrega_o_corpo_nem_como_contexto(self):
        def handler(pedido):
            return httpx.Response(200, content=f"{ECO} {SEGREDO} isto não é json")

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert SEGREDO not in str(erro.value) and ECO not in str(erro.value)
        assert erro.value.__context__ is None and erro.value.__cause__ is None

    def test_falha_de_rede_nao_carrega_o_erro_como_contexto(self):
        def handler(pedido):
            raise httpx.ConnectError(f"sem rota {SEGREDO}", request=pedido)

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, ESTADO_COM_SEGREDO)

        assert erro.value.__context__ is None and erro.value.__cause__ is None


class TestSerializacaoDoPedido:
    def _falha(self, estado: EstadoJev) -> JevIndisponivel:
        pedidos = []

        def handler(pedido):
            pedidos.append(pedido)
            return handler_ok(pedido)

        with jev_com(handler) as jev, pytest.raises(JevIndisponivel) as erro:
            jev.avaliar(PERGUNTAS, estado)

        assert pedidos == []
        assert "montar o pedido" in str(erro.value)
        assert "JSON" not in str(erro.value)
        assert erro.value.__suppress_context__ and erro.value.__cause__ is None
        return erro.value

    def _estado_com(self, valor) -> EstadoJev:
        return EstadoJev(
            historico=(("CLIENTE", f"meu nome é {SEGREDO}"),),
            regua=Regua.R3,
            divida_centavos=10020,
            estado_conversa=EstadoConversa.ENGAJADO,
            oferta_atual=None,
            numeros={"x": valor},
        )

    def test_nan_nos_numeros(self):
        self._falha(self._estado_com(float("nan")))

    def test_infinito_nos_numeros(self):
        self._falha(self._estado_com(float("inf")))

    def test_date_nos_numeros(self):
        erro = self._falha(self._estado_com(date(2026, 11, 3)))
        assert "2026" not in str(erro)

    def test_o_pedido_sai_como_json_utf8_com_o_cabecalho(self):
        pedidos = []

        def handler(pedido):
            pedidos.append(pedido)
            return handler_ok(pedido)

        estado = EstadoJev(
            historico=(("CLIENTE", "não tenho dinheiro até sexta"),),
            regua=Regua.R3,
            divida_centavos=10020,
            estado_conversa=EstadoConversa.ENGAJADO,
            oferta_atual=None,
        )
        with jev_com(handler) as jev:
            jev.avaliar(PERGUNTAS, estado)

        assert pedidos[0].headers["Content-Type"] == "application/json"
        assert "não tenho dinheiro".encode() in pedidos[0].content


class TestDaConfig:
    def test_usa_a_chave_a_url_o_modelo_e_75_por_cento_do_prazo(self):
        config = ConfigJev(
            jev_api_key="chave-ficticia", jev_url="https://typesafe.invalid", jev_modelo="jev-x"
        )
        with JevTypeSafe.da_config(config, 2000) as jev:
            assert jev.origem == "typesafe:jev-x"
            assert jev._cliente.timeout.read == 1.5
            assert str(jev._cliente.base_url) == "https://typesafe.invalid"
            assert jev._cliente.headers["Authorization"] == "Bearer chave-ficticia"

    def test_sem_a_chave_e_um_erro_de_programacao(self):
        with pytest.raises(ValueError, match="AGENTE_JEV_API_KEY"):
            JevTypeSafe.da_config(ConfigJev(jev_api_key=None), 2000)


class TestUso:
    def test_guarda_os_tokens_de_entrada_da_ultima_chamada(self):
        with jev_com(handler_ok) as jev:
            assert jev.ultimo_uso is None
            jev.avaliar(PERGUNTAS, ESTADO)
            assert jev.ultimo_uso == 412

    def test_sem_o_campo_de_uso_fica_none(self):
        def handler(pedido):
            dados = resposta_para(json.loads(pedido.content))
            del dados["usage"]
            return httpx.Response(200, json=dados)

        with jev_com(handler) as jev:
            jev.avaliar(PERGUNTAS, ESTADO)
            assert jev.ultimo_uso is None

    def test_soma_o_uso_de_todas_as_chamadas(self):
        with jev_com(handler_ok) as jev:
            assert jev.uso_total == 0
            jev.avaliar(PERGUNTAS, ESTADO)
            jev.avaliar(PERGUNTAS, ESTADO)
            assert jev.uso_total == 824

    def test_a_chamada_que_falha_nao_deixa_o_uso_da_anterior(self):
        respostas = iter([200, 500])

        def handler(pedido):
            if next(respostas) == 200:
                return handler_ok(pedido)
            return httpx.Response(500)

        with jev_com(handler) as jev:
            jev.avaliar(PERGUNTAS, ESTADO)
            with pytest.raises(JevIndisponivel):
                jev.avaliar(PERGUNTAS, ESTADO)
            assert jev.ultimo_uso is None


@pytest.mark.skipif(not CHAVE_REAL, reason="sem a AGENTE_JEV_API_KEY: não chama a API")
class TestTypesafeDeVerdade(ContratoJev):
    """A API real, com o texto inventado de PERGUNTAS e ESTADO. Custa tokens de entrada."""

    @pytest.fixture
    def jev(self):
        with JevTypeSafe(CHAVE_REAL, timeout=10.0) as jev:
            yield jev

    def test_o_cliente_que_cita_o_procon_levanta_o_risco_juridico(self, jev):
        juridico, _, _ = jev.avaliar(PERGUNTAS, ESTADO)
        assert juridico.valor is True
        assert jev.ultimo_uso and jev.ultimo_uso > 0
