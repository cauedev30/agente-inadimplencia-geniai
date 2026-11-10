"""O ensaio em sombra do CTN real (PARADA 3): contra um servidor de mentira, sem rede.

O dado é inventado, com valores sentinela no telefone, no nome, no id da baixa e na chave: a saída
nunca pode conter nenhum deles, e nada é enviado a lugar nenhum.
"""

import importlib.util
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from agente.db.modelos import Cobranca
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootFalso, ChatwootHttp
from agente.tipos import StatusCobranca
from tests.integracoes.test_ctn_eficaz import CHAVE, UNIDADE, URL, _bx, _inad, _lanc, servidor

from ..conftest import RAIZ

_CAMINHO = RAIZ / "ferramentas" / "ctn" / "ensaio_sombra.py"
_spec = importlib.util.spec_from_file_location("ctn_ensaio_sombra", _CAMINHO)
ensaio = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ensaio
_spec.loader.exec_module(ensaio)

AGORA = datetime(2026, 11, 10, 15, tzinfo=UTC)
HOJE = "2026-11-10"
POLITICA = RAIZ / "politica.toml"

TELEFONES = ("(83) 97531 8642", "(83) 98642 1357", "(83) 99123 4680", "(83) 98765 4321")
PROIBIDOS = (
    "97531",
    "8642",
    "98642",
    "1357",
    "Sentinela",
    "bx-SENT",
    "xx00000",
    "yy00000",
    CHAVE,
    "3340",
    "33.4",
    "33,4",
)


def _iso(instante: datetime) -> str:
    return instante.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _baixa(id_, matricula, minutos_atras):
    coletado = _iso(AGORA - timedelta(minutes=minutos_atras))
    return _bx(id_, matricula, "PIX", "2026/10", HOJE, 33.4, coletado)


def _tabelas():
    matriculas = ("xx000000001", "xx000000002", "xx000000003", "xx000000004")
    atrasos = (2, 1, 1, 1)
    retrato = [
        _inad(m, "Fulano Sentinela Zeta", telefone, atraso, 4, "2026-09-01", HOJE)
        for m, telefone, atraso in zip(matriculas, TELEFONES, atrasos, strict=True)
    ]
    # A Energisa: o CTN não conta como atraso.
    retrato.append(_inad("xx000000005", "Outro Sentinela", "(83) 91111 2222", 0, 9, None, HOJE))
    lancamentos = [
        _lanc(1, "2026/09", "rejeitada", data_baixa="2026-09-12", matricula="xx000000001"),
        _lanc(2, "2026/10", "rejeitada", data_baixa="2026-10-12", matricula="xx000000001"),
        _lanc(3, "2026/10", "rejeitada", data_baixa="2026-10-12", matricula="xx000000002"),
        _lanc(4, "2026/10", "rejeitada", data_baixa="2026-10-12", matricula="xx000000003"),
    ]
    baixas = [
        _baixa("bx-SENT-7001", "xx000000001", 5),  # paga um dos dois meses: abate
        _baixa("bx-SENT-7002", "xx000000002", 15),  # paga o único mês: quita
        _baixa("bx-SENT-7003", "yy000000009", 25),  # quem não está na carteira
    ]
    return {
        "/v1/ctn/inadimplencia": retrato,
        "/v1/ctn/lancamentos": lancamentos,
        "/v1/ctn/lancamentos_bx": baixas,
    }


@pytest.fixture(scope="session")
def modelo_migrado(tmp_path_factory):
    """O banco migrado uma vez só; cada teste usa uma cópia (a migração leva alguns segundos)."""
    caminho = tmp_path_factory.mktemp("modelo") / "modelo.db"
    ensaio.migrar(f"sqlite:///{caminho.as_posix()}")
    return caminho


@pytest.fixture
def banco(tmp_path, modelo_migrado):
    pasta = tmp_path / "pre-sombra" / "parada3"
    pasta.mkdir(parents=True)
    destino = pasta / "ensaio.db"
    shutil.copy(modelo_migrado, destino)
    return destino


def _rodar(banco, tabelas=None, pedidos=None, **extra):
    pedidos = [] if pedidos is None else pedidos
    opcoes = {
        "url": URL,
        "unidade": UNIDADE,
        "politica_path": POLITICA,
        "transporte": httpx.MockTransport(
            servidor(_tabelas() if tabelas is None else tabelas, pedidos)
        ),
        "agora": lambda: AGORA,
    }
    return ensaio.executar(CHAVE, banco, **{**opcoes, **extra})


def _sessao(banco):
    return criar_sessao(f"sqlite:///{banco.as_posix()}")()


# ---- o caminho do banco e do relatório ----


