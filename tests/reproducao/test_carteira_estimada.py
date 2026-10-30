"""A carteira estimada das conversas (tarefa 10.6): dados inventados, com os textos-modelo.

Os modelos de mensagem (débito, chave Pix, lembrete) são da operação, não de cliente, e podem
aparecer aqui. A amostra real só entra no último teste, que confere contagens.
"""

import csv
import json
from datetime import UTC, datetime

import pytest

from agente.carteira.importar import importar_carteira
from agente.integracoes.ctn import CTNFalso
from agente.reproducao.carteira_estimada import (
    conversas_da_pasta,
    conversas_do_chatwoot,
    estimar_carteira,
    formatar_resultado_estimativa,
    gravar_csv,
)
from agente.reproducao.chatwoot_leitura import ChatwootLeitura, conversa_lida

from .amostra import ler_amostra
from .fabrica import CONTA, INBOX, SEGUNDA_10H, TOKEN, ChatwootSimulado, conversa, mensagem

T = SEGUNDA_10H
LEMBRETE = (
    "***LEMBRETE***: Ao não realizar o pagamento pode acarretar o registro no SPC/Serasa. "
    "Digite **EU QUERO** para regularização"
)


def _debito(valor: str, com_desconto: str = "85,00") -> str:
    return (
        f"Olá, seu débito é de R$ {valor}. Com esse mega desconto pagando agora fica por "
        f"R$ {com_desconto} 😊"
    )


def _pix(valor: str, referente: str = "07 e 08") -> str:
    return (
        "Aqui está a chave Pix para o pagamento! 😊 📱 Chave Pix (CNPJ): 12345678000199 "
        f"💵 Valor: R$ {valor} 📍 Referente: {referente}"
    )


def _lida(
    id: int, textos: list[str], *, telefone: str | None = "+5583999990001", atividade: int = T
):
    """A conversa inventada e as mensagens da empresa (tipo 1), uma por texto, em ordem."""
    lida = conversa_lida(conversa(id, atividade, telefone=telefone))
    mensagens = [mensagem(id * 100 + i, id, 1, texto, T + i) for i, texto in enumerate(textos)]
    return lida, mensagens


def _estimar(politica, *conversas):
    return estimar_carteira(list(conversas), politica)


def _mensalidades(resultado) -> list[int]:
    return [linha.mensalidades for linha in resultado.linhas]


@pytest.mark.parametrize(
    ("valor", "mensalidades"),
    [("33,40", 1), ("66,80", 2), ("100,20.", 3), ("133,60", 4)],
)
def test_o_debito_vira_o_numero_de_mensalidades(politica, valor, mensalidades):
    resultado = _estimar(politica, _lida(1, [_debito(valor)]))

    assert _mensalidades(resultado) == [mensalidades]
    assert resultado.com_valor == {"debito": 1}
    assert resultado.mensalidades == {mensalidades: 1}


def test_vale_a_divida_cheia_e_nao_o_valor_com_desconto(politica):
    # 85,00 não é múltiplo de nada: se o desconto fosse lido, a conversa cairia em "sem valor".
    resultado = _estimar(politica, _lida(1, [_debito("100,20", com_desconto="33,40")]))

    assert _mensalidades(resultado) == [3]


def test_o_texto_varia_em_maiuscula_espaco_acento_e_emoji(politica):
    texto = "OLA 👋  SEU   DEBITO E DE  R$ 66,80 .  pagando AGORA..."

    resultado = _estimar(politica, _lida(1, [texto]))

    assert _mensalidades(resultado) == [2]


def test_o_resto_de_ate_cinco_centavos_vale_e_acima_disso_nao(politica):
    # 100,25 está a 5 centavos de 3 mensalidades; 100,26 está a 6.
    dentro = _estimar(politica, _lida(1, [_debito("100,25")]))
    fora = _estimar(politica, _lida(2, [_debito("100,26")]))

    assert _mensalidades(dentro) == [3]
    assert fora.linhas == []
    assert fora.sem_valor == 1


