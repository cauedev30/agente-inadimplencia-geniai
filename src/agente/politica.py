"""Carrega e valida o politica.toml: todos os parâmetros de negócio do agente."""

import tomllib
from datetime import date, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agente.dominio.telefone import normalizar_telefone
from agente.tipos import FormaHistorico


class Regua(StrEnum):
    NR = "NR"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    PREVIA = "PREVIA"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"


DiaSemana = Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]


class _Imutavel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _em_e164(telefones: frozenset[str]) -> frozenset[str]:
    """Os telefones em E.164, como a carteira e o webhook os guardam; o inválido recusa."""
    normalizados = {normalizar_telefone(telefone) for telefone in telefones}
    if None in normalizados:
        raise ValueError("telefone inválido: use o DDD e o número, com ou sem +55")
    return frozenset(normalizados)


class ReguaParametros(_Imutavel):
    """O valor final da régua (D64): o que o cliente paga depois da oferta. None = sem desconto."""

    valor_final_centavos: int | None = Field(default=None, gt=0)


class Horario(_Imutavel):
    inicio: time
    fim: time

    @model_validator(mode="before")
    @classmethod
    def _de_lista(cls, valor):
        if isinstance(valor, list | tuple) and len(valor) == 2:
            return {"inicio": valor[0], "fim": valor[1]}
        return valor

    @model_validator(mode="after")
    def _inicio_antes_do_fim(self):
        if self.inicio >= self.fim:
            raise ValueError("o início tem de ser antes do fim")
        return self


class Janela(_Imutavel):
    fuso: str
    dias: dict[DiaSemana, Horario]
    feriados: frozenset[date] = frozenset()

    @field_validator("fuso")
    @classmethod
    def _fuso_existe(cls, valor: str) -> str:
        try:
            ZoneInfo(valor)
        except (ZoneInfoNotFoundError, ValueError) as erro:
            raise ValueError(f"fuso desconhecido: {valor}") from erro
        return valor

    @property
    def zona(self) -> ZoneInfo:
        return ZoneInfo(self.fuso)


class Propensao(_Imutavel):
    n_meses: int = Field(ge=1)
    tolerancia_dias: int = Field(ge=0)


class ColunasCarteira(_Imutavel):
    """O nome, no CSV, de cada coluna que o agente lê. A coluna da régua é opcional."""

    matricula: str = "matricula"
    nome: str = "nome"
    telefone: str = "telefone"
    mensalidades_abertas: str = "mensalidades_abertas"
    regua: str = "regua"


class Carteira(_Imutavel):
    separador: str = Field(default=";", min_length=1, max_length=1)
    colunas: ColunasCarteira = ColunasCarteira()


class LiberacaoPiloto(_Imutavel):
    """Só os telefones da lista conversam com o agente: o piloto (D61). Lista vazia: ninguém."""

    modo: Literal["piloto"] = "piloto"
    telefones: frozenset[str] = frozenset()

    _normalizar = field_validator("telefones")(_em_e164)


class HistoricoDoCaso(_Imutavel):
    """Um pagamento do histórico do caso: `meses_atras` meses antes do 1º mês em aberto."""

    meses_atras: int = Field(ge=1)
    dia: int = Field(ge=1, le=28)
    forma: FormaHistorico


class CasoTeste(_Imutavel):
    """Um caso do cardápio do modo teste (D56): uma pessoa inventada com a dívida e o histórico."""

    numero: int = Field(ge=1)
    titulo: str = Field(min_length=1)
    nome: str = Field(min_length=1)
    meses_atraso: int = Field(ge=1, le=6)
    historico: tuple[HistoricoDoCaso, ...] = ()


# A campanha da importação diária do CTN (o padrão do `agente importar-ctn`).
CAMPANHA_DO_CTN = "ctn"