def test_recusa_o_banco_dentro_de_repo_mesmo_com_pre_sombra_no_caminho():
    with pytest.raises(ensaio.ErroDaConferencia, match="repo/"):
        ensaio.validar_caminho(RAIZ / "pre-sombra" / "parada3" / "ensaio.db")
    with pytest.raises(ensaio.ErroDaConferencia, match="repo/"):
        ensaio.validar_caminho(RAIZ / "ensaio.db")


def test_recusa_o_banco_sem_pasta_pre_sombra(tmp_path):
    with pytest.raises(ensaio.ErroDaConferencia, match="pre-sombra"):
        ensaio.validar_caminho(tmp_path / "ensaio.db")


def test_aceita_o_banco_em_pre_sombra_fora_de_repo(tmp_path):
    caminho = tmp_path / "Pre-Sombra" / "parada3" / "ensaio.db"
    assert ensaio.validar_caminho(caminho) == caminho.resolve()


def test_a_recusa_vem_antes_de_criar_qualquer_coisa_e_de_chamar_o_cto(tmp_path):
    pedidos = []
    dentro = RAIZ / "pre-sombra-do-teste" / "ensaio.db"
    with pytest.raises(ensaio.ErroDaConferencia):
        _rodar(dentro, pedidos=pedidos)
    with pytest.raises(ensaio.ErroDaConferencia):
        _rodar(tmp_path / "ensaio.db", pedidos=pedidos)
    assert not dentro.parent.exists()
    assert not (tmp_path / "ensaio.db").exists()
    assert pedidos == []


def test_recusa_o_relatorio_dentro_de_repo(banco):
    destino = RAIZ / "relatorio-do-ensaio.txt"
    with pytest.raises(ensaio.ErroDaConferencia, match="repo/"):
        _rodar(banco, relatorio=destino)
    assert not destino.exists()


def test_a_fase_desconhecida_e_recusada(banco):
    with pytest.raises(ensaio.ErroDaConferencia, match="fase desconhecida"):
        _rodar(banco, fases=["importar", "inventada"])


def test_a_linha_de_comando_recusa_o_banco_ruim_antes_de_ler_a_chave(capsys):
    codigo = ensaio.main(
        ["--banco", str(RAIZ / "ensaio.db"), "--segredos", str(RAIZ / "nao-existe")]
    )

    assert codigo == 2
    assert "repo/" in capsys.readouterr().err


def test_a_linha_de_comando_passa_os_padroes_e_devolve_o_codigo(monkeypatch, banco, capsys):
    monkeypatch.setenv("AGENTE_CTN_API_KEY", CHAVE)
    recebido = {}

    def falso(chave, caminho, **opcoes):
        recebido.update(opcoes, chave=chave, caminho=caminho)
        return ensaio.Resultado("RELATORIO", 1, ensaio.ChatwootSemEnvio(), None)

    monkeypatch.setattr(ensaio, "executar", falso)

    assert ensaio.main(["--banco", str(banco)]) == 1

    assert capsys.readouterr().out.strip().endswith("RELATORIO")
    assert recebido["chave"] == CHAVE
    assert tuple(recebido["fases"]) == ("importar", "conferir", "baixas")
    assert (recebido["conferir"], recebido["ciclos"], recebido["intervalo_min"]) == (5, 6, 10.0)
    assert (recebido["teto_linhas"], recebido["teto_chamadas"]) == (5000, 400)
    assert recebido["campanha"] == "ctn"


# ---- as fases ----


def test_o_fluxo_padrao_importa_confere_e_le_as_baixas_nessa_ordem(banco):
    resultado = _rodar(banco)
    texto = resultado.texto

    assert resultado.codigo == 0
    assert (
        texto.index("== importar ==") < texto.index("== conferir ==") < texto.index("== baixas ==")
    )
    assert "criadas: 4" in texto and "ignoradas Energisa: 1" in texto
    assert "custo esperado: ~58 chamadas, ~2900 linhas" in texto
    assert "resultado: conferida=2, quitada=1, sem_lancamentos=1" in texto
    assert "meses em aberto no CTN contra o retrato: iguais=1, menores=2" in texto
    # A conferência já absorveu a baixa da matrícula 001; a 002 ainda quita; a 009 não tem cobrança.
    assert "efeitos das novas: quitou_sem_conversa=1, sem_cobranca=1, sem_mudanca=1" in texto
    assert "RESULTADO: concluído" in texto