def test_o_debito_que_nao_bate_com_a_mensalidade_fica_sem_valor(politica):
    resultado = _estimar(politica, _lida(1, [_debito("233,40")]))

    assert resultado.linhas == []
    assert (resultado.de_cobranca, resultado.sem_valor) == (1, 1)


def test_vale_o_ultimo_debito_da_conversa(politica):
    resultado = _estimar(politica, _lida(1, [_debito("33,40"), "ok", _debito("100,20")]))

    assert _mensalidades(resultado) == [3]


def test_sem_debito_vale_a_ultima_chave_pix_quando_e_multiplo_exato(politica):
    resultado = _estimar(politica, _lida(1, [_pix("33,40"), _pix("66,80")]))

    assert _mensalidades(resultado) == [2]
    assert resultado.com_valor == {"pix": 1}


def test_o_debito_que_nao_bate_cai_na_chave_pix(politica):
    resultado = _estimar(politica, _lida(1, [_pix("66,80"), _debito("233,40")]))

    assert _mensalidades(resultado) == [2]
    assert resultado.com_valor == {"pix": 1}


@pytest.mark.parametrize("valor", ["6680", "666,80", "85,00", "60,00", "100,25"])
def test_a_chave_pix_com_valor_quebrado_ou_nao_multiplo_fica_sem_valor(politica, valor):
    # O 6680 sem vírgula seria 200 mensalidades se fosse lido como reais.
    resultado = _estimar(politica, _lida(1, [_pix(valor)]))

    assert resultado.linhas == []
    assert resultado.sem_valor == 1


def test_o_pix_sem_valor_nem_referente_nao_e_cobranca(politica):
    resultado = _estimar(politica, _lida(1, ["Aqui está a chave Pix: 123"]))

    assert (resultado.conversas_lidas, resultado.de_cobranca) == (1, 0)


def test_so_o_lembrete_e_cobranca_sem_valor(politica):
    resultado = _estimar(politica, _lida(1, [LEMBRETE]))

    assert (resultado.de_cobranca, resultado.sem_valor, resultado.linhas) == (1, 1, [])


def test_conversa_sem_cobranca_conta_so_como_lida(politica):
    atendimento = _lida(1, ["Bom dia, em que posso ajudar?"])
    vazia = _lida(2, [])

    resultado = _estimar(politica, atendimento, vazia)

    assert (resultado.conversas_lidas, resultado.de_cobranca, resultado.linhas) == (2, 0, [])


def test_nota_privada_atividade_e_mensagem_do_cliente_nao_contam(politica):
    lida, _ = _lida(1, [])
    mensagens = [
        mensagem(1, 1, 1, _debito("100,20"), T, privada=True),
        mensagem(2, 1, 2, _debito("100,20"), T + 1),
        mensagem(3, 1, 0, _debito("100,20"), T + 2),
        mensagem(4, 1, 3, _debito("100,20"), T + 3),
    ]

    resultado = _estimar(politica, (lida, mensagens))

    assert resultado.de_cobranca == 0


def test_telefone_repetido_vale_a_conversa_mais_recente(politica):
    antiga = _lida(1, [_debito("100,20")], atividade=T)
    recente = _lida(2, [_debito("33,40")], atividade=T + 3600)

    resultado = _estimar(politica, recente, antiga)

    assert _mensalidades(resultado) == [1]
    assert (resultado.com_valor, resultado.telefones_unicos) == ({"debito": 2}, 1)


def test_o_mesmo_telefone_em_formatos_diferentes_e_um_so(politica):
    a = _lida(1, [_debito("100,20")], telefone="+5583999990001", atividade=T)
    b = _lida(2, [_debito("66,80")], telefone="(83) 99999-0001", atividade=T + 60)

    resultado = _estimar(politica, a, b)

    assert _mensalidades(resultado) == [2]


def test_conversa_sem_telefone_valido_nao_entra(politica):
    sem = _lida(1, [_debito("100,20")], telefone=None)
    invalido = _lida(2, [_debito("100,20")], telefone="12345")

    resultado = _estimar(politica, sem, invalido)

    assert resultado.linhas == []
    assert (resultado.de_cobranca, resultado.sem_telefone) == (2, 2)


