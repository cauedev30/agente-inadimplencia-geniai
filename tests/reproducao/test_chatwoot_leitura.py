"""A leitura do Chatwoot (tarefa 10.1): só GET, paginada, com o corte pela data."""

from datetime import UTC, datetime

import httpx
import pytest

from agente.reproducao.chatwoot_leitura import ChatwootLeitura, ConversaLida

from .fabrica import (
    CONTA,
    INBOX,
    SEGUNDA_10H,
    TELEFONE,
    TOKEN,
    ChatwootSimulado,
    conversa,
    mensagem,
)

DESDE = datetime.fromtimestamp(SEGUNDA_10H - 3 * 86400, UTC)


def _leitura(servidor: ChatwootSimulado) -> ChatwootLeitura:
    return ChatwootLeitura(
        "https://chatwoot.invalid/", CONTA, TOKEN, transporte=servidor.transporte
    )


def test_conversas_pagina_ate_a_atividade_passar_de_desde():
    recentes = [conversa(i, SEGUNDA_10H - i * 3600) for i in range(1, 6)]
    antigas = [conversa(i, SEGUNDA_10H - 5 * 86400 - i) for i in range(10, 14)]
    servidor = ChatwootSimulado(antigas + recentes, {}, pagina=2)

    with _leitura(servidor) as leitura:
        lidas = leitura.conversas(INBOX, DESDE)

    assert [c.id for c in lidas] == [1, 2, 3, 4, 5]
    # A página 3 ainda tem uma conversa recente (a 5); a 4 é toda antiga e fecha a leitura.
    paginas = [int(p.url.params["page"]) for p in servidor.pedidos]
    assert paginas == [1, 2, 3, 4]
    assert all(p.url.params["inbox_id"] == str(INBOX) for p in servidor.pedidos)
    assert all(p.url.params["status"] == "all" for p in servidor.pedidos)
    assert all(p.url.params["assignee_type"] == "all" for p in servidor.pedidos)


def test_conversas_devolve_o_telefone_do_meta_e_os_instantes():
    servidor = ChatwootSimulado([conversa(3, SEGUNDA_10H, criada=SEGUNDA_10H - 900)], {})

    with _leitura(servidor) as leitura:
        [lida] = leitura.conversas(INBOX, DESDE)

    assert lida == ConversaLida(
        id=3,
        conta_id=CONTA,
        inbox_id=INBOX,
        telefone=TELEFONE,
        criada_em=datetime.fromtimestamp(SEGUNDA_10H - 900, UTC),
        ultima_atividade_em=datetime.fromtimestamp(SEGUNDA_10H, UTC),
    )


def test_conversa_sem_telefone_vem_com_telefone_none():
    sem_meta = conversa(2, SEGUNDA_10H - 10)
    sem_meta["meta"] = {}
    servidor = ChatwootSimulado(
        [conversa(1, SEGUNDA_10H, telefone=None), sem_meta],
        {},
    )

    with _leitura(servidor) as leitura:
        lidas = leitura.conversas(INBOX, DESDE)

    assert [(c.id, c.telefone) for c in lidas] == [(1, None), (2, None)]


def test_conversas_sem_nenhuma_devolve_lista_vazia():
    servidor = ChatwootSimulado([], {})

    with _leitura(servidor) as leitura:
        assert leitura.conversas(INBOX, DESDE) == []
    assert len(servidor.pedidos) == 1


def test_mensagens_pagina_com_before_e_devolve_em_ordem():
    todas = [mensagem(100 + i, 5, 0 if i % 2 == 0 else 1, f"texto {i}", 1000 + i) for i in range(7)]
    servidor = ChatwootSimulado([], {5: todas}, pagina=3)

    with _leitura(servidor) as leitura:
        lidas = leitura.mensagens(5)

    assert [m["id"] for m in lidas] == [100, 101, 102, 103, 104, 105, 106]
    antes = [p.url.params.get("before") for p in servidor.pedidos]
    # A 1ª página não leva `before`; as seguintes pedem as anteriores ao menor id já visto.
    assert antes == [None, "104", "101", "100"]


def test_mensagens_ordena_por_created_at_e_nao_repete():
    desordenadas = [
        mensagem(12, 5, 0, "c", 3000),
        mensagem(10, 5, 0, "a", 1000),
        mensagem(11, 5, 1, "b", 2000),
    ]
    servidor = ChatwootSimulado([], {5: desordenadas}, pagina=20)

    with _leitura(servidor) as leitura:
        lidas = leitura.mensagens(5)

    assert [m["id"] for m in lidas] == [10, 11, 12]


def test_mensagens_de_conversa_sem_mensagens():
    servidor = ChatwootSimulado([], {})

    with _leitura(servidor) as leitura:
        assert leitura.mensagens(5) == []


def test_so_sai_get_e_o_token_vai_no_cabecalho():
    servidor = ChatwootSimulado(
        [conversa(1, SEGUNDA_10H)], {1: [mensagem(1, 1, 0, "oi", SEGUNDA_10H)]}
    )

    with _leitura(servidor) as leitura:
        for lida in leitura.conversas(INBOX, DESDE):
            leitura.mensagens(lida.id)

    assert servidor.pedidos
    assert {p.method for p in servidor.pedidos} == {"GET"}
    assert {p.headers["api_access_token"] for p in servidor.pedidos} == {TOKEN}


def test_a_classe_nao_tem_metodo_de_escrita():
    publicos = {nome for nome in dir(ChatwootLeitura) if not nome.startswith("_")}

    assert publicos == {"conversas", "mensagens", "fechar"}


def test_erro_http_levanta():
    def recusar(_pedido: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    leitura = ChatwootLeitura(
        "https://chatwoot.invalid", CONTA, "token-errado", transporte=httpx.MockTransport(recusar)
    )

    with leitura, pytest.raises(httpx.HTTPStatusError):
        leitura.conversas(INBOX, DESDE)