def test_a_fase_baixas_mostra_a_baixa_virando_quitou_e_abateu_na_cobranca_certa(banco):
    resultado = _rodar(banco, fases=["importar", "baixas"])
    texto = resultado.texto

    assert (
        "baixas novas gravadas: 3 (casadas com cobrança do banco local: 2; sem cobrança: 1)"
        in texto
    )
    assert "efeitos das novas: abateu=1, quitou_sem_conversa=1, sem_cobranca=1" in texto
    assert (
        "***002 -> quitou_sem_conversa -> cobrança PAGA -> motivo baixa_externa;"
        " meses em atraso: retrato 1, agora 1" in texto
    )
    assert (
        "***001 -> abateu -> cobrança ATIVA -> motivo -; meses em atraso: retrato 2, agora 1"
        in texto
    )
    with _sessao(banco) as sessao:
        status = {c.cliente.matricula_ctn: c.status for c in sessao.scalars(select(Cobranca)).all()}
    assert status["xx000000002"] is StatusCobranca.PAGA
    assert status["xx000000001"] is StatusCobranca.ATIVA


def test_a_conferencia_sem_cobranca_ativa_pede_o_importar(banco):
    resultado = _rodar(banco, fases=["conferir"])

    assert "nenhuma cobrança ATIVA no banco" in resultado.texto
    assert resultado.codigo == 1


def test_o_relatorio_em_arquivo_tem_o_mesmo_texto_e_fica_fora_de_repo(banco, tmp_path):
    destino = tmp_path / "saida" / "relatorio.txt"

    resultado = _rodar(banco, relatorio=destino)

    assert destino.read_text(encoding="utf-8") == resultado.texto + "\n"


def test_a_saida_traz_so_contagens_e_formatos_e_nenhum_dado_pessoal(banco, tmp_path):
    destino = tmp_path / "relatorio.txt"
    mensagens = []

    resultado = _rodar(
        banco,
        relatorio=destino,
        progresso=mensagens.append,
        fases=["importar", "conferir", "baixas"],
    )
    tudo = resultado.texto + destino.read_text(encoding="utf-8") + "\n".join(mensagens)

    for proibido in PROIBIDOS:
        assert proibido not in tudo
    # Nem telefone, nem CPF, nem id: nenhuma sequência longa de dígitos.
    assert not re.search(r"\d{7,}", tudo.replace(str(banco), ""))
    assert "***001" in tudo and "***002" in tudo


# ---- nada é enviado ----


def test_o_chatwoot_do_ensaio_nao_tem_rede_e_o_aviso_vira_contador():
    chatwoot = ensaio.ChatwootSemEnvio()
    assert isinstance(chatwoot, ChatwootFalso) and not isinstance(chatwoot, ChatwootHttp)

    chatwoot.enviar_mensagem(1, "aviso")
    chatwoot.nota_privada(1, "nota")

    assert (chatwoot.avisos, chatwoot.notas) == (1, 1)