def test_a_distribuicao_e_os_totais_batem_com_as_linhas(politica):
    resultado = _estimar(
        politica,
        _lida(1, [_debito("33,40")], telefone="+5583999990001"),
        _lida(2, [_debito("33,40")], telefone="+5583999990002"),
        _lida(3, [_pix("66,80")], telefone="+5583999990003"),
        _lida(4, [LEMBRETE], telefone="+5583999990004"),
        _lida(5, ["oi"], telefone="+5583999990005"),
    )

    assert resultado.conversas_lidas == 5
    assert resultado.de_cobranca == 4
    assert resultado.com_valor == {"debito": 2, "pix": 1}
    assert resultado.sem_valor == 1
    assert resultado.telefones_unicos == 3
    assert resultado.mensalidades == {1: 2, 2: 1}


def test_a_mensalidade_vem_da_politica(politica):
    outra = politica.model_copy(update={"mensalidade_centavos": 5000})

    resultado = _estimar(outra, _lida(1, [_debito("100,00")]))

    assert _mensalidades(resultado) == [2]


def test_a_matricula_e_sintetica_e_estavel(politica):
    a = _estimar(politica, _lida(1, [_debito("33,40")], telefone="+5583999990001"))
    a_de_novo = _estimar(politica, _lida(9, [_debito("66,80")], telefone="+5583999990001"))
    outro = _estimar(politica, _lida(2, [_debito("33,40")], telefone="+5583999990002"))

    matricula = a.linhas[0].matricula
    assert matricula.startswith("E-") and len(matricula) == 10
    assert matricula == a_de_novo.linhas[0].matricula
    assert matricula != outro.linhas[0].matricula


def test_o_resultado_nao_mostra_nome_nem_telefone(politica):
    resultado = _estimar(politica, _lida(1, [_debito("33,40")]))

    impresso = repr(resultado) + repr(resultado.linhas) + formatar_resultado_estimativa(resultado)
    assert "99999000" not in impresso
    assert "Fulano" not in impresso
    # A linha guarda os dois para o CSV, mas fora do repr.
    assert resultado.linhas[0].nome == "Fulano Inventado"
    assert resultado.linhas[0].telefone == "+5583999990001"


def test_o_csv_volta_pelo_importador(tmp_path, politica, sessao):
    resultado = _estimar(
        politica,
        _lida(1, [_debito("33,40")], telefone="+5583999990001"),
        _lida(2, [_debito("100,20")], telefone="+5583999990002"),
        _lida(3, [_pix("66,80")], telefone="+5583999990003"),
    )
    caminho = tmp_path / "carteira.csv"

    gravar_csv(resultado, caminho, politica)
    relatorio = importar_carteira(caminho, sessao, politica, CTNFalso(), campanha="teste")

    assert (relatorio.criadas, relatorio.rejeitadas) == (3, [])
    linhas = list(csv.DictReader(caminho.open(encoding="utf-8"), delimiter=";"))
    assert [linha["mensalidades_abertas"] for linha in linhas] == ["1", "3", "2"]
    assert all(linha["regua"] == "" for linha in linhas)
    assert linhas[0]["telefone"] == "+5583999990001"


def test_o_csv_usa_as_colunas_e_o_separador_da_politica(tmp_path, politica):
    resultado = _estimar(politica, _lida(1, [_debito("33,40")]))
    colunas = politica.carteira.colunas.model_copy(update={"telefone": "celular"})
    outra = politica.model_copy(
        update={
            "carteira": politica.carteira.model_copy(update={"separador": ",", "colunas": colunas})
        }
    )

    gravar_csv(resultado, tmp_path / "c.csv", outra)

    primeira = (tmp_path / "c.csv").read_text(encoding="utf-8").splitlines()[0]
    assert primeira == "matricula,nome,celular,mensalidades_abertas,regua"


