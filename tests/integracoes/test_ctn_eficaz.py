"""O `CTNEficaz` (API EFICAZ, só leitura) contra um servidor de mentira.

Nenhum teste faz rede: o `httpx.MockTransport` responde no formato real da API (sondagens de
2026-11-04 e 2026-11-06), com dado inventado.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from agente.integracoes.ctn import (
    ClienteCTN,
    ClienteNaoEncontrado,
    CTNIndisponivel,
    InadimplenteCTN,
    MensalidadeAberta,
)
from agente.integracoes.ctn_eficaz import (
    PAGINAS_MAXIMAS_POR_ROTA,
    CTNEficaz,
    forma_do_historico,
)
from agente.tipos import FormaHistorico, PagamentoHistorico
from tests.integracoes.test_contrato_ctn import DATA_REF, ContratoLeituraCTN

URL = "https://api.exemplo-inventado.test"
CHAVE = "eg_SEGREDO_INVENTADO_123"
UNIDADE = "jp"
MATRICULA = "xx000000001"
AGORA = datetime(2026, 11, 6, 12, 30, tzinfo=UTC)

# Por rota: as linhas e a coluna de data do período (`de`/`ate`), como no catálogo da API.
COLUNA_DATA = {
    "/v1/ctn/inadimplencia": "data_ref",
    "/v1/ctn/lancamentos": "data_baixa",
    "/v1/ctn/lancamentos_bx": "dia_relatorio",
    "/v1/ctn/filiados_full": "data_filiacao",
}
RESERVADOS = {"unidade", "ordem", "direcao", "pagina", "limite", "de", "ate"}


def _bx(id_, matricula, forma, mes_ref, dt_baixa, valor, coletado_em, dia=None):
    return {
        "unidade": UNIDADE,
        "id": id_,
        "matricula": matricula,
        "nome": "Nome Inventado",
        "forma": forma,
        "mes_ref": mes_ref,
        "dt_baixa": dt_baixa,
        "valor": valor,
        "dia_relatorio": dia or dt_baixa[:10],
        "coletado_em": coletado_em,
    }


def _lanc(id_, mes_ano, status, valor=33.4, data_baixa=None, matricula=MATRICULA):
    return {
        "unidade": UNIDADE,
        "id": id_,
        "matricula_titular": matricula,
        "mes_ano": mes_ano,
        "forma_pgto": "VISA",
        "valor": valor,
        "desconto": 0,
        "data_baixa": data_baixa,
        "status_lancamento": status,
    }


def _inad(matricula, nome, telefone, atraso, ate_corte, ult_mesref, data_ref):
    return {
        "unidade": UNIDADE,
        "franquia": UNIDADE,
        "data_ref": data_ref,
        "matricula": matricula,
        "regua": 7,
        "ult_mesref": ult_mesref,
        "ult_dt_baixa": "2026-04-10T13:00:00.000Z",
        "nome": nome,
        "telefone": telefone,
        "meses_atraso": atraso,
        "meses_ate_corte": ate_corte,
    }


def tabelas_padrao():
    return {
        "/v1/ctn/filiados_full": [
            {
                "unidade": UNIDADE,
                "matricula": MATRICULA,
                "cpf": "111.222.333-44",
                "nome": "Cliente Inventado",
                "telefone": "(83) 91234 5678",
                "data_filiacao": "2020-01-10",
                "status": "Ativo",
            }
        ],
        "/v1/ctn/inadimplencia": [
            _inad(
                MATRICULA, "Cliente Inventado", "(83) 91234 5678", 2, 4, "2026-09-01", "2026-11-06"
            ),
            _inad(
                "xx000000002", "Outro Inventado", "(83) 3234 5678", 1, 5, "2026-10-01", "2026-11-06"
            ),
            _inad(
                MATRICULA, "Cliente Inventado", "(83) 91234 5678", 1, 5, "2026-10-01", "2026-11-05"
            ),
        ],
        "/v1/ctn/lancamentos": [
            _lanc(1, "2026/08", "arrecadada", data_baixa="2026-08-12"),
            _lanc(2, "2026/09", "rejeitada", data_baixa="2026-09-12"),
            _lanc(3, "2026/10", "rejeitada", data_baixa="2026-10-12"),
            _lanc(4, "2026/11", "aberta"),
        ],
        "/v1/ctn/lancamentos_bx": [
            _bx(10, MATRICULA, "PIX", "2026/06", "2026-08-10", 33.4, "2026-08-10T10:00:00.000Z"),
            _bx(11, MATRICULA, "VISA", "2026/07", "2026-09-10", 33.4, "2026-09-10T10:00:00.000Z"),
            _bx(12, MATRICULA, "PIX", "2026/08", "2026-10-10", 33.4, "2026-10-10T10:00:00.000Z"),
            _bx(
                20, "xx000000002", "PIX", "2026/10", "2026-11-06", 33.4, "2026-11-06T11:00:00.000Z"
            ),
            _bx(21, MATRICULA, "PIX", "2026/05", "2026-11-06", 33.4, "2026-11-06T10:05:00.000Z"),
            _bx(
                22,
                "xx000000003",
                "BOLETO SANTANDER - CORPORATIVO",
                "2026/10",
                "2026-11-05",
                33.4,
                "2026-11-05T10:00:00.000Z",
            ),
        ],
    }


def servidor(tabelas=None, chamadas=None, chave=CHAVE):
    """Um servidor com a regra da API: igualdade por coluna, `de`/`ate` na coluna de data da rota,
    `ordem` e `pagina`/`limite` (o corpo traz `dados`, `mais` e `uso`)."""
    tabelas = tabelas_padrao() if tabelas is None else tabelas
    chamadas = [] if chamadas is None else chamadas

    def tratar(requisicao: httpx.Request) -> httpx.Response:
        chamadas.append(requisicao)
        if requisicao.headers.get("authorization") != f"Bearer {chave}":
            return httpx.Response(401, json={"erro": "chave inválida"})
        rota = requisicao.url.path
        if rota not in tabelas:
            return httpx.Response(404, json={"erro": "rota inexistente"})
        params = dict(requisicao.url.params)
        if params.get("unidade") != UNIDADE or "ordem" not in params:
            return httpx.Response(400, json={"erro": "faltou unidade ou ordem"})
        linhas = list(tabelas[rota])
        for coluna, valor in params.items():
            if coluna not in RESERVADOS:
                linhas = [x for x in linhas if str(x.get(coluna)) == valor]
        coluna_data = COLUNA_DATA[rota]
        if "de" in params:
            linhas = [x for x in linhas if (x.get(coluna_data) or "")[:10] >= params["de"]]
        if "ate" in params:
            linhas = [x for x in linhas if (x.get(coluna_data) or "")[:10] <= params["ate"]]
        elif "de" in params:
            return httpx.Response(400, json={"erro": "de sem ate"})
        linhas.sort(key=lambda x: str(x.get(params["ordem"])))
        limite = int(params.get("limite", 50))
        pagina = int(params.get("pagina", 0))
        trecho = linhas[pagina * limite : (pagina + 1) * limite]
        corpo = {
            "dados": trecho,
            "mais": (pagina + 1) * limite < len(linhas),
            "uso": {"chamadas_hoje": len(chamadas), "limite_minuto": 60},
        }
        return httpx.Response(200, json=corpo)

    return tratar


def cliente(tratar, **extra):
    extra.setdefault("agora", lambda: AGORA)
    return CTNEficaz(URL, CHAVE, UNIDADE, transporte=httpx.MockTransport(tratar), **extra)


class TestCTNEficaz(ContratoLeituraCTN):
    @pytest.fixture
    def ctn_e_matricula(self):
        return cliente(servidor()), MATRICULA

    @pytest.fixture
    def ctn_fora_do_ar_e_matricula(self):
        return cliente(lambda _requisicao: httpx.Response(503, text="fora")), MATRICULA


# ---- os 5 métodos, no formato real ----


def test_consultar_cliente_traz_o_telefone_em_e164_e_o_cpf_so_com_digitos():
    assert cliente(servidor()).consultar_cliente(MATRICULA) == ClienteCTN(
        MATRICULA, "Cliente Inventado", "+5583912345678", "11122233344"
    )


def test_consultar_cliente_sem_cpf_devolve_none():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/filiados_full"][0]["cpf"] = None
    assert cliente(servidor(tabelas)).consultar_cliente(MATRICULA).cpf is None


def test_consultar_cliente_com_telefone_que_nao_e_brasileiro_devolve_vazio():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/filiados_full"][0]["telefone"] = "sem telefone"
    assert cliente(servidor(tabelas)).consultar_cliente(MATRICULA).telefone == ""


def test_o_retrato_converte_telefone_datas_e_inteiros():
    retrato = {i.matricula: i for i in cliente(servidor()).inadimplentes(DATA_REF)}
    assert retrato[MATRICULA] == InadimplenteCTN(
        MATRICULA, "Cliente Inventado", "+5583912345678", 2, 4, date(2026, 9, 1), DATA_REF
    )
    # O fixo (sem o 9) também vira E.164.
    assert retrato["xx000000002"].telefone == "+558332345678"


def test_o_retrato_pede_so_o_dia_com_de_e_ate_iguais():
    chamadas = []
    list(cliente(servidor(chamadas=chamadas)).inadimplentes(DATA_REF))
    params = dict(chamadas[0].url.params)
    assert chamadas[0].url.path == "/v1/ctn/inadimplencia"
    assert params["de"] == params["ate"] == "2026-11-06"


def test_o_retrato_com_ult_mesref_nulo():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/inadimplencia"][0]["ult_mesref"] = None
    retrato = list(cliente(servidor(tabelas)).inadimplentes(DATA_REF))
    assert next(i for i in retrato if i.matricula == MATRICULA).ult_mesref is None


def test_o_retrato_pagina_ate_o_mais_ser_falso_e_sempre_manda_ordem_e_unidade():
    tabelas = {
        "/v1/ctn/inadimplencia": [
            _inad(f"xx{n:09d}", f"Nome {n}", "(83) 91234 5678", 1, 5, "2026-10-01", "2026-11-06")
            for n in range(5)
        ]
    }
    chamadas = []
    ctn = cliente(servidor(tabelas, chamadas), linhas_por_pagina=2)
    matriculas = [i.matricula for i in ctn.inadimplentes(DATA_REF)]
    assert matriculas == [f"xx{n:09d}" for n in range(5)]
    assert [dict(c.url.params)["pagina"] for c in chamadas] == ["0", "1", "2"]
    for chamada in chamadas:
        params = dict(chamada.url.params)
        assert params["unidade"] == UNIDADE
        assert params["ordem"] == "matricula"
        assert params["direcao"] == "asc"
        assert params["limite"] == "2"
    assert all(c.headers["authorization"] == f"Bearer {CHAVE}" for c in chamadas)


def test_o_retrato_que_falha_na_segunda_pagina_entrega_a_primeira_e_levanta():
    tabelas = {
        "/v1/ctn/inadimplencia": [
            _inad(f"xx{n:09d}", f"Nome {n}", "(83) 91234 5678", 1, 5, "2026-10-01", "2026-11-06")
            for n in range(4)
        ]
    }
    ok = servidor(tabelas)

    def tratar(requisicao):
        if dict(requisicao.url.params)["pagina"] == "1":
            return httpx.Response(500)
        return ok(requisicao)

    iterador = cliente(tratar, linhas_por_pagina=2).inadimplentes(DATA_REF)
    assert [next(iterador).matricula for _ in range(2)] == ["xx000000000", "xx000000001"]
    with pytest.raises(CTNIndisponivel):
        next(iterador)


def test_linha_do_retrato_sem_meses_de_atraso_e_resposta_invalida():
    tabelas = tabelas_padrao()
    del tabelas["/v1/ctn/inadimplencia"][0]["meses_atraso"]
    with pytest.raises(CTNIndisponivel, match="formato"):
        list(cliente(servidor(tabelas)).inadimplentes(DATA_REF))


def test_historico_filtra_pelos_meses_e_ordena_do_mais_antigo_ao_mais_novo():
    historico = cliente(servidor()).historico_pagamentos(MATRICULA, meses=3)
    assert historico == [
        PagamentoHistorico(date(2026, 9, 10), FormaHistorico.CARTAO, 3340),
        PagamentoHistorico(date(2026, 10, 10), FormaHistorico.PIX, 3340),
        PagamentoHistorico(date(2026, 11, 6), FormaHistorico.PIX, 3340),
    ]


def test_historico_de_matricula_sem_baixas_vem_vazio():
    assert cliente(servidor()).historico_pagamentos("xx999999999", meses=12) == []


def test_mensalidades_em_aberto_sao_as_rejeitadas_e_ignoram_a_aberta_e_a_arrecadada():
    abertas = cliente(servidor()).mensalidades_em_aberto(MATRICULA)
    assert abertas == [MensalidadeAberta("2026/09", 3340), MensalidadeAberta("2026/10", 3340)]


def test_mensalidades_em_aberto_pede_so_as_rejeitadas_da_matricula():
    chamadas = []
    cliente(servidor(chamadas=chamadas)).mensalidades_em_aberto(MATRICULA)
    lancamentos = next(c for c in chamadas if c.url.path == "/v1/ctn/lancamentos")
    params = dict(lancamentos.url.params)
    assert params["matricula_titular"] == MATRICULA
    assert params["status_lancamento"] == "rejeitada"
    assert "ordem" in params


def test_baixa_mais_nova_que_o_lancamento_tira_o_mes_das_abertas():
    tabelas = tabelas_padrao()
    # Pago em 2026-11-06 o mês 2026/10, que o lançamento rejeitou em 2026-10-12.
    tabelas["/v1/ctn/lancamentos_bx"].append(
        _bx(30, MATRICULA, "PIX", "2026/10", "2026-11-06", 33.4, "2026-11-06T09:00:00.000Z")
    )
    abertas = cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)
    assert abertas == [MensalidadeAberta("2026/09", 3340)]


def test_baixa_mais_velha_que_o_lancamento_nao_tira_o_mes():
    tabelas = tabelas_padrao()
    # Pagamento anterior à recusa de 2026-10-12: o lançamento continua em aberto.
    tabelas["/v1/ctn/lancamentos_bx"].append(
        _bx(31, MATRICULA, "PIX", "2026/10", "2026-10-01", 33.4, "2026-10-01T09:00:00.000Z")
    )
    abertas = cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)
    assert [a.mes_ano for a in abertas] == ["2026/09", "2026/10"]


def test_baixa_de_outro_mes_nao_tira_nada_e_lancamento_sem_data_cai_com_qualquer_baixa():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/lancamentos"][2]["data_baixa"] = None
    tabelas["/v1/ctn/lancamentos_bx"].append(
        _bx(32, MATRICULA, "PIX", "2026/10", "2026-10-01", 33.4, "2026-10-01T09:00:00.000Z")
    )
    abertas = cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)
    assert [a.mes_ano for a in abertas] == ["2026/09"]


def _abertas_com(lancamentos, baixas):
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/lancamentos"] = lancamentos
    tabelas["/v1/ctn/lancamentos_bx"] = baixas
    return cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)


def test_uma_baixa_tira_so_um_dos_lancamentos_rejeitados_do_mesmo_mes():
    lancamentos = [
        _lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-05"),
        _lanc(2, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12"),
    ]
    baixas = [_bx(30, MATRICULA, "PIX", "2026/10", "2026-11-06", "99.99", "2026-11-06T09:00:00Z")]
    assert _abertas_com(lancamentos, baixas) == [MensalidadeAberta("2026/10", 9999)]


def test_com_uma_baixa_so_o_lancamento_mais_antigo_do_mes_e_pago():
    # Os dois cabem na baixa; os lançamentos vêm do mais novo ao mais velho, e o mais antigo casa
    # primeiro: sobra o de R$ 80, o mais novo. Sem a ordenação por data, sobraria o de R$ 50.
    lancamentos = [
        _lanc(1, "2026/10", "rejeitada", valor="80.00", data_baixa="2026-10-12"),
        _lanc(2, "2026/10", "rejeitada", valor="50.00", data_baixa="2026-10-05"),
    ]
    baixas = [_bx(30, MATRICULA, "PIX", "2026/10", "2026-11-06", "80.00", "2026-11-06T09:00:00Z")]
    assert _abertas_com(lancamentos, baixas) == [MensalidadeAberta("2026/10", 8000)]


def test_o_pareamento_fecha_os_dois_lancamentos_quando_o_guloso_deixaria_um_aberto():
    # A (R$ 50, 10-05) e B (R$ 50, 10-12); X de R$ 60 entre as duas datas só serve a A, e Y de
    # R$ 50 depois de B serve aos dois: X-A e Y-B pagam tudo. Y-A deixaria B em aberto.
    lancamentos = [
        _lanc(1, "2026/10", "rejeitada", valor="50.00", data_baixa="2026-10-05"),
        _lanc(2, "2026/10", "rejeitada", valor="50.00", data_baixa="2026-10-12"),
    ]
    baixas = [
        _bx(30, MATRICULA, "PIX", "2026/10", "2026-10-08", "60.00", "2026-10-08T09:00:00Z"),
        _bx(31, MATRICULA, "PIX", "2026/10", "2026-11-06", "50.00", "2026-11-06T09:00:00Z"),
    ]
    assert _abertas_com(lancamentos, baixas) == []


def test_o_pareamento_fecha_os_dois_quando_a_baixa_menor_chega_antes_da_maior():
    # A de R$ 80 e B de R$ 50; a de R$ 50 só cobre B e a de R$ 80 cobre os dois: A pede a de 80.
    lancamentos = [
        _lanc(1, "2026/10", "rejeitada", valor="80.00", data_baixa="2026-10-05"),
        _lanc(2, "2026/10", "rejeitada", valor="50.00", data_baixa="2026-10-12"),
    ]
    baixas = [
        _bx(30, MATRICULA, "PIX", "2026/10", "2026-10-20", "80.00", "2026-10-20T09:00:00Z"),
        _bx(31, MATRICULA, "PIX", "2026/10", "2026-10-25", "50.00", "2026-10-25T09:00:00Z"),
    ]
    assert _abertas_com(lancamentos, baixas) == []


def test_duas_baixas_tiram_os_dois_lancamentos_do_mesmo_mes():
    lancamentos = [
        _lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-05"),
        _lanc(2, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12"),
    ]
    baixas = [
        _bx(30, MATRICULA, "PIX", "2026/10", "2026-11-05", "99.99", "2026-11-05T09:00:00Z"),
        _bx(31, MATRICULA, "PIX", "2026/10", "2026-11-06", "99.99", "2026-11-06T09:00:00Z"),
    ]
    assert _abertas_com(lancamentos, baixas) == []


def test_baixa_parcial_nao_tira_o_mes():
    lancamentos = [_lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12")]
    baixas = [_bx(30, MATRICULA, "PIX", "2026/10", "2026-11-06", "50.00", "2026-11-06T09:00:00Z")]
    assert _abertas_com(lancamentos, baixas) == [MensalidadeAberta("2026/10", 9999)]


def test_baixa_de_valor_maior_tira_o_mes():
    lancamentos = [_lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12")]
    baixas = [_bx(30, MATRICULA, "PIX", "2026/10", "2026-11-06", "120.00", "2026-11-06T09:00:00Z")]
    assert _abertas_com(lancamentos, baixas) == []


def test_baixa_anterior_ao_lancamento_nao_tira_o_mes_e_do_mesmo_dia_tira():
    lancamentos = [_lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12")]
    antes = [_bx(30, MATRICULA, "PIX", "2026/10", "2026-10-11", "99.99", "2026-10-11T09:00:00Z")]
    no_dia = [_bx(31, MATRICULA, "PIX", "2026/10", "2026-10-12", "99.99", "2026-10-12T09:00:00Z")]
    assert _abertas_com(lancamentos, antes) == [MensalidadeAberta("2026/10", 9999)]
    assert _abertas_com(lancamentos, no_dia) == []


def test_baixa_de_outro_mes_nao_mexe_nos_lancamentos():
    lancamentos = [_lanc(1, "2026/10", "rejeitada", valor="99.99", data_baixa="2026-10-12")]
    baixas = [_bx(30, MATRICULA, "PIX", "2026/09", "2026-11-06", "99.99", "2026-11-06T09:00:00Z")]
    assert _abertas_com(lancamentos, baixas) == [MensalidadeAberta("2026/10", 9999)]


@pytest.mark.parametrize(
    ("bruto", "centavos"),
    [(33.4, 3340), (33, 3300), ("33.40", 3340), ("33,40", 3340), (0.1 + 0.2, 30), (33.405, 3341)],
)
def test_o_valor_em_reais_vira_centavos_sem_erro_de_ponto_flutuante(bruto, centavos):
    tabelas = tabelas_padrao()
    for linha in tabelas["/v1/ctn/lancamentos"]:
        linha["valor"] = bruto
    abertas = cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)
    assert abertas[0].valor_centavos == centavos


def test_baixas_desde_converte_os_campos_e_filtra_pelo_cursor():
    desde = datetime(2026, 11, 6, 10, 6, tzinfo=UTC)
    baixas = cliente(servidor()).baixas_desde(desde)
    assert [b.id_externo for b in baixas] == ["20"]
    baixa = baixas[0]
    assert baixa.matricula == "xx000000002"
    assert baixa.mes_ref == "2026/10"
    assert baixa.valor_centavos == 3340
    assert baixa.forma is FormaHistorico.PIX
    assert baixa.pago_em == datetime(2026, 11, 6, tzinfo=UTC)
    assert baixa.coletado_em == datetime(2026, 11, 6, 11, tzinfo=UTC)


def test_baixas_desde_inclui_a_coletada_no_instante_do_cursor():
    # Duas baixas de uma mesma coleta: o cursor guardado e o dessa coleta, e a segunda nao se perde.
    desde = datetime(2026, 11, 6, 10, 5, tzinfo=UTC)
    baixas = cliente(servidor()).baixas_desde(desde)
    assert [b.id_externo for b in baixas] == ["21", "20"]
    assert baixas[0].coletado_em == desde


def test_baixas_desde_nao_repete_a_baixa_que_vem_duas_vezes():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/lancamentos_bx"].append(dict(tabelas["/v1/ctn/lancamentos_bx"][3]))
    baixas = cliente(servidor(tabelas)).baixas_desde(datetime(2026, 11, 6, tzinfo=UTC))
    assert [b.id_externo for b in baixas].count("20") == 1


def test_baixas_desde_ordena_pelo_coletado_em_e_inclui_o_dia_anterior_ao_cursor():
    # O relatório do dia pode ter sido coletado depois da meia-noite: a janela abre um dia antes.
    chamadas = []
    desde = datetime(2026, 11, 5, 9, tzinfo=UTC)
    baixas = cliente(servidor(chamadas=chamadas)).baixas_desde(desde)
    assert [b.id_externo for b in baixas] == ["22", "21", "20"]
    params = dict(chamadas[0].url.params)
    assert params["de"] == "2026-11-04"
    assert params["ate"] == "2026-11-06"


def test_baixas_desde_um_cursor_antigo_consulta_em_janelas_de_ate_31_dias():
    chamadas = []
    desde = AGORA - timedelta(days=70)
    cliente(servidor(chamadas=chamadas)).baixas_desde(desde)
    assert len(chamadas) >= 3
    janelas = [
        (
            date.fromisoformat(dict(c.url.params)["de"]),
            date.fromisoformat(dict(c.url.params)["ate"]),
        )
        for c in chamadas
    ]
    assert all((fim - inicio).days + 1 <= 31 for inicio, fim in janelas)
    # As janelas se emendam sem falha e a última acaba hoje.
    for (_, fim), (inicio, _) in zip(janelas, janelas[1:], strict=False):
        assert inicio == fim + timedelta(days=1)
    assert janelas[-1][1] == AGORA.date()


def test_baixa_sem_coletado_em_e_resposta_invalida():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/lancamentos_bx"][4]["coletado_em"] = None
    with pytest.raises(CTNIndisponivel, match="formato"):
        cliente(servidor(tabelas)).baixas_desde(datetime(2026, 11, 6, tzinfo=UTC))


# ---- a paginação falha fechada ----


def _resposta_fixa(corpo):
    return lambda _r: httpx.Response(200, json=corpo)


_LINHA_RETRATO = _inad("xx000000001", "Nome", "(83) 91234 5678", 1, 5, "2026-10-01", "2026-11-06")


@pytest.mark.parametrize(
    "extra",
    [{}, {"mais": "false"}, {"mais": "true"}, {"mais": 0}, {"mais": 1}, {"mais": None}],
    ids=["ausente", "texto_false", "texto_true", "zero", "um", "nulo"],
)
def test_mais_que_nao_e_booleano_ou_falta_e_formato_inesperado(extra):
    ctn = cliente(_resposta_fixa({"dados": [_LINHA_RETRATO], **extra}))
    with pytest.raises(CTNIndisponivel, match="formato") as info:
        list(ctn.inadimplentes(DATA_REF))
    _confere_erro_limpo(info.value)
    assert "false" not in str(info.value)


def test_pagina_vazia_com_mais_verdadeiro_levanta_em_vez_de_laco():
    chamadas = []

    def tratar(requisicao):
        chamadas.append(requisicao)
        return httpx.Response(200, json={"dados": [], "mais": True})

    with pytest.raises(CTNIndisponivel, match="formato"):
        list(cliente(tratar).inadimplentes(DATA_REF))
    assert len(chamadas) == 1


def test_passar_do_teto_de_paginas_levanta_e_nunca_devolve_o_resultado_cortado():
    chamadas = []

    def tratar(requisicao):
        chamadas.append(requisicao)
        return httpx.Response(200, json={"dados": [_LINHA_RETRATO], "mais": True})

    ctn = cliente(tratar, limite_minuto=100_000)
    assert PAGINAS_MAXIMAS_POR_ROTA == 200
    with pytest.raises(CTNIndisponivel, match="limite de páginas"):
        list(ctn.inadimplentes(DATA_REF))
    assert len(chamadas) == PAGINAS_MAXIMAS_POR_ROTA


# ---- o valor em texto ----


@pytest.mark.parametrize(
    ("bruto", "centavos"), [("1234.56", 123456), ("33.4", 3340), ("33", 3300), ("1234,5", 123450)]
)
def test_valores_em_texto_sem_ambiguidade_seguem_validos(bruto, centavos):
    tabelas = tabelas_padrao()
    for linha in tabelas["/v1/ctn/lancamentos"]:
        linha["valor"] = bruto
    abertas = cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)
    assert abertas[0].valor_centavos == centavos


@pytest.mark.parametrize("bruto", ["1.234", "1,234", "0.001", "-5", "-33.40", -5, -0.01])
def test_valor_ambiguo_ou_negativo_e_formato_inesperado(bruto):
    tabelas = tabelas_padrao()
    for linha in tabelas["/v1/ctn/lancamentos"]:
        linha["valor"] = bruto
    with pytest.raises(CTNIndisponivel, match="formato"):
        cliente(servidor(tabelas)).mensalidades_em_aberto(MATRICULA)


# ---- o "hoje" é o de Brasília ----

ULTIMO_DIA_22H_EM_BRASILIA = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)  # 31/10, 22h em Brasília


def test_o_historico_usa_o_mes_de_brasilia_as_22h_do_ultimo_dia_do_mes():
    # Em UTC já é novembro e `meses=1` deixaria outubro de fora.
    ctn = cliente(servidor(), agora=lambda: ULTIMO_DIA_22H_EM_BRASILIA)
    datas = [p.data for p in ctn.historico_pagamentos(MATRICULA, meses=1)]
    assert date(2026, 10, 10) in datas
    assert date(2026, 9, 10) not in datas


def test_a_janela_das_baixas_acaba_no_dia_de_brasilia():
    chamadas = []
    ctn = cliente(servidor(chamadas=chamadas), agora=lambda: ULTIMO_DIA_22H_EM_BRASILIA)
    ctn.baixas_desde(datetime(2026, 10, 30, tzinfo=UTC))
    assert dict(chamadas[-1].url.params)["ate"] == "2026-10-31"


# ---- o mapa de formas ----


@pytest.mark.parametrize(
    ("bruta", "esperada"),
    [
        ("PIX", FormaHistorico.PIX),
        ("pix ", FormaHistorico.PIX),
        ("BOLETO SANTANDER - CORPORATIVO", FormaHistorico.BOLETO),
        ("ENERGISA", FormaHistorico.ENERGIA),
        ("MASTERCARD", FormaHistorico.CARTAO),
        ("VISA", FormaHistorico.CARTAO),
        ("ELO", FormaHistorico.CARTAO),
        ("HIPERCARD", FormaHistorico.CARTAO),
        ("MASTER", FormaHistorico.CARTAO),
        ("DISCOVER", FormaHistorico.CARTAO),
        # Os textos brutos da leitura real da rota de baixas (2026-11-07).
        ("CAIXA ECONOMICA FEDERAL", FormaHistorico.OUTRO),
        ("DIRETO NO CARTAO - CORPORATIVO", FormaHistorico.CARTAO),
        ("DIRETO NO CARTÃO - DINHEIRO", FormaHistorico.OUTRO),
        ("DIRETO NO CARTAO - DINHEIRO", FormaHistorico.OUTRO),
        ("FORMA NOVA", FormaHistorico.OUTRO),
        ("", FormaHistorico.OUTRO),
        (None, FormaHistorico.OUTRO),
    ],
)
def test_mapa_de_formas(bruta, esperada):
    assert forma_do_historico(bruta) is esperada


def test_todos_os_textos_de_forma_da_leitura_real_tem_destino():
    esperado = {
        # Não confirmado se é boleto (pergunta ao dev): fica OUTRO até a resposta.
        "CAIXA ECONOMICA FEDERAL": FormaHistorico.OUTRO,
        "DIRETO NO CARTAO - CORPORATIVO": FormaHistorico.CARTAO,
        "DIRETO NO CARTÃO - DINHEIRO": FormaHistorico.OUTRO,
        "DISCOVER": FormaHistorico.CARTAO,
        "ELO": FormaHistorico.CARTAO,
        "ENERGISA": FormaHistorico.ENERGIA,
        "MASTER": FormaHistorico.CARTAO,
        "MASTERCARD": FormaHistorico.CARTAO,
        "PIX": FormaHistorico.PIX,
        "VISA": FormaHistorico.CARTAO,
    }
    assert {texto: forma_do_historico(texto) for texto in esperado} == esperado


# ---- erros ----


def test_404_do_cadastro_e_cliente_nao_encontrado():
    ctn = cliente(lambda _r: httpx.Response(404, json={"erro": "não achei"}))
    with pytest.raises(ClienteNaoEncontrado):
        ctn.consultar_cliente(MATRICULA)


def test_400_nunca_e_cliente_nao_encontrado():
    """Um 400 é pedido malformado (falta de `ordem`, período inválido...), falha do CTN."""
    ctn = cliente(lambda _r: httpx.Response(400, json={"erro": "pedido ruim"}))
    for chamada in (
        lambda: ctn.consultar_cliente(MATRICULA),
        lambda: ctn.historico_pagamentos(MATRICULA, meses=12),
        lambda: ctn.mensalidades_em_aberto(MATRICULA),
    ):
        with pytest.raises(CTNIndisponivel, match="400") as info:
            chamada()
        assert not isinstance(info.value, ClienteNaoEncontrado)


def test_404_do_historico_e_das_mensalidades_e_falha_do_ctn():
    # Matrícula desconhecida volta como lista vazia (200); um 404 aí é rota ou chave quebrada.
    ctn = cliente(lambda _r: httpx.Response(404, json={"erro": "x"}))
    with pytest.raises(CTNIndisponivel):
        ctn.historico_pagamentos(MATRICULA, meses=12)
    with pytest.raises(CTNIndisponivel):
        ctn.mensalidades_em_aberto(MATRICULA)


def test_cadastro_com_lista_vazia_e_cliente_nao_encontrado():
    tabelas = tabelas_padrao()
    tabelas["/v1/ctn/filiados_full"] = []
    with pytest.raises(ClienteNaoEncontrado):
        cliente(servidor(tabelas)).consultar_cliente(MATRICULA)


def test_matricula_desconhecida_vem_vazia_no_historico_e_nas_mensalidades():
    ctn = cliente(servidor())
    assert ctn.historico_pagamentos("xx999999999", meses=12) == []
    assert ctn.mensalidades_em_aberto("xx999999999") == []


@pytest.mark.parametrize("status", [400, 404, 401, 403, 429, 500, 502, 504])
def test_erro_fora_de_pessoa_ou_de_infra_e_indisponivel(status):
    ctn = cliente(lambda _r: httpx.Response(status, json={"erro": "x"}))
    with pytest.raises(CTNIndisponivel):
        list(ctn.inadimplentes(DATA_REF))
    with pytest.raises(CTNIndisponivel):
        ctn.baixas_desde(AGORA)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 504])
def test_erro_de_infra_numa_consulta_de_pessoa_e_indisponivel(status):
    ctn = cliente(lambda _r: httpx.Response(status, json={"erro": "x"}))
    with pytest.raises(CTNIndisponivel):
        ctn.consultar_cliente(MATRICULA)


def _erros_de_transporte():
    def levantar(erro):
        def tratar(requisicao):
            raise erro(f"falhou em {requisicao.url} com {CHAVE}", request=requisicao)

        return tratar

    return [
        levantar(httpx.ReadTimeout),
        levantar(httpx.ConnectTimeout),
        levantar(httpx.ConnectError),
        levantar(httpx.RemoteProtocolError),
    ]


@pytest.mark.parametrize("tratar", _erros_de_transporte())
def test_timeout_e_rede_caida_sao_indisponivel_sem_url_nem_chave(tratar):
    with pytest.raises(CTNIndisponivel) as info:
        cliente(tratar).consultar_cliente(MATRICULA)
    _confere_erro_limpo(info.value)


def test_resposta_que_nao_e_json_e_indisponivel_sem_o_corpo():
    ctn = cliente(lambda _r: httpx.Response(200, text=f"<html>{CHAVE} {URL}</html>"))
    with pytest.raises(CTNIndisponivel) as info:
        ctn.consultar_cliente(MATRICULA)
    _confere_erro_limpo(info.value)


def test_corpo_sem_dados_e_indisponivel():
    ctn = cliente(lambda _r: httpx.Response(200, json={"uso": {}}))
    with pytest.raises(CTNIndisponivel, match="formato"):
        ctn.consultar_cliente(MATRICULA)


@pytest.mark.parametrize("status", [401, 404, 429, 500, 504])
def test_a_mensagem_de_erro_http_nao_leva_url_chave_nem_corpo(status):
    ctn = cliente(lambda _r: httpx.Response(status, json={"erro": f"{CHAVE} {URL}"}))
    with pytest.raises(Exception) as info:
        list(ctn.inadimplentes(DATA_REF))
    _confere_erro_limpo(info.value)


def _confere_erro_limpo(erro: BaseException):
    texto = f"{erro} {erro!r} {erro.args}"
    assert CHAVE not in texto
    assert URL not in texto
    assert "exemplo-inventado" not in texto
    assert "/v1/" not in texto
    # O erro de origem (que guarda a requisição, com a URL) não vai junto.
    assert erro.__cause__ is None
    assert erro.__context__ is None


# ---- limite de taxa ----


class RelogioFalso:
    def __init__(self):
        self.agora = 1000.0
        self.dormiu: list[float] = []

    def __call__(self) -> float:
        return self.agora

    def dormir(self, segundos: float) -> None:
        self.dormiu.append(segundos)
        self.agora += segundos


def test_o_limite_por_minuto_faz_esperar_a_janela_passar():
    relogio = RelogioFalso()
    chamadas = []
    ctn = cliente(
        servidor(chamadas=chamadas),
        limite_minuto=3,
        relogio=relogio,
        dormir=relogio.dormir,
    )
    for _ in range(3):
        ctn.consultar_cliente(MATRICULA)
    assert relogio.dormiu == []
    ctn.consultar_cliente(MATRICULA)
    assert len(relogio.dormiu) == 1
    assert relogio.dormiu[0] == pytest.approx(60)
    assert len(chamadas) == 4


def test_o_limite_conta_so_a_ultima_janela_de_60_segundos():
    relogio = RelogioFalso()
    ctn = cliente(servidor(), limite_minuto=2, relogio=relogio, dormir=relogio.dormir)
    ctn.consultar_cliente(MATRICULA)
    relogio.agora += 30
    ctn.consultar_cliente(MATRICULA)
    relogio.agora += 31  # a 1ª chamada saiu da janela
    ctn.consultar_cliente(MATRICULA)
    assert relogio.dormiu == []


def test_o_padrao_e_55_chamadas_por_minuto():
    relogio = RelogioFalso()
    ctn = cliente(servidor(), relogio=relogio, dormir=relogio.dormir)
    for _ in range(55):
        ctn.consultar_cliente(MATRICULA)
    assert relogio.dormiu == []
    ctn.consultar_cliente(MATRICULA)
    assert len(relogio.dormiu) == 1


def test_cada_pagina_conta_para_o_limite():
    tabelas = {
        "/v1/ctn/inadimplencia": [
            _inad(f"xx{n:09d}", f"Nome {n}", "(83) 91234 5678", 1, 5, "2026-10-01", "2026-11-06")
            for n in range(4)
        ]
    }
    relogio = RelogioFalso()
    ctn = cliente(
        servidor(tabelas),
        limite_minuto=1,
        linhas_por_pagina=2,
        relogio=relogio,
        dormir=relogio.dormir,
    )
    list(ctn.inadimplentes(DATA_REF))
    assert len(relogio.dormiu) == 1


def test_a_56a_chamada_no_mesmo_minuto_dorme_o_necessario_e_segue():
    # O prazo de 12 s é da requisição, não da espera pela vaga: um laço de mais de 55 chamadas
    # num minuto (retrato paginado, uma consulta por cliente) dorme e termina.
    relogio = RelogioFalso()
    chamadas = []
    ctn = cliente(servidor(chamadas=chamadas), relogio=relogio, dormir=relogio.dormir)
    for _ in range(56):
        assert ctn.consultar_cliente(MATRICULA).nome == "Cliente Inventado"
    assert relogio.dormiu == [pytest.approx(60)]
    assert len(chamadas) == 56


def test_a_espera_que_cabe_na_janela_dorme_so_o_necessario():
    relogio = RelogioFalso()
    ctn = cliente(servidor(), limite_minuto=1, relogio=relogio, dormir=relogio.dormir)
    ctn.consultar_cliente(MATRICULA)
    relogio.agora += 55  # a janela fecha em 5 s
    ctn.consultar_cliente(MATRICULA)
    assert relogio.dormiu == [pytest.approx(5)]


def test_a_espera_que_passa_do_teto_proprio_levanta_sem_dormir():
    # O teto da espera é a janela de 60 s mais o prazo (12 s). Passar dele só acontece quando
    # outras threads encheram a janela depois de a chamada começar; aqui a vaga mais antiga
    # é 20 s mais nova que a chamada, então a espera (80 s) estoura o teto (72 s).
    relogio = RelogioFalso()
    chamadas = []
    ctn = cliente(
        servidor(chamadas=chamadas), limite_minuto=1, relogio=relogio, dormir=relogio.dormir
    )
    ctn._chamadas.append(relogio.agora + 20)
    with pytest.raises(CTNIndisponivel, match="prazo") as info:
        ctn.consultar_cliente(MATRICULA)
    assert relogio.dormiu == []
    assert chamadas == []
    _confere_erro_limpo(info.value)


def test_o_prazo_da_requisicao_vale_depois_da_vaga():
    relogio = RelogioFalso()

    def lento(_requisicao):
        relogio.agora += 20  # passa dos 12 s do prazo
        return servidor()(_requisicao)

    ctn = cliente(lento, relogio=relogio, dormir=relogio.dormir)
    with pytest.raises(CTNIndisponivel, match="prazo") as info:
        ctn.consultar_cliente(MATRICULA)
    _confere_erro_limpo(info.value)


def test_limite_por_minuto_invalido_e_recusado():
    with pytest.raises(ValueError, match="limite"):
        CTNEficaz(URL, CHAVE, UNIDADE, limite_minuto=0)


# ---- configuração ----


def test_da_config_le_url_chave_e_unidade():
    chamadas = []
    config = SimpleNamespace(ctn_url=URL, ctn_api_key=CHAVE, ctn_unidade=UNIDADE)
    ctn = CTNEficaz.da_config(config, transporte=httpx.MockTransport(servidor(chamadas=chamadas)))
    assert ctn.consultar_cliente(MATRICULA).nome == "Cliente Inventado"
    assert str(chamadas[0].url).startswith(URL + "/v1/ctn/filiados_full")


def test_da_config_sem_a_chave_e_recusada_sem_ecoar_nada():
    config = SimpleNamespace(ctn_url=URL, ctn_api_key=None, ctn_unidade=UNIDADE)
    with pytest.raises(ValueError, match="AGENTE_CTN_API_KEY"):
        CTNEficaz.da_config(config)


def test_o_cliente_http_pode_ser_fechado():
    with cliente(servidor()) as ctn:
        ctn.consultar_cliente(MATRICULA)
    with pytest.raises(RuntimeError):
        ctn._cliente.get("/")