class ModoTeste(_Imutavel):
    """O modo teste (D56): os testadores recebem casos inventados no lugar da carteira.

    `casos` vem do arquivo do `cardapio`, que o `carregar_politica` lê; não se escreve no `[teste]`.
    """

    cardapio: Path
    campanha: str = Field(default="teste", min_length=1)
    caso_padrao: int = Field(default=1, ge=1)
    texto_abertura: str = Field(min_length=1)
    casos: tuple[CasoTeste, ...] = ()

    @field_validator("campanha")
    @classmethod
    def _nao_e_a_do_ctn(cls, valor: str) -> str:
        if valor == CAMPANHA_DO_CTN:
            raise ValueError(
                f"campanha: '{CAMPANHA_DO_CTN}' é a da importação diária da carteira real; o "
                "teste misturaria as duas"
            )
        return valor

    @field_validator("texto_abertura")
    @classmethod
    def _traz_nome_e_valor(cls, valor: str) -> str:
        faltando = [marcador for marcador in ("{nome}", "{valor}") if marcador not in valor]
        if faltando:
            raise ValueError(f"texto_abertura: falta o marcador {' e '.join(faltando)}")
        return valor

    @model_validator(mode="after")
    def _cardapio_coerente(self):
        numeros = sorted(caso.numero for caso in self.casos)
        if numeros != list(range(1, len(numeros) + 1)):
            raise ValueError(
                "casos: os números têm de ser únicos e seguir de 1 em 1, a partir do 1"
            )
        if self.caso_padrao not in numeros:
            raise ValueError(f"caso_padrao: o caso {self.caso_padrao} não está no cardápio")
        return self


class Politica(_Imutavel):
    mensalidade_centavos: int = Field(gt=0)
    teto_resolucao: int = Field(ge=1)
    regua_acima_de_6: Regua
    pix_validade_horas: int = Field(ge=1)
    # O Pix pendente que vence em menos que isto não é reaproveitado no pedido seguinte do cliente.
    pix_reaproveitar_minimo_minutos: int = Field(ge=0)
    # O handoff expira o Pix pendente? Padrão não: o Pix continua valendo e o pagamento conta.
    handoff_expira_pix: bool = False
    rajada_segundos: int = Field(ge=0)
    audio_max_segundos: int = Field(ge=1)
    # O download do áudio do cliente (D43): o maior arquivo, o prazo de tudo e, para o formato cuja
    # duração não se lê (qualquer um fora do Ogg Opus), o maior arquivo que vale pelos 120 s.
    audio_max_bytes: int = Field(ge=1)
    audio_prazo_segundos: int = Field(ge=1)
    audio_sem_duracao_max_bytes: int = Field(ge=1)
    # A chamada de transcrição: o prazo e o tamanho do texto guardado (o resto é cortado).
    transcricao_prazo_segundos: int = Field(ge=1)
    transcricao_max_caracteres: int = Field(ge=2)
    # Os pedidos de texto, quando a rajada fica sem nada legível (só áudio que não foi lido; D43).
    pedir_texto_audio_longo: str = Field(min_length=1)
    pedir_texto_audio_falhou: str = Field(min_length=1)
    integracao_tentativas: int = Field(ge=1)
    erro_interno_tentativas: int = Field(ge=1)
    integracao_espera_segundos: int = Field(ge=1)
    integracao_espera_maxima_segundos: int = Field(ge=1)
    # O texto da fixa da saída por DESCONFIANCA (D44), que fica no WhatsApp.
    texto_humano_no_whatsapp: str = Field(min_length=1)
    # O recebedor do Pix (D44): vazio desliga a frase; o texto traz `{recebedor}`.
    pix_recebedor: str
    texto_pix_recebedor: str = Field(min_length=1)
    template_abertura: str = Field(min_length=1)
    template_confirmacao: str = Field(min_length=1)
    # O aviso único quando o cliente quitou por fora e a baixa chegou pelo CTN (spec do CTN real,
    # decisão 3): texto fixo, sem valor nem nome, só dentro das 24h e fora do teto de mensagens.
    texto_baixa_externa: str = Field(min_length=1)
    # true: o agente registra no CTN a baixa do pagamento pelo link (o link não baixa, T9.6).
    # false: o link já baixa no CTN e o agente não dá a segunda baixa. A do Pix é sempre do agente.
    agente_da_baixa_no_link: bool
    anexos_aceitos: frozenset[str]
    comando_reinicio: str = Field(min_length=1)
    # O que o reinício responde (sai fora do teto e fica na conversa antiga).
    texto_reinicio: str = Field(default="Conversa reiniciada.", min_length=1)
    telefones_equipe: frozenset[str] = frozenset()
    reguas: dict[Regua, ReguaParametros]
    janela: Janela
    propensao: Propensao
    carteira: Carteira = Carteira()
    # Quem conversa com o agente (D61): só os telefones do piloto; o resto é ignorado.
    liberacao: LiberacaoPiloto = LiberacaoPiloto()
    # Ligado, os testadores do piloto recebem os casos do cardápio e não a carteira (D56).
    teste: ModoTeste | None = None

    _normalizar_equipe = field_validator("telefones_equipe")(_em_e164)

    @field_validator("texto_pix_recebedor")
    @classmethod
    def _traz_o_recebedor(cls, valor: str) -> str:
        if "{recebedor}" not in valor:
            raise ValueError("texto_pix_recebedor: falta o marcador {recebedor}")
        return valor

    @model_validator(mode="after")
    def _espera_maxima_nao_menor_que_a_inicial(self):
        if self.integracao_espera_maxima_segundos < self.integracao_espera_segundos:
            raise ValueError("integracao_espera_maxima_segundos: menor que a espera inicial")
        return self

    @model_validator(mode="after")
    def _teste_so_no_piloto(self):
        if self.teste is None:
            return self
        # A matrícula simulada leva os 4 últimos dígitos do telefone (`SIM-<n>-<dddd>`).
        finais = [telefone[-4:] for telefone in self.liberacao.telefones]
        if len(set(finais)) != len(finais):
            raise ValueError(
                "teste: os telefones do piloto têm de ter os 4 últimos dígitos distintos "
                "(são a matrícula simulada)"
            )
        return self

    def espera_integracao(self, falhas: int) -> timedelta:
        """A espera depois da `falhas`-ésima falha seguida: dobra a cada uma, até o teto."""
        segundos = self.integracao_espera_segundos * 2 ** max(falhas - 1, 0)
        return timedelta(seconds=min(segundos, self.integracao_espera_maxima_segundos))

    @field_validator("reguas")
    @classmethod
    def _todas_as_reguas(cls, valor: dict[Regua, ReguaParametros]):
        faltando = [r.value for r in Regua if r not in valor]
        if faltando:
            raise ValueError(f"faltam as réguas: {', '.join(faltando)}")
        return valor


