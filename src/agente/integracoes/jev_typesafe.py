"""O Jev real pela API direta da TypeSafe (`POST /v1/systemone`), T10.

Um batch de perguntas vira uma só chamada. O mapeamento de tipos e o do retorno para o Laudo estão
em `specs/2026-11-03-etapa-11-jev-typesafe.md`. A mensagem de todo erro leva só o código HTTP ou o
tipo do erro e o nome da pergunta, nunca o estado nem o corpo da resposta: ela vai para o log e para
o turno, e o estado traz a conversa do cliente.

Zero Data Retention e No Training (D12) ainda não estão confirmados pela TypeSafe: por isso a
`AGENTE_JEV_DADOS_REAIS_LIBERADOS` (config.py) trava o uso com conversa real.
"""

import json
import math
import threading
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from agente.integracoes.jev import EstadoJev, JevIndisponivel, Pergunta
from agente.tipos import Laudo

if TYPE_CHECKING:
    from agente.config import ConfigJev

URL_TYPESAFE = "https://api.typesafe.ai"
MODELO_PADRAO = "jev-1.14.0"
CAMINHO = "/v1/systemone"

# Os nomes dos campos da resposta, num lugar só: o parsing inteiro fica em `laudo_da_resposta`.
CAMPO_RESPOSTAS = "answers"
CAMPO_USO = "usage"
CAMPO_TOKENS_DE_ENTRADA = "input_tokens"
CAMPO_NOUL = "noul"
CAMPO_ESCOLHA = "choice"
CAMPO_CONFIANCA = "confidence"
CAMPO_PROBABILIDADES = "probabilities"


def pergunta_para_typesafe(pergunta: Pergunta) -> dict:
    """bool vira noul; escolha vira choice, com `criteria` = {opção: quando ela se aplica}; score
    vira score, com `criteria` = a descrição de cada nível, em ordem (a posição é o nível, do menor
    para o maior). Sem `criterios` na pergunta, a descrição de cada opção é o próprio nome dela, e a
    TypeSafe a interpreta só pelo nome."""
    base: dict[str, Any] = {"instructions": pergunta.descricao}
    opcoes = pergunta.opcoes or ()
    descricoes = pergunta.criterios if pergunta.criterios is not None else opcoes
    if pergunta.tipo == "bool":
        return {"type": "noul", **base}
    if pergunta.tipo == "escolha":
        return {"type": "choice", **base, "criteria": dict(zip(opcoes, descricoes, strict=True))}
    return {"type": "score", **base, "criteria": list(descricoes)}


def _reais(centavos: int) -> float:
    return centavos / 100


def estado_para_typesafe(estado: EstadoJev) -> dict:
    """O `state` do pedido: só os campos presentes entram."""
    resultado: dict[str, Any] = {
        "historico": [{"origem": origem, "texto": texto} for origem, texto in estado.historico],
        "regua": str(estado.regua),
        "divida_reais": _reais(estado.divida_centavos),
        "estado_conversa": str(estado.estado_conversa),
    }
    if estado.oferta_atual is not None:
        oferta = estado.oferta_atual
        resultado["oferta"] = {
            "degrau": oferta.degrau,
            "desconto_reais": _reais(oferta.desconto_centavos),
            "valor_final_reais": _reais(oferta.valor_final_centavos),
        }
    if estado.leitura:
        resultado["leitura_jev1"] = [
            {"pergunta": laudo.pergunta, "valor": laudo.valor, "probabilidade": laudo.probabilidade}
            for laudo in estado.leitura
        ]
    if estado.texto_candidato is not None:
        resultado["texto_candidato"] = estado.texto_candidato
    if estado.acoes_do_turno:
        resultado["acoes_do_turno"] = list(estado.acoes_do_turno)
    if estado.numeros:
        resultado["numeros"] = dict(estado.numeros)
    return resultado


def _invalida(pergunta: Pergunta, motivo: str) -> JevIndisponivel:
    return JevIndisponivel(f"resposta da TypeSafe inválida em {pergunta.nome}: {motivo}")


