"""A folha de revisão da pré-sombra (tarefa 10.4): um bloco por turno, com os dados do cliente
mascarados. Dados inventados; a amostra real só entra no último teste, que confere propriedades.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agente.config import Config
from agente.db.sessao import criar_sessao
from agente.integracoes.jev import JevFalso, responder
from agente.reproducao.chatwoot_leitura import ChatwootLeitura
from agente.reproducao.folha import (
    AVISO_CARTEIRA_ESTIMADA,
    escrever_folha,
    mascarar_nome,
    mascarar_numeros,
)
from agente.reproducao.reproduzir import RelatorioReproducao, reproduzir

from .amostra import ler_amostra
from .fabrica import CONTA, INBOX, SEGUNDA_10H, TOKEN, ChatwootSimulado, conversa, mensagem

CARTEIRA = Path(__file__).parent / "fixtures" / "carteira.csv"
DESDE = datetime.fromtimestamp(SEGUNDA_10H - 3 * 86400, UTC)
T = SEGUNDA_10H


@pytest.fixture
def config():
    return Config(database_url="sqlite://", chatwoot_conta=CONTA, chatwoot_inbox=INBOX)


def _rodar(servidor, tmp_path, politica, config, jev=None) -> tuple[RelatorioReproducao, Path]:
    banco = tmp_path / "reproducao.db"
    with ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, TOKEN, transporte=servidor.transporte
    ) as leitura:
        relatorio = reproduzir(leitura, CARTEIRA, DESDE, banco, politica, config, jev)
    return relatorio, banco


def _folha(banco, caminho, **opcoes) -> str:
    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        escrever_folha(sessao, caminho, **opcoes)
    return caminho.read_text(encoding="utf-8")


def _servidor_com_muitos_turnos(quantos: int) -> ChatwootSimulado:
    # Uma mensagem a cada 10 minutos, dentro da janela: cada uma fecha o seu turno.
    mensagens = [mensagem(8000 + i, 950, 0, f"pergunta {i}", T + 600 * i) for i in range(quantos)]
    return ChatwootSimulado([conversa(950, T + 600 * quantos)], {950: mensagens})


def _turnos_da_folha(texto: str) -> list[int]:
    return [int(numero) for numero in re.findall(r"^## Turno (\d+)", texto, flags=re.MULTILINE)]


def test_um_bloco_por_turno_com_cliente_decisao_e_resposta_do_atendente(tmp_path, politica, config):
    mensagens = [
        mensagem(8101, 951, 0, "quanto eu devo?", T),
        mensagem(8102, 951, 0, "e o valor total?", T + 2),
        mensagem(8103, 951, 1, "Já te passo o valor", T + 300),
    ]
    servidor = ChatwootSimulado([conversa(951, T + 300)], {951: mensagens})
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")

    assert _turnos_da_folha(texto) == [1]
    # As duas mensagens da rajada, na ordem, como citação.
    assert "> quanto eu devo?\n> e o valor total?" in texto
    assert "INFORM_DEBT" in texto
    # Os números previstos em sombra: a dívida da carteira (3 mensalidades) em reais.
    assert "divida_centavos" in texto
    assert "R$ " in texto
    assert "> Já te passo o valor" in texto
    assert "Veredito: ok | errado | perigoso" in texto
    assert "Nota:" in texto


def test_o_gatilho_aparece_no_lugar_da_acao(tmp_path, politica, config):
    jev = JevFalso([responder("quer_retencao", True, 0.92)])
    servidor = ChatwootSimulado(
        [conversa(952, T + 60)], {952: [mensagem(8201, 952, 0, "quero cancelar", T)]}
    )
    _, banco = _rodar(servidor, tmp_path, politica, config, jev)

    texto = _folha(banco, tmp_path / "folha.md")

    assert "Gatilho: RETENCAO" in texto
    assert "Ação:" not in texto


def test_sem_resposta_do_atendente_o_bloco_diz_que_nao_houve(tmp_path, politica, config):
    servidor = ChatwootSimulado([conversa(953, T + 60)], {953: [mensagem(8301, 953, 0, "oi", T)]})
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")

    assert "Atendente: sem resposta" in texto


def test_a_resposta_seguinte_e_a_primeira_do_atendente_depois_do_turno(tmp_path, politica, config):
    mensagens = [
        mensagem(8401, 954, 0, "primeira", T),
        mensagem(8402, 954, 1, "resposta um", T + 300),
        mensagem(8403, 954, 1, "resposta dois", T + 360),
        mensagem(8404, 954, 0, "segunda", T + 900),
    ]
    servidor = ChatwootSimulado([conversa(954, T + 900)], {954: mensagens})
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")
    primeiro, segundo = re.split(r"^## Turno ", texto, flags=re.MULTILINE)[1:]

    assert "resposta um" in primeiro
    assert "resposta dois" not in primeiro
    # Depois da segunda mensagem do cliente não há mais atendente.
    assert "resposta um" not in segundo
    assert "resposta dois" not in segundo
    assert "sem resposta" in segundo.lower()


def test_mascara_telefone_e_cpf_do_cliente_e_do_atendente(tmp_path, politica, config):
    mensagens = [
        mensagem(
            8501,
            955,
            0,
            "meu cpf é 123.456.789-09, o zap +5583999990001 ou 83988887777 e 12345678909",
            T,
        ),
        mensagem(8502, 955, 1, "anotei, ligo para +5583977776666 e confiro 987.654.321-00", T + 60),
    ]
    servidor = ChatwootSimulado([conversa(955, T + 60)], {955: mensagens})
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")

    for cru in ("123.456.789-09", "987.654.321-00", "+5583999990001", "+5583977776666"):
        assert cru not in texto
    assert not re.search(r"\b\d{11}\b", texto)
    assert "***.***.***-**" in texto
    # O telefone mantém o +55 e os 4 últimos dígitos, como no log.
    assert "+55*******0001" in texto


# Telefone e CPF escritos à mão, como o cliente digita (dados inventados).
ESCRITOS_A_MAO = [
    "(83) 98765-4321",
    "83 98765-4321",
    "8398765432",
    "5583987654321",
    "+55 83 98765-4321",
    "123456789-01",
    "123 456 789 01",
    "123.456.789.01",
    "123.456.789-01",
    "+55 (83) 98765 4321",
]


@pytest.mark.parametrize("escrito", ESCRITOS_A_MAO)
def test_mascara_telefone_e_cpf_escritos_a_mao(escrito):
    limpo = mascarar_numeros(f"meu número é {escrito}, pode ligar")

    # Sem dado: só a contagem de dígitos que sobrou.
    sobra = sum(c.isdigit() for c in limpo)
    assert sobra == 0, "sobrou dígito de telefone ou CPF escrito à mão"
    assert limpo.startswith("meu número é ") and limpo.endswith(", pode ligar")


@pytest.mark.parametrize(
    "texto",
    [
        "paguei R$ 1.234,56 ontem",
        "o valor é R$ 100,20",
        "dia 10/11/2026 eu pago",
        "dia 10-11-2026 eu pago",
        "dia 10.11.2026 eu pago",
        "devo 2 mensalidades",
        "são 3 mensalidades de R$ 33,40",
        "pago até as 18h30 do dia 5",
    ],
)
def test_valores_em_reais_datas_e_contagens_sobrevivem(texto):
    assert mascarar_numeros(texto) == texto


def test_a_folha_mascara_os_formatos_escritos_a_mao(tmp_path, politica, config):
    mensagens = [
        mensagem(8901, 960, 0, "meu zap é (83) 98765-4321 ou 83 98765-4321, cpf 123 456 789 01", T),
        mensagem(8902, 960, 1, "anotei 8398765432 e 123.456.789.01; são R$ 1.234,56", T + 60),
    ]
    servidor = ChatwootSimulado([conversa(960, T + 60)], {960: mensagens})
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")

    corpo = "\n".join(linha for linha in texto.splitlines() if linha.startswith("> "))
    assert not re.search(r"\d{8}", corpo.replace(" ", "")), "sobrou número longo na folha"
    assert "R$ 1.234,56" in corpo


def test_a_folha_mascara_o_nome_do_cliente_da_carteira(tmp_path, politica, config):
    # O contato do Chatwoot tem outro nome; o da carteira ("Cliente Inventado Um") também sai.
    bruta = conversa(961, T + 60)
    bruta["meta"]["sender"]["name"] = "Apelido"
    mensagens = [
        mensagem(8903, 961, 0, "oi, sou o Inventado e a Apelido", T),
        mensagem(8904, 961, 1, "Bom dia, Inventado", T + 60),
    ]
    _, banco = _rodar(ChatwootSimulado([bruta], {961: mensagens}), tmp_path, politica, config)
    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        escrever_folha(sessao, tmp_path / "folha.md", nomes={961: "Apelido"})
    texto = (tmp_path / "folha.md").read_text(encoding="utf-8")

    assert "> oi, sou o [nome] e a [nome]" in texto
    assert "> Bom dia, [nome]" in texto
    assert "inventado" not in texto.lower()
    assert "apelido" not in texto.lower()


def test_mascara_o_nome_do_contato_nas_duas_mensagens(tmp_path, politica, config):
    mensagens = [
        mensagem(8601, 956, 0, "Oi, aqui é o FULANO, quanto eu devo?", T),
        mensagem(8602, 956, 1, "Bom dia, inventado! Vou olhar", T + 60),
    ]
    servidor = ChatwootSimulado([conversa(956, T + 60)], {956: mensagens})
    relatorio, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md", nomes=relatorio.nomes_dos_contatos)

    assert "> Oi, aqui é o [nome], quanto eu devo?" in texto
    assert "> Bom dia, [nome]! Vou olhar" in texto
    assert "fulano" not in texto.lower()
    assert "inventado" not in texto.lower()


def test_o_nome_do_contato_nao_vai_ao_relatorio_impresso(tmp_path, politica, config):
    servidor = ChatwootSimulado([conversa(957, T + 60)], {957: [mensagem(8701, 957, 0, "oi", T)]})

    relatorio, _ = _rodar(servidor, tmp_path, politica, config)

    assert relatorio.nomes_dos_contatos == {957: "Fulano Inventado"}
    assert "Fulano" not in repr(relatorio)


def test_sem_nome_disponivel_a_folha_segue_sem_a_troca(tmp_path, politica, config):
    servidor = ChatwootSimulado(
        [conversa(958, T + 60)], {958: [mensagem(8801, 958, 0, "sou o Fulano", T)]}
    )
    _, banco = _rodar(servidor, tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md")

    assert "> sou o Fulano" in texto
    assert "[nome]" not in texto


def test_mascarar_nome_ignora_maiuscula_acento_e_particulas():
    nome = "José da Álvaro"

    assert mascarar_nome("josé, JOSE e Jose; ÁLVARO e alvaro", nome) == (
        "[nome], [nome] e [nome]; [nome] e [nome]"
    )
    # A partícula "da" e as palavras que só contêm o nome dentro não são trocadas.
    assert mascarar_nome("da casa do josemar e do alvaros", nome) == (
        "da casa do josemar e do alvaros"
    )
    assert mascarar_nome("sem nome nenhum", None) == "sem nome nenhum"
    assert mascarar_nome("texto", "") == "texto"


def test_a_amostra_respeita_a_semente(tmp_path, politica, config):
    _, banco = _rodar(_servidor_com_muitos_turnos(10), tmp_path, politica, config)

    a = _turnos_da_folha(_folha(banco, tmp_path / "a.md", amostra=4, semente=1))
    b = _turnos_da_folha(_folha(banco, tmp_path / "b.md", amostra=4, semente=1))
    c = _turnos_da_folha(_folha(banco, tmp_path / "c.md", amostra=4, semente=2))

    assert len(a) == 4
    assert a == b
    assert a == sorted(a)
    assert a != c


def test_a_semente_padrao_e_fixa(tmp_path, politica, config):
    _, banco = _rodar(_servidor_com_muitos_turnos(10), tmp_path, politica, config)

    a = _turnos_da_folha(_folha(banco, tmp_path / "a.md", amostra=4))
    b = _turnos_da_folha(_folha(banco, tmp_path / "b.md", amostra=4))

    assert a == b


def test_amostra_maior_que_o_total_leva_todos_os_turnos(tmp_path, politica, config):
    _, banco = _rodar(_servidor_com_muitos_turnos(3), tmp_path, politica, config)

    texto = _folha(banco, tmp_path / "folha.md", amostra=50)

    assert _turnos_da_folha(texto) == [1, 2, 3]
    assert "3 de 3" in texto


def test_nao_sobrescreve_uma_folha_que_ja_existe(tmp_path, politica, config):
    _, banco = _rodar(_servidor_com_muitos_turnos(2), tmp_path, politica, config)
    caminho = tmp_path / "folha.md"
    caminho.write_text("os vereditos do Cauê", encoding="utf-8")

    with criar_sessao(f"sqlite:///{banco.as_posix()}")() as sessao:
        with pytest.raises(FileExistsError):
            escrever_folha(sessao, caminho)

    assert caminho.read_text(encoding="utf-8") == "os vereditos do Cauê"


def test_a_folha_da_amostra_real_nao_leva_telefone_cpf_nem_nome(tmp_path, politica):
    """Só propriedades: nada do que a amostra tem de pessoal pode sobrar na folha."""
    amostra = ler_amostra()
    inbox = amostra[0]["conversa"]["inbox_id"]
    amostra = [arquivo for arquivo in amostra if arquivo["conversa"]["inbox_id"] == inbox]
    conversas = [arquivo["conversa"] for arquivo in amostra]
    mensagens = {arquivo["conversa"]["id"]: arquivo["mensagens"] for arquivo in amostra}
    telefones = {c["meta"]["sender"]["phone_number"] for c in conversas}
    carteira = tmp_path / "carteira.csv"
    linhas = [f"R-{n:04d};Cliente {n};{t};3;" for n, t in enumerate(sorted(telefones))]
    cabecalho = "matricula;nome;telefone;mensalidades_abertas;regua"
    carteira.write_text("\n".join([cabecalho, *linhas]) + "\n", encoding="utf-8")
    config = Config(database_url="sqlite://", chatwoot_inbox=inbox)
    servidor = ChatwootSimulado(conversas, mensagens, pagina=20)
    banco = tmp_path / "reproducao.db"
    with ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, TOKEN, transporte=servidor.transporte
    ) as leitura:
        relatorio = reproduzir(
            leitura, carteira, datetime(2000, 1, 1, tzinfo=UTC), banco, politica, config
        )

    texto = _folha(banco, tmp_path / "folha.md", nomes=relatorio.nomes_dos_contatos)

    # As asserções afirmam um booleano ou uma contagem com mensagem fixa: o pytest imprime os
    # valores comparados quando uma falha, e aqui eles são texto e telefone de cliente.
    tem_turnos = bool(_turnos_da_folha(texto))
    assert tem_turnos, "a folha da amostra saiu sem turnos"
    telefones_na_folha = sum(
        (telefone in texto) + (telefone.removeprefix("+") in texto) for telefone in telefones
    )
    assert telefones_na_folha == 0, "sobrou telefone da amostra na folha"
    onze_digitos = len(re.findall(r"\b\d{11}\b", texto))
    assert onze_digitos == 0, "sobrou sequência de 11 dígitos na folha"
    cpfs = len(re.findall(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", texto))
    assert cpfs == 0, "sobrou CPF formatado na folha"
    # Os formatos escritos à mão também: 8 dígitos ou mais, com separadores entre eles, no que o
    # cliente e o atendente escreveram (as linhas de citação).
    citacoes = "\n".join(linha for linha in texto.splitlines() if linha.startswith("> "))
    longos = len(re.findall(r"(?<!\d)\d(?:[ .()\-]{0,2}\d){7,}(?!\d)", citacoes))
    assert longos == 0, "sobrou número longo na folha"
    # Por conversa: o atendente de uma conversa pode ter, por acaso, o nome do contato de outra.
    # As palavras fixas da folha não contam (um contato poderia se chamar "Nota"). As asserções
    # contam em vez de comparar texto, para que uma falha não imprima dado de cliente.
    fixas = {"turno", "conversa", "cliente", "nota", "atendente", "decisão", "ação", "dos", "das"}
    nome_da_conversa = {c["id"]: c["meta"]["sender"].get("name") or "" for c in conversas}
    vazamentos = 0
    for bloco in re.split(r"^## Turno ", texto, flags=re.MULTILINE)[1:]:
        id_da_conversa = int(re.match(r"\d+ \(conversa (\d+)\)", bloco).group(1))
        for palavra in re.findall(r"\w+", nome_da_conversa[id_da_conversa].lower()):
            if len(palavra) >= 3 and palavra not in fixas:
                vazamentos += bool(re.search(rf"(?<!\w){re.escape(palavra)}(?!\w)", bloco.lower()))
    assert vazamentos == 0, "sobrou nome de contato na folha"


def test_o_aviso_da_carteira_estimada_fica_no_alto_da_folha(tmp_path, politica, config):
    servidor = _servidor_com_muitos_turnos(2)
    _, banco = _rodar(servidor, tmp_path, politica, config)

    sem = _folha(banco, tmp_path / "sem.md")
    com = _folha(banco, tmp_path / "com.md", aviso=AVISO_CARTEIRA_ESTIMADA)

    assert AVISO_CARTEIRA_ESTIMADA not in sem
    assert "mensalidades estimadas a partir das conversas, não do sistema do CDT" in com
    # Antes do primeiro turno, para o Cauê ler antes de dar o veredito.
    assert com.index(AVISO_CARTEIRA_ESTIMADA) < com.index("## Turno")