def test_nada_e_enviado_so_o_ctn_e_lido_por_get(banco, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("o ensaio não pode montar o Chatwoot HTTP")

    monkeypatch.setattr(ChatwootHttp, "__init__", proibido)
    pedidos = []

    resultado = _rodar(banco, pedidos=pedidos)

    assert pedidos
    assert {p.method for p in pedidos} == {"GET"}
    assert {p.url.host for p in pedidos} == {httpx.URL(URL).host}
    assert isinstance(resultado.chatwoot, ChatwootFalso)
    assert resultado.chatwoot.chamadas == []
    assert "avisos de baixa que sairiam: 0; notas ao atendente: 0" in resultado.texto


# ---- a cota ----


@pytest.fixture
def custo_pequeno(monkeypatch):
    """Faz o custo esperado do retrato caber nos tetos pequenos dos testes."""
    monkeypatch.setattr(ensaio, "CHAMADAS_ESPERADAS_DO_RETRATO", 0)
    monkeypatch.setattr(ensaio, "LINHAS_ESPERADAS_DO_RETRATO", 0)


@pytest.mark.parametrize(
    ("teto", "mensagem", "chamadas"),
    [
        ({"teto_linhas": 6}, "teto de 6 linhas atingido (7 lidas)", 2),
        ({"teto_chamadas": 2}, "teto de 2 chamadas atingido", 2),
    ],
)
def test_o_teto_aborta_a_fase_sem_seguir_chamando_e_o_relatorio_sai_parcial(
    banco, custo_pequeno, teto, mensagem, chamadas
):
    pedidos = []

    resultado = _rodar(banco, pedidos=pedidos, **teto)
    texto = resultado.texto

    assert resultado.codigo == 1
    assert f"RESULTADO: ABORTADO por cota ({mensagem}); relatório parcial" in texto
    assert f"ABORTADA: {mensagem}" in texto
    assert "== baixas ==\n  NÃO RODADA" in texto
    assert "criadas: 4" in texto
    # O retrato (1 chamada) e os lançamentos (1): a chamada seguinte foi recusada antes de sair.
    assert len(pedidos) == resultado.medidor.chamadas == chamadas


def test_o_teto_estourado_no_retrato_nao_grava_nada(banco, custo_pequeno):
    pedidos = []

    resultado = _rodar(banco, pedidos=pedidos, teto_chamadas=0, fases=["importar", "conferir"])

    assert pedidos == []
    assert (
        "ABORTADA: teto de 0 chamadas atingido; nada foi gravado nem encerrado." in resultado.texto
    )
    assert "== conferir ==\n  NÃO RODADA" in resultado.texto
    with _sessao(banco) as sessao:
        assert sessao.scalars(select(Cobranca)).all() == []


def test_o_importar_nem_comeca_se_a_cota_restante_e_menor_que_o_custo_esperado(banco):
    pedidos = []

    resultado = _rodar(banco, pedidos=pedidos, teto_chamadas=57, fases=["importar"])

    assert pedidos == []
    assert "NÃO INICIADA: restam 57 chamadas e 5000 linhas de cota" in resultado.texto
    assert resultado.codigo == 1


def test_o_relatorio_mostra_a_cota_gasta(banco):
    resultado = _rodar(banco)

    assert f"chamadas: {resultado.medidor.chamadas} de no máximo 400" in resultado.texto
    assert "linhas devolvidas:" in resultado.texto and "de no máximo 5000" in resultado.texto
    assert "`uso` da API (último corpo lido): chamadas_hoje=" in resultado.texto


# ---- o atraso das baixas ----


def test_as_estatisticas_do_atraso():
    assert ensaio.estatisticas_do_atraso([]) is None
    assert ensaio.estatisticas_do_atraso([12, 6, 8]) == {
        "n": 3,
        "min": 6,
        "mediana": 8,
        "p90": 12,
        "max": 12,
    }
    # p90 pelo posto mais próximo: o 9º de 10.
    assert ensaio.estatisticas_do_atraso(list(range(1, 11)))["p90"] == 9
    assert ensaio.formatar_atraso([]) == "nenhuma baixa nova"


def test_o_atraso_e_o_instante_do_ciclo_menos_o_coletado_em_com_o_relogio_injetado(banco):
    relogio = [AGORA]
    dormidas = []
    base = "/v1/ctn/lancamentos_bx"
    tabelas = {base: [_baixa("bx-SENT-0", "zz000000001", 120)]}
    # O que o CTN passa a mostrar a cada espera: baixas coletadas 6, 8 e 12 minutos antes do ciclo.
    chegadas = [4, 12, 18]

    def dormir(segundos):
        dormidas.append(segundos)
        relogio[0] += timedelta(seconds=segundos)
        minutos = chegadas[len(dormidas) - 1]
        coletado = _iso(AGORA + timedelta(minutes=minutos))
        linha = _bx(
            f"bx-SENT-{len(dormidas)}", "zz000000001", "PIX", "2026/10", HOJE, 33.4, coletado
        )
        tabelas[base].append(linha)

    resultado = _rodar(
        banco,
        tabelas=tabelas,
        fases=["ciclos"],
        ciclos=4,
        intervalo_min=10,
        agora=lambda: relogio[0],
        dormir=dormir,
    )
    texto = resultado.texto

    assert dormidas == [600.0, 600.0, 600.0]
    assert "ciclos feitos: 4 de 4, a cada 10 min" in texto
    assert (
        "ciclos 2 em diante (decide a MARGEM_DE_RELEITURA, hoje 0 min):"
        " n=3, min=6.0, mediana=8.0, p90=12.0, max=12.0 (minutos)" in texto
    )
    assert (
        "ciclo 1 (carga inicial; NÃO entra na decisão):"
        " n=1, min=120.0, mediana=120.0, p90=120.0, max=120.0 (minutos)" in texto
    )
    assert "ciclo 1/4 (carga inicial): novas=1" in texto
    assert "ciclo 4/4: novas=1" in texto
    assert "efeitos de todas as novas: sem_cobranca=4" in texto
    assert resultado.codigo == 0


def test_o_ciclo_que_estoura_a_cota_para_e_nao_dorme_de_novo(banco):
    dormidas = []
    tabelas = {"/v1/ctn/lancamentos_bx": [_baixa("bx-SENT-0", "zz000000001", 30)]}

    resultado = _rodar(
        banco,
        tabelas=tabelas,
        fases=["ciclos"],
        ciclos=5,
        teto_chamadas=1,
        dormir=dormidas.append,
    )

    # O ciclo 1 usa a única chamada; no 2º a leitura é recusada e o ciclo vê o teto.
    assert "ABORTADA: teto de 1 chamadas atingido" in resultado.texto
    assert "ciclos feitos: 2 de 5" in resultado.texto
    assert len(dormidas) == 1
    assert resultado.codigo == 1