def test_o_nome_com_separador_e_aspas_volta_inteiro(tmp_path, politica):
    lida, mensagens = _lida(1, [_debito("33,40")])
    lida = conversa_lida(
        {
            **conversa(1, T),
            "meta": {"sender": {"name": 'Ana; "Bia"', "phone_number": "+5583999990001"}},
        }
    )
    resultado = _estimar(politica, (lida, mensagens))

    gravar_csv(resultado, tmp_path / "c.csv", politica)

    (linha,) = csv.DictReader((tmp_path / "c.csv").open(encoding="utf-8"), delimiter=";")
    assert linha["nome"] == 'Ana; "Bia"'


def test_o_csv_recusa_o_arquivo_que_ja_existe(tmp_path, politica):
    resultado = _estimar(politica, _lida(1, [_debito("33,40")]))
    caminho = tmp_path / "carteira.csv"
    caminho.write_text("antigo", encoding="utf-8")

    with pytest.raises(FileExistsError):
        gravar_csv(resultado, caminho, politica)

    assert caminho.read_text(encoding="utf-8") == "antigo"


def test_conversas_da_pasta_le_as_subpastas_sem_rede(tmp_path):
    for pasta, id_ in (("inbox1", 11), ("inbox2", 12)):
        (tmp_path / pasta).mkdir()
        lida, mensagens = _lida(id_, [_debito("33,40")])
        corpo = {"conversa": conversa(id_, T), "mensagens": mensagens}
        (tmp_path / pasta / f"conv{id_}.json").write_text(json.dumps(corpo), encoding="utf-8")
    (tmp_path / "leiame.txt").write_text("ignorado", encoding="utf-8")

    lidas = list(conversas_da_pasta(tmp_path))

    assert [lida.id for lida, _ in lidas] == [11, 12]
    assert [len(mensagens) for _, mensagens in lidas] == [1, 1]


def test_conversas_da_pasta_recusa_o_arquivo_que_nao_e_uma_conversa(tmp_path):
    (tmp_path / "conv1.json").write_text(json.dumps({"outra": 1}), encoding="utf-8")

    with pytest.raises(ValueError, match="conv1.json"):
        list(conversas_da_pasta(tmp_path))


def test_conversas_do_chatwoot_so_faz_get_e_corta_pela_data(politica):
    desde = datetime.fromtimestamp(T - 3600, UTC)
    _, antigas = _lida(3, [_debito("33,40")])
    servidor = ChatwootSimulado(
        [
            conversa(1, T),
            conversa(2, T - 60, telefone="+5583999990002"),
            conversa(3, T - 86400, telefone="+5583999990003"),
        ],
        {
            1: [mensagem(1, 1, 1, _debito("100,20"), T)],
            2: [mensagem(2, 2, 1, _debito("66,80"), T - 60)],
            3: antigas,
        },
        pagina=2,
    )

    with ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, TOKEN, transporte=servidor.transporte
    ) as leitura:
        resultado = estimar_carteira(conversas_do_chatwoot(leitura, INBOX, desde), politica)

    assert {pedido.method for pedido in servidor.pedidos} == {"GET"}
    assert (resultado.conversas_lidas, resultado.telefones_unicos) == (2, 2)
    assert sorted(_mensalidades(resultado)) == [2, 3]


def test_amostra_real_so_confere_contagens(tmp_path, politica, sessao):
    amostra = ler_amostra()
    conversas = [(conversa_lida(a["conversa"]), a["mensagens"]) for a in amostra]

    resultado = estimar_carteira(conversas, politica)

    assert resultado.conversas_lidas == len(amostra)
    assert resultado.de_cobranca <= resultado.conversas_lidas
    assert resultado.de_cobranca == (
        resultado.sem_telefone + resultado.sem_valor + sum(resultado.com_valor.values())
    )
    assert resultado.telefones_unicos == len(resultado.linhas) <= sum(resultado.com_valor.values())
    assert sum(resultado.mensalidades.values()) == resultado.telefones_unicos
    assert all(n >= 1 for n in resultado.mensalidades)
    if resultado.linhas:
        caminho = tmp_path / "estimada.csv"
        gravar_csv(resultado, caminho, politica)
        relatorio = importar_carteira(caminho, sessao, politica, CTNFalso(), campanha="teste")
        assert relatorio.rejeitadas == []
        assert relatorio.criadas == resultado.telefones_unicos