class PoliticaInvalida(ValueError):
    """O politica.toml tem um campo errado; a mensagem diz qual."""


def _erros(arquivo: Path, erro: ValidationError, prefixo: str = "") -> list[str]:
    return [
        f"{arquivo}: {prefixo}{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}"
        for e in erro.errors()
    ]


def _ler_cardapio(politica: Path, teste: dict) -> dict:
    """O `[teste]` com o `cardapio` resolvido e os `casos` dele; o erro diz o arquivo e o campo."""
    if "casos" in teste:
        raise PoliticaInvalida(f"{politica}: teste.casos: os casos vêm do arquivo do cardapio")
    if not isinstance(teste.get("cardapio"), str):
        return teste  # a validação da política diz que o campo falta ou está errado
    arquivo = politica.parent / teste["cardapio"]
    try:
        with open(arquivo, "rb") as aberto:
            lido = tomllib.load(aberto)
    except OSError as erro:
        raise PoliticaInvalida(
            f"{politica}: teste.cardapio: não abriu {arquivo} ({erro})"
        ) from erro
    except tomllib.TOMLDecodeError as erro:
        raise PoliticaInvalida(f"{arquivo}: {erro}") from erro
    sobra = sorted(set(lido) - {"caso"})
    if sobra:
        raise PoliticaInvalida(f"{arquivo}: só a tabela [[caso]] é aceita, veio {', '.join(sobra)}")
    casos, erros = [], []
    for posicao, bruto in enumerate(lido.get("caso", [])):
        try:
            casos.append(CasoTeste.model_validate(bruto))
        except ValidationError as erro:
            erros += _erros(arquivo, erro, f"caso[{posicao}].")
    if erros:
        raise PoliticaInvalida("; ".join(erros))
    return {**teste, "cardapio": arquivo, "casos": tuple(casos)}


def carregar_politica(caminho: Path) -> Politica:
    with open(caminho, "rb") as arquivo:
        dados = tomllib.load(arquivo)
    if isinstance(dados.get("teste"), dict):
        dados["teste"] = _ler_cardapio(caminho, dados["teste"])
    try:
        return Politica.model_validate(dados)
    except ValidationError as erro:
        raise PoliticaInvalida("; ".join(_erros(caminho, erro))) from erro