def _probabilidade(pergunta: Pergunta, valor: object, campo: str) -> float:
    """Um número de 0 a 1 (booleano, texto e nan não valem)."""
    try:
        valida = (
            not isinstance(valor, bool)
            and isinstance(valor, int | float)
            and not math.isnan(valor)
            and 0 <= valor <= 1
        )
    except OverflowError:
        # Um inteiro grande demais para virar float: o isnan estoura.
        valida = False
    if not valida:
        raise _invalida(pergunta, f"{campo} não é uma probabilidade de 0 a 1")
    return float(valor)  # type: ignore[arg-type]


def _nivel_do_score(pergunta: Pergunta, chave: object) -> str:
    """A chave de `probabilities` do score é o índice do nível (0, 1, …) ou o próprio critério."""
    opcoes = pergunta.opcoes or ()
    if isinstance(chave, str) and chave in opcoes:
        return chave
    try:
        indice = int(chave)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _invalida(pergunta, "nível desconhecido") from None
    if not 0 <= indice < len(opcoes):
        raise _invalida(pergunta, "nível desconhecido")
    return opcoes[indice]


def laudo_da_resposta(pergunta: Pergunta, resposta: Mapping) -> Laudo:
    """Do retorno de uma pergunta para o Laudo. A probabilidade é sempre a do valor escolhido (o
    jev1.py exige sim com p ≥ limiar). Levanta JevIndisponivel, sem o conteúdo da resposta."""
    if pergunta.tipo == "bool":
        p = _probabilidade(pergunta, resposta.get(CAMPO_NOUL), CAMPO_NOUL)
        valor = p >= 0.5
        escolhida = p if valor else 1 - p
        # A noul não traz confiança: vale a própria probabilidade.
        return Laudo(pergunta.nome, valor, escolhida, escolhida)

    probabilidades = resposta.get(CAMPO_PROBABILIDADES)
    if not isinstance(probabilidades, Mapping):
        raise _invalida(pergunta, f"falta {CAMPO_PROBABILIDADES}")
    confianca = _probabilidade(pergunta, resposta.get(CAMPO_CONFIANCA), CAMPO_CONFIANCA)
    if pergunta.tipo == "escolha":
        escolha = resposta.get(CAMPO_ESCOLHA)
        if not isinstance(escolha, str) or escolha not in (pergunta.opcoes or ()):
            raise _invalida(pergunta, f"{CAMPO_ESCOLHA} fora das opções")
        if escolha not in probabilidades:
            raise _invalida(pergunta, f"falta a probabilidade de {CAMPO_ESCOLHA}")
        p = _probabilidade(pergunta, probabilidades[escolha], CAMPO_PROBABILIDADES)
        return Laudo(pergunta.nome, escolha, p, confianca)

    # score: a opção de maior probabilidade; o score esperado (fracionário) não entra.
    por_opcao = {
        _nivel_do_score(pergunta, chave): _probabilidade(pergunta, p, CAMPO_PROBABILIDADES)
        for chave, p in probabilidades.items()
    }
    if not por_opcao:
        raise _invalida(pergunta, f"{CAMPO_PROBABILIDADES} vazio")
    melhor = max(por_opcao, key=por_opcao.__getitem__)
    return Laudo(pergunta.nome, melhor, por_opcao[melhor], confianca)


class JevTypeSafe:
    """Um POST por batch. O `timeout` é do chamador (75% do `jev_timeout_ms`) e vale como prazo
    TOTAL da chamada, conexão e leitura do corpo juntas: o `timeout` do httpx vale por fase, e um
    servidor que pinga um pedaço a cada instante o manteria vivo para sempre. Assim a chamada não
    fica viva depois do turno.

    O `ultimo_uso` é o total de tokens de entrada da última chamada (a saída é grátis); fica None se
    a chamada falhou antes da resposta ou a resposta não trouxe o uso.
    """

    def __init__(
        self,
        chave: str,
        *,
        url: str = URL_TYPESAFE,
        modelo: str = MODELO_PADRAO,
        timeout: float,
        transporte: httpx.BaseTransport | None = None,
    ):
        self.modelo = modelo
        self.origem = f"typesafe:{modelo}"
        self.ultimo_uso: int | None = None
        # A soma dos tokens de entrada de todas as chamadas, para o custo de uma simulação.
        self.uso_total = 0
        # O JevComPrazo chama `avaliar` de uma thread, e a chamada atrasada segue viva.
        self._trava = threading.Lock()
        self._prazo = timeout
        self._cliente = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {chave}"},
            timeout=timeout,
            transport=transporte,
        )

    @classmethod
    def da_config(
        cls, config: "ConfigJev", timeout_ms: int, transporte: httpx.BaseTransport | None = None
    ) -> "JevTypeSafe":
        """O timeout HTTP é 75% do `jev_timeout_ms` da política: a chamada não passa do turno."""
        if not config.jev_api_key:
            raise ValueError("o Jev real precisa da AGENTE_JEV_API_KEY")
        return cls(
            config.jev_api_key,
            url=config.jev_url,
            modelo=config.jev_modelo,
            timeout=0.75 * timeout_ms / 1000,
            transporte=transporte,
        )

    def __enter__(self) -> "JevTypeSafe":
        return self

    def __exit__(self, *_erro) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._cliente.close()

    def avaliar(self, perguntas: Sequence[Pergunta], estado: EstadoJev) -> list[Laudo]:
        with self._trava:
            self.ultimo_uso = None
        # Serializa antes de sair: o erro daqui é do pedido (nada foi enviado), não da resposta.
        try:
            corpo = {
                "model": self.modelo,
                "state": estado_para_typesafe(estado),
                "questions": {p.nome: pergunta_para_typesafe(p) for p in perguntas},
            }
            conteudo = json.dumps(corpo, allow_nan=False, ensure_ascii=False).encode()
        except (TypeError, ValueError, OverflowError):
            raise JevIndisponivel("não foi possível montar o pedido ao Jev") from None
        dados = self._chamar(conteudo)
        if not isinstance(dados, Mapping):
            raise JevIndisponivel("a resposta da TypeSafe não é um objeto JSON")
        uso = self._uso(dados)
        with self._trava:
            self.ultimo_uso = uso
            self.uso_total += uso or 0
        respostas = dados.get(CAMPO_RESPOSTAS)
        if not isinstance(respostas, Mapping):
            raise JevIndisponivel(f"a resposta da TypeSafe não traz {CAMPO_RESPOSTAS}")
        laudos = []
        for pergunta in perguntas:
            resposta_da_pergunta = respostas.get(pergunta.nome)
            if not isinstance(resposta_da_pergunta, Mapping):
                raise _invalida(pergunta, "sem resposta")
            laudos.append(laudo_da_resposta(pergunta, resposta_da_pergunta))
        return laudos

    def _chamar(self, conteudo: bytes) -> object:
        """O POST com o prazo total: o corpo é lido por pedaços, conferindo o relógio a cada um."""
        limite = time.monotonic() + self._prazo
        # `from None` em tudo: o corpo do erro e a mensagem do httpx podem ecoar o pedido.
        try:
            with self._cliente.stream(
                "POST", CAMINHO, content=conteudo, headers={"Content-Type": "application/json"}
            ) as resposta:
                # O status vem antes do corpo: o erro nunca lê (nem guarda) o que o servidor ecoou.
                if not resposta.is_success:
                    raise JevIndisponivel(f"a TypeSafe respondeu {resposta.status_code}") from None
                pedacos = []
                for pedaco in resposta.iter_bytes():
                    pedacos.append(pedaco)
                    if time.monotonic() > limite:
                        raise JevIndisponivel("a TypeSafe passou do prazo") from None
                if time.monotonic() > limite:
                    raise JevIndisponivel("a TypeSafe passou do prazo") from None
            return json.loads(b"".join(pedacos))
        except httpx.TimeoutException:
            raise JevIndisponivel("a TypeSafe passou do prazo") from None
        except httpx.HTTPError as erro:
            raise JevIndisponivel(f"falha de rede com a TypeSafe ({type(erro).__name__})") from None
        except ValueError:
            raise JevIndisponivel("a resposta da TypeSafe não é JSON") from None

    @staticmethod
    def _uso(dados: Mapping) -> int | None:
        uso = dados.get(CAMPO_USO)
        tokens = uso.get(CAMPO_TOKENS_DE_ENTRADA) if isinstance(uso, Mapping) else None
        return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
