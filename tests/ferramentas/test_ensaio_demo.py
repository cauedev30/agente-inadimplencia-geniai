"""O ensaio automático da demo: roteiros, kit, conferência e relatório, sem rede externa nem Jev.

O serviço de verdade não sobe aqui: um serviço de mentira, em 127.0.0.1, grava no banco o que o
webhook do agente gravaria, para o motor rodar um roteiro inteiro.
"""

import importlib.util
import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agente.api.chatwoot_webhook import ler_evento
from agente.db.modelos import (
    Base,
    Cliente,
    Cobranca,
    Conversa,
    LaudoGravado,
    Mensagem,
    Turno,
)
from agente.db.sessao import criar_sessao
from agente.integracoes.chatwoot import ChatwootHttp
from agente.politica import Regua, carregar_politica
from agente.tipos import (
    BatchJev,
    EstadoConversa,
    Modo,
    MotivoHumano,
    OrigemMensagem,
    ResultadoTurno,
)

from ..conftest import RAIZ

_PASTA = RAIZ / "ferramentas" / "demo-whatsapp"
_spec = importlib.util.spec_from_file_location("demo_whatsapp_ensaiar", _PASTA / "ensaiar.py")
ensaiar = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ensaiar
_spec.loader.exec_module(ensaiar)

Veredito = ensaiar.Veredito


# --------------------------------------------------------------------------- roteiros


def _yaml(tmp_path, texto):
    caminho = tmp_path / "roteiros.yaml"
    caminho.write_text(texto, encoding="utf-8")
    return caminho


def test_um_yaml_valido_carrega_com_os_padroes(tmp_path):
    caminho = _yaml(
        tmp_path,
        """
- nome: divida
  passos:
    - cliente: "Quanto eu estou devendo?"
      espera:
        acao: INFORM_DEBT
        resultado: ENVIADO
        texto_tem: ["100,20"]
""",
    )
    [roteiro] = ensaiar.carregar_roteiros(caminho)
    assert roteiro.nome == "divida"
    assert roteiro.regua == Regua.R3
    assert roteiro.mensalidades == 3
    assert roteiro.equipe is False
    assert roteiro.fim is None
    passo = roteiro.passos[0]
    assert passo.espera.acao == "INFORM_DEBT"
    assert passo.espera.resultado == ResultadoTurno.ENVIADO
    assert passo.espera.texto_nao_tem == []
    assert passo.a_decidir is False


def test_o_rotulo_do_humano_e_o_do_reinicio_sao_acoes_validas(tmp_path):
    caminho = _yaml(
        tmp_path,
        """
- nome: a
  passos:
    - cliente: "x"
      espera: {acao: "HUMAN:DESCONFIANCA"}
    - cliente: "y"
      espera: {acao: "NAO_EXECUTADA:INFORM_DEBT"}
    - cliente: "z"
      espera: {acao: REINICIO}
""",
    )
    assert len(ensaiar.carregar_roteiros(caminho)[0].passos) == 3


def test_campo_desconhecido_da_erro_com_o_arquivo_e_o_campo(tmp_path):
    caminho = _yaml(
        tmp_path,
        """
- nome: a
  passos:
    - cliente: "x"
      esperra: {acao: INFORM_DEBT}
""",
    )
    with pytest.raises(ensaiar.RoteiroInvalido) as erro:
        ensaiar.carregar_roteiros(caminho)
    assert "roteiros.yaml" in str(erro.value)
    assert "esperra" in str(erro.value)


def test_acao_invalida_da_erro_com_o_campo(tmp_path):
    caminho = _yaml(
        tmp_path,
        """
- nome: a
  passos:
    - cliente: "x"
      espera: {acao: HUMAN:NAO_EXISTE}
""",
    )
    with pytest.raises(ensaiar.RoteiroInvalido, match=r"espera\.acao.*HUMAN:NAO_EXISTE"):
        ensaiar.carregar_roteiros(caminho)


def test_lista_sem_passos_da_erro_com_o_campo(tmp_path):
    caminho = _yaml(tmp_path, "- nome: a\n  passos: []\n")
    with pytest.raises(ensaiar.RoteiroInvalido, match="passos"):
        ensaiar.carregar_roteiros(caminho)


def test_fim_vazio_e_nome_repetido_sao_recusados(tmp_path):
    vazio = _yaml(tmp_path, '- nome: a\n  passos: [{cliente: "x"}]\n  fim: {}\n')
    with pytest.raises(ensaiar.RoteiroInvalido, match="fim"):
        ensaiar.carregar_roteiros(vazio)
    repetido = _yaml(
        tmp_path, '- {nome: a, passos: [{cliente: "x"}]}\n- {nome: a, passos: [{cliente: "y"}]}\n'
    )
    with pytest.raises(ensaiar.RoteiroInvalido, match="repetido"):
        ensaiar.carregar_roteiros(repetido)


def test_arquivo_que_nao_e_lista_ou_nao_existe_da_erro(tmp_path):
    with pytest.raises(ensaiar.RoteiroInvalido, match="lista"):
        ensaiar.carregar_roteiros(_yaml(tmp_path, "nome: a\n"))
    with pytest.raises(ensaiar.RoteiroInvalido, match="falta"):
        ensaiar.carregar_roteiros(tmp_path / "nao-existe.yaml")


def test_o_demo_yaml_real_carrega_com_os_doze_roteiros():
    roteiros = ensaiar.carregar_roteiros(_PASTA / "roteiros" / "demo.yaml")
    assert [r.nome for r in roteiros] == [
        "divida",
        "desconto",
        "aceite-condicional",
        "golpe",
        "enrolacao",
        "contestacao",
        "ja-paguei",
        "fora-da-politica",
        "irritado",
        "fora-do-assunto",
        "cumprimento-solto",
        "reset",
    ]
    reset = roteiros[-1]
    assert reset.equipe is True
    assert reset.passos[1].cliente == "/reset"
    assert reset.passos[2].intervalo == 3
    assert roteiros[3].passos[0].espera.etiqueta == "desconfianca"
    assert roteiros[4].fim.motivo == MotivoHumano.TETO


# --------------------------------------------------------------------------- kit


def _roteiro(nome="a", **campos):
    return ensaiar.Roteiro(nome=nome, passos=[ensaiar.Passo(cliente="oi")], **campos)


POLITICA_BASE = """\
mensalidade_centavos = 3340
# O piloto: liberacao = { modo = "piloto", telefones = ["+55..."] }
telefones_equipe = ["+558387468188"]  # DEMO
liberacao = { modo = "piloto", telefones = ["+558387468188"] }  # DEMO: só o celular do Cauê
"""


def test_a_politica_do_ensaio_troca_as_duas_linhas_e_deixa_o_resto():
    nova = ensaiar.politica_do_ensaio(POLITICA_BASE, ["+5583900000001", "+5583900000002"])
    assert "+558387468188" not in nova.replace('telefones = ["+55..."]', "")
    assert 'telefones_equipe = ["+5583900000001", "+5583900000002"]' in nova
    assert (
        'liberacao = { modo = "piloto", telefones = ["+5583900000001", "+5583900000002"] }' in nova
    )
    assert "mensalidade_centavos = 3340" in nova
    # O comentário de exemplo não é trocado.
    assert '# O piloto: liberacao = { modo = "piloto", telefones = ["+55..."] }' in nova


@pytest.mark.parametrize("linha", ["telefones_equipe", "liberacao"])
def test_a_politica_sem_a_linha_ou_com_ela_duas_vezes_e_recusada(linha):
    sem = "\n".join(ln for ln in POLITICA_BASE.splitlines() if not ln.startswith(f"{linha} ="))
    with pytest.raises(ensaiar.ErroDaDemo, match=linha):
        ensaiar.politica_do_ensaio(sem, ["+5583900000001"])
    duas = POLITICA_BASE + f"{linha} = []\n"
    with pytest.raises(ensaiar.ErroDaDemo, match=linha):
        ensaiar.politica_do_ensaio(duas, ["+5583900000001"])


def test_a_carteira_tem_um_devedor_por_roteiro_no_formato_da_demo():
    csv = ensaiar.carteira_do_ensaio([_roteiro("a"), _roteiro("b", regua=Regua.R6, mensalidades=7)])
    assert csv.splitlines() == [
        "matricula;nome;telefone;mensalidades_abertas;regua",
        "ENS-0001;Ana Ensaio;+5583900000001;3;R3",
        "ENS-0002;Bruno Ensaio;+5583900000002;7;R6",
    ]


def test_montar_kit_grava_a_politica_e_a_carteira_e_devolve_os_telefones(tmp_path):
    politica = RAIZ / "politica.toml"
    texto = politica.read_text(encoding="utf-8")
    kit_demo = tmp_path / "kit"
    kit_demo.mkdir()
    (kit_demo / "politica-demo.toml").write_text(texto, encoding="utf-8")
    saida = tmp_path / "ensaio"

    telefones = ensaiar.montar_kit(kit_demo, saida, [_roteiro("a"), _roteiro("b")])

    assert telefones == {"a": "+5583900000001", "b": "+5583900000002"}
    carregada = carregar_politica(saida / "politica-demo.toml")
    assert carregada.telefones_equipe == {"+5583900000001", "+5583900000002"}
    assert carregada.liberacao.modo == "piloto"
    assert set(carregada.liberacao.telefones) == {"+5583900000001", "+5583900000002"}
    assert (saida / "carteira-ensaio.csv").read_text(encoding="utf-8").count("ENS-") == 2


def test_montar_kit_sem_a_politica_do_kit_falha_dizendo_o_que_falta(tmp_path):
    with pytest.raises(ensaiar.ErroDaDemo, match="politica-demo.toml"):
        ensaiar.montar_kit(tmp_path, tmp_path / "ensaio", [_roteiro()])


# --------------------------------------------------------------------------- evento e envio


def test_o_evento_do_cliente_passa_pelo_ler_evento_de_verdade():
    evento = ensaiar.evento_do_cliente(100001, 9001, "+5583900000001", "Oi", 91, 280)
    lido = ler_evento(evento)
    assert lido is not None
    assert lido.mensagem_id == "100001"
    assert lido.conversa_chatwoot_id == 9001
    assert (lido.conta_id, lido.inbox_id) == (91, 280)
    assert (lido.tipo, lido.privada, lido.remetente) == ("incoming", False, "contact")
    assert lido.telefone == "+5583900000001"
    assert lido.texto == "Oi"


class _ServidorQualquer:
    """Um servidor HTTP local que conta os POSTs e responde com o código dado.

    Com uma lista, cada POST responde o próximo código dela, na ordem de chegada.
    """

    def __init__(self, codigo=200):
        self.codigos = list(codigo) if isinstance(codigo, list) else None
        self.codigo = codigo
        self.trava = threading.Lock()
        self.corpos: list[dict] = []
        self.caminhos: list[str] = []
        dono = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                tamanho = int(self.headers.get("Content-Length") or 0)
                dono.corpos.append(json.loads(self.rfile.read(tamanho)))
                dono.caminhos.append(self.path)
                with dono.trava:
                    codigo = dono.codigos.pop(0) if dono.codigos is not None else dono.codigo
                self.send_response(codigo)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.servidor.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}/webhooks/chatwoot/segredo"

    def parar(self):
        self.servidor.shutdown()
        self.servidor.server_close()


def test_postar_manda_o_mesmo_evento_duas_vezes_e_exige_200():
    servidor = _ServidorQualquer()
    try:
        evento = ensaiar.evento_do_cliente(1, 9001, "+5583900000001", "Oi", 91, 280)
        ensaiar.postar(servidor.url, evento)
        assert servidor.corpos == [evento, evento]
    finally:
        servidor.parar()


def test_postar_uma_vez_so_quando_nao_e_em_dobro():
    servidor = _ServidorQualquer()
    try:
        ensaiar.postar(servidor.url, {"event": "x"}, em_dobro=False)
        assert len(servidor.corpos) == 1
    finally:
        servidor.parar()


def test_postar_com_um_500_e_um_200_segue_e_devolve_os_codigos():
    # A 1ª mensagem de uma conversa nova chega em dobro e uma das entregas pode perder a corrida
    # (A8): a mensagem foi gravada pela outra, então o ensaio segue e o passo ganha um aviso.
    servidor = _ServidorQualquer(codigo=[500, 200])
    try:
        assert sorted(ensaiar.postar(servidor.url, {"event": "x"})) == [200, 500]
    finally:
        servidor.parar()


def test_postar_devolve_os_dois_200():
    servidor = _ServidorQualquer()
    try:
        assert ensaiar.postar(servidor.url, {"event": "x"}) == [200, 200]
    finally:
        servidor.parar()


def test_postar_sem_nenhum_200_falha_sem_citar_o_segredo():
    servidor = _ServidorQualquer(codigo=500)
    try:
        with pytest.raises(ensaiar.ErroDoEnsaio, match="500") as erro:
            ensaiar.postar(servidor.url, {"event": "x"})
        assert "segredo" not in str(erro.value)
    finally:
        servidor.parar()


def test_postar_sem_servico_falha_sem_citar_o_endereco():
    servidor = _ServidorQualquer()
    url = servidor.url
    servidor.parar()
    with pytest.raises(ensaiar.ErroDoEnsaio) as erro:
        ensaiar.postar(url, {"event": "x"}, em_dobro=False)
    assert "segredo" not in str(erro.value)
    assert "127.0.0.1" not in str(erro.value)


# --------------------------------------------------------------------------- conferência


def _obtido(**campos):
    base = {
        "acao_principal": "INFORM_DEBT",
        "acoes_anexadas": [],
        "resultado": "ENVIADO",
        "texto_enviado": "Você deve R$ 100,20. Pix: 00020126abc",
        "jev2_acao": "INFORM_DEBT",
        "jev2_p": 0.93,
        "jev3_reprovadas": [[]],
        "estado": "ENGAJADO",
        "motivo": None,
        "segundos": 11.0,
    }
    return ensaiar.Obtido(**{**base, **campos})


def _registro(tipo, conteudo, conversa=9001):
    return ensaiar.chatwoot_falso.Registro(conversa, tipo, conteudo, 0.0)


ESPERA_DIVIDA = ensaiar.Espera(
    acao="INFORM_DEBT", resultado="ENVIADO", texto_tem=["100,20", "00020126"]
)


def test_conferir_ok_quando_tudo_bate():
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido()) == Veredito.OK


def test_conferir_sem_expectativa_e_ok_se_o_turno_fechou():
    assert ensaiar.conferir(None, False, _obtido(acao_principal="NONE")) == Veredito.OK


def test_conferir_diverge_pela_acao_pelo_resultado_e_pelo_texto():
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido(acao_principal="NONE")) == (
        Veredito.DIVERGIU
    )
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido(resultado="SEM_ENVIO")) == (
        Veredito.DIVERGIU
    )
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido(texto_enviado="Oi")) == Veredito.DIVERGIU
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido(texto_enviado=None)) == Veredito.DIVERGIU


def test_conferir_texto_nao_tem_ignora_a_caixa():
    espera = ensaiar.Espera(texto_nao_tem=["http"])
    assert ensaiar.conferir(espera, False, _obtido(texto_enviado="Veja HTTPS://x")) == (
        Veredito.DIVERGIU
    )
    assert ensaiar.conferir(espera, False, _obtido(texto_enviado="sem link")) == Veredito.OK


def test_conferir_a_etiqueta_vem_dos_registros_do_chatwoot_falso():
    espera = ensaiar.Espera(etiqueta="desconfianca")
    com = _obtido(registros_chatwoot=[_registro("etiquetas", ["Desconfianca"])])
    sem = _obtido(registros_chatwoot=[_registro("nota", "x")])
    assert ensaiar.conferir(espera, False, com) == Veredito.OK
    assert ensaiar.conferir(espera, False, sem) == Veredito.DIVERGIU


def test_conferir_sem_turno_quando_nao_ha_obtido():
    assert ensaiar.conferir(ESPERA_DIVIDA, False, None) == Veredito.SEM_TURNO
    assert ensaiar.conferir(None, True, None) == Veredito.SEM_TURNO


def test_conferir_erro_pelo_resultado_ou_pelo_log_vale_mais_que_a_decidir():
    assert ensaiar.conferir(ESPERA_DIVIDA, False, _obtido(resultado="ERRO")) == Veredito.ERRO
    assert ensaiar.conferir(None, True, _obtido(erros_no_log=["ERROR boom"])) == Veredito.ERRO


def test_conferir_a_decidir_nao_da_veredito_mesmo_que_diverja():
    assert ensaiar.conferir(ESPERA_DIVIDA, True, _obtido(acao_principal="NONE")) == (
        Veredito.A_DECIDIR
    )


def test_as_divergencias_dizem_o_esperado_e_o_obtido():
    achadas = ensaiar.divergencias(ESPERA_DIVIDA, _obtido(acao_principal="NONE", texto_enviado="x"))
    assert any("INFORM_DEBT" in a and "NONE" in a for a in achadas)
    assert any("100,20" in a for a in achadas)


def test_o_fim_confere_o_estado_e_o_motivo():
    fim = ensaiar.Fim(motivo=MotivoHumano.TETO)
    assert ensaiar.divergencias_do_fim(fim, "ATENDIMENTO_HUMANO", "TETO") == []
    assert ensaiar.divergencias_do_fim(fim, "ATENDIMENTO_HUMANO", "TOM")
    fim = ensaiar.Fim(estado=EstadoConversa.PAGO)
    assert ensaiar.divergencias_do_fim(fim, "ENGAJADO", None)


# --------------------------------------------------------------------------- banco


@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "demo.db"
    fabrica = criar_sessao(f"sqlite:///{caminho.as_posix()}")
    Base.metadata.create_all(fabrica.kw["bind"])
    return caminho, fabrica


def _cobranca(sessao, numero=1, chatwoot_conversa_id=9001):
    cliente = Cliente(
        matricula_ctn=f"ENS-{numero:04d}",
        nome="Ana Ensaio",
        telefone=f"+5583900000{numero:03d}",
    )
    cobranca = Cobranca(
        cliente=cliente,
        regua=Regua.R3,
        mensalidades_abertas=3,
        divida_centavos=10020,
        propenso_cartao=False,
        campanha="demo",
    )
    conversa = Conversa(cobranca=cobranca, chatwoot_conversa_id=chatwoot_conversa_id)
    sessao.add(conversa)
    sessao.flush()
    return conversa


def _mensagem(sessao, conversa, chatwoot_id, texto="Quanto eu estou devendo?"):
    mensagem = Mensagem(
        conversa_id=conversa.id,
        origem=OrigemMensagem.CLIENTE,
        texto=texto,
        chatwoot_message_id=str(chatwoot_id),
        conta_no_teto=False,
        momento=datetime.now(UTC),
    )
    sessao.add(mensagem)
    sessao.flush()
    return mensagem


def _turno(sessao, conversa, mensagens, resultado=ResultadoTurno.ENVIADO, **campos):
    turno = Turno(
        conversa_id=conversa.id,
        modo=Modo.ATIVO,
        mensagens_ids=[m.id for m in mensagens],
        acao_principal=campos.pop("acao_principal", "INFORM_DEBT"),
        acoes_anexadas=campos.pop("acoes_anexadas", []),
        texto_enviado=campos.pop("texto_enviado", "Você deve R$ 100,20"),
        redacao=campos.pop(
            "redacao",
            {
                "tentativas": [
                    {"texto": "t1", "reprovadas": ["sem_codigo"]},
                    {"texto": "t2", "reprovadas": []},
                ]
            },
        ),
        resultado=resultado,
        **campos,
    )
    sessao.add(turno)
    sessao.flush()
    return turno


def test_esperar_turno_le_o_turno_fechado_com_o_jev_2_e_o_jev_3(banco):
    caminho, fabrica = banco
    with fabrica() as sessao:
        conversa = _cobranca(sessao)
        mensagem = _mensagem(sessao, conversa, 100001)
        turno = _turno(sessao, conversa, [mensagem], acoes_anexadas=["GENERATE_PIX"])
        sessao.add(
            LaudoGravado(
                turno_id=turno.id,
                batch=BatchJev.JEV2,
                origem="jev-real",
                pergunta="acao",
                valor="INFORM_DEBT",
                probabilidade=0.93,
                confianca=0.9,
            )
        )
        conversa.estado = EstadoConversa.ENGAJADO
        sessao.commit()

    obtido = ensaiar.esperar_turno(caminho, "100001", prazo_s=2, intervalo_s=0.05)

    assert obtido.acao_principal == "INFORM_DEBT"
    assert obtido.acoes_anexadas == ["GENERATE_PIX"]
    assert obtido.resultado == "ENVIADO"
    assert obtido.texto_enviado == "Você deve R$ 100,20"
    assert (obtido.jev2_acao, obtido.jev2_p) == ("INFORM_DEBT", 0.93)
    assert obtido.jev3_reprovadas == [["sem_codigo"], []]
    assert (obtido.estado, obtido.motivo) == ("ENGAJADO", None)
    assert obtido.segundos >= 0


def test_esperar_turno_sem_a_mensagem_ou_sem_turno_fechado_volta_none_no_prazo(banco):
    caminho, fabrica = banco
    with fabrica() as sessao:
        conversa = _cobranca(sessao)
        _mensagem(sessao, conversa, 100001)
        sessao.commit()
    # A mensagem existe mas não há turno; e a outra mensagem nem existe.
    assert ensaiar.esperar_turno(caminho, "100001", prazo_s=0.3, intervalo_s=0.05) is None
    assert ensaiar.esperar_turno(caminho, "999", prazo_s=0.3, intervalo_s=0.05) is None


def test_esperar_turno_ignora_o_turno_aberto_e_o_adiado_e_espera_o_seguinte(banco):
    caminho, fabrica = banco
    with fabrica() as sessao:
        conversa = _cobranca(sessao)
        mensagem = _mensagem(sessao, conversa, 100001)
        _turno(sessao, conversa, [mensagem], resultado=None, acao_principal=None)
        _turno(sessao, conversa, [mensagem], resultado=ResultadoTurno.ADIADO)
        sessao.commit()
    assert ensaiar.esperar_turno(caminho, "100001", prazo_s=0.3, intervalo_s=0.05) is None

    with fabrica() as sessao:
        conversa = sessao.query(Conversa).one()
        mensagem = sessao.query(Mensagem).one()
        _turno(sessao, conversa, [mensagem], acao_principal="OFFER_DISCOUNT")
        sessao.commit()
    obtido = ensaiar.esperar_turno(caminho, "100001", prazo_s=2, intervalo_s=0.05)
    assert obtido.acao_principal == "OFFER_DISCOUNT"


def test_esperar_turno_acha_o_turno_pela_lista_de_mensagens_juntadas(banco):
    caminho, fabrica = banco
    with fabrica() as sessao:
        conversa = _cobranca(sessao)
        primeira = _mensagem(sessao, conversa, 100001)
        segunda = _mensagem(sessao, conversa, 100002)
        _turno(sessao, conversa, [primeira, segunda], acao_principal="REINICIO")
        sessao.commit()
    obtido = ensaiar.esperar_turno(caminho, "100002", prazo_s=2, intervalo_s=0.05)
    assert obtido.acao_principal == "REINICIO"


def test_esperar_turno_sem_as_tabelas_tenta_ate_o_prazo_e_volta_none(tmp_path):
    vazio = tmp_path / "vazio.db"
    import sqlite3

    sqlite3.connect(vazio).close()
    assert ensaiar.esperar_turno(vazio, "1", prazo_s=0.3, intervalo_s=0.05) is None


def test_ler_conversa_acha_pela_conversa_do_chatwoot(banco):
    caminho, fabrica = banco
    with fabrica() as sessao:
        conversa = _cobranca(sessao)
        conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
        conversa.motivo_humano = MotivoHumano.TETO
        sessao.commit()
    assert ensaiar.ler_conversa(caminho, 9001) == ("ATENDIMENTO_HUMANO", "TETO")
    assert ensaiar.ler_conversa(caminho, 1) is None


# --- motor (serviço de mentira)


class _ServicoDeMentira:
    """Faz no webhook o que o agente faria: grava a mensagem (sem repetir) e fecha um turno.

    `respostas` mapeia o texto do cliente ao turno; o texto que não está lá fica sem turno.
    """

    def __init__(self, fabrica, falso, respostas, log):
        self.fabrica = fabrica
        self.respostas = respostas
        self.recebidas = 0
        self._trava = threading.Lock()
        dono = self
        chatwoot = ChatwootHttp(falso.url, 91, "token")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                tamanho = int(self.headers.get("Content-Length") or 0)
                evento = json.loads(self.rfile.read(tamanho))
                with dono._trava:
                    dono.recebidas += 1
                    dono._tratar(evento, chatwoot, log)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.servidor.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}/webhooks/chatwoot/segredo"

    def _tratar(self, evento, chatwoot, log):
        lido = ler_evento(evento)
        with self.fabrica() as sessao:
            if sessao.query(Mensagem).filter_by(chatwoot_message_id=lido.mensagem_id).first():
                return
            conversa = sessao.query(Conversa).filter_by(
                chatwoot_conversa_id=lido.conversa_chatwoot_id
            ).first() or _cobranca(
                sessao,
                numero=lido.conversa_chatwoot_id - 9000,
                chatwoot_conversa_id=lido.conversa_chatwoot_id,
            )
            mensagem = _mensagem(sessao, conversa, lido.mensagem_id, lido.texto)
            resposta = self.respostas.get(lido.texto)
            if resposta is not None:
                if resposta.get("erro_no_log"):
                    with open(log, "a", encoding="utf-8") as arquivo:
                        arquivo.write("ERROR agente: algo quebrou\n")
                acao = resposta["acao"]
                if acao.startswith("HUMAN:"):
                    chatwoot.nota_privada(lido.conversa_chatwoot_id, "laudo")
                    chatwoot.etiquetar(lido.conversa_chatwoot_id, [acao.split(":")[1].lower()])
                    conversa.estado = EstadoConversa.ATENDIMENTO_HUMANO
                    conversa.motivo_humano = MotivoHumano(acao.split(":")[1])
                else:
                    chatwoot.enviar_mensagem(lido.conversa_chatwoot_id, resposta["texto"])
                _turno(
                    sessao,
                    conversa,
                    [mensagem],
                    resultado=ResultadoTurno(resposta.get("resultado", "ENVIADO")),
                    acao_principal=acao,
                    texto_enviado=resposta.get("texto"),
                )
            sessao.commit()

    def parar(self):
        self.servidor.shutdown()
        self.servidor.server_close()


@pytest.fixture
def cenario(banco, tmp_path):
    caminho, fabrica = banco
    falso = ensaiar.ChatwootFalsoLocal(91)
    falso.iniciar()
    log = tmp_path / "servico.log"
    log.write_text("ERROR antigo, de antes do passo\n", encoding="utf-8")
    servicos = []

    def montar(respostas):
        servico = _ServicoDeMentira(fabrica, falso, respostas, log)
        servicos.append(servico)
        rodada = ensaiar.Rodada(
            url_webhook=servico.url,
            banco=caminho,
            log=log,
            falso=falso,
            prazo_s=1,
            intervalo_s=0.05,
            pausa=lambda _s: None,
        )
        return servico, rodada

    yield montar
    for servico in servicos:
        servico.parar()
    falso.parar()


DIVIDA = {"acao": "INFORM_DEBT", "texto": "Você deve R$ 100,20. Pix: 00020126abc"}


def test_rodar_roteiro_ok_manda_em_dobro_e_confere_o_fim(cenario):
    servico, rodada = cenario(
        {
            "Quanto eu estou devendo?": DIVIDA,
            "Isso é golpe?": {"acao": "HUMAN:DESCONFIANCA", "resultado": "HANDOFF"},
        }
    )
    roteiro = ensaiar.Roteiro(
        nome="a",
        passos=[
            ensaiar.Passo(
                cliente="Quanto eu estou devendo?",
                espera=ensaiar.Espera(acao="INFORM_DEBT", texto_tem=["100,20"]),
            ),
            ensaiar.Passo(
                cliente="Isso é golpe?",
                espera=ensaiar.Espera(
                    acao="HUMAN:DESCONFIANCA", resultado="HANDOFF", etiqueta="desconfianca"
                ),
            ),
        ],
        fim=ensaiar.Fim(motivo=MotivoHumano.DESCONFIANCA),
    )

    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)

    assert [p.veredito for p in resultado.passos] == [Veredito.OK, Veredito.OK]
    assert resultado.fim.veredito == Veredito.OK
    assert (resultado.ok, resultado.total) == (3, 3)
    assert servico.recebidas == 4  # cada mensagem, em dobro
    primeiro, segundo = (p.obtido for p in resultado.passos)
    # Só os registros do passo: o 1º viu a mensagem, o 2º a nota e a etiqueta.
    assert [r.tipo for r in primeiro.registros_chatwoot] == ["mensagem"]
    assert [r.tipo for r in segundo.registros_chatwoot] == ["nota", "etiquetas"]
    assert primeiro.erros_no_log == []


def test_rodar_roteiro_sem_turno_da_sem_turno_e_diz_o_prazo(cenario):
    _, rodada = cenario({})
    roteiro = ensaiar.Roteiro(nome="a", passos=[ensaiar.Passo(cliente="Ninguém responde")])
    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)
    [passo] = resultado.passos
    assert passo.veredito == Veredito.SEM_TURNO
    assert "1 s" in passo.divergencias[0]


def test_rodar_roteiro_so_olha_o_log_depois_de_cada_passo(cenario):
    _, rodada = cenario(
        {"quebra": {"acao": "INFORM_DEBT", "texto": "x", "erro_no_log": True}, "ok": DIVIDA}
    )
    roteiro = ensaiar.Roteiro(
        nome="a", passos=[ensaiar.Passo(cliente="ok"), ensaiar.Passo(cliente="quebra")]
    )
    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)
    assert resultado.passos[0].veredito == Veredito.OK  # o ERROR antigo do log não conta
    assert resultado.passos[1].veredito == Veredito.ERRO
    assert resultado.passos[1].obtido.erros_no_log == ["ERROR agente: algo quebrou"]


def test_rodar_roteiro_com_intervalo_nao_espera_o_turno_anterior(cenario):
    pausas = []
    _, rodada = cenario({"/reset": {"acao": "REINICIO", "texto": None}, "oi": DIVIDA})
    rodada.pausa = pausas.append
    roteiro = ensaiar.Roteiro(
        nome="a",
        passos=[
            ensaiar.Passo(cliente="/reset"),
            ensaiar.Passo(cliente="oi", intervalo=3, espera=ensaiar.Espera(acao="INFORM_DEBT")),
        ],
    )
    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)
    assert pausas == [3]
    primeiro, segundo = resultado.passos
    assert (primeiro.veredito, primeiro.esperou_turno, primeiro.obtido) == (
        Veredito.OK,
        False,
        None,
    )
    assert segundo.veredito == Veredito.OK


def test_rodar_roteiro_com_webhook_que_recusa_para_no_passo_com_erro(cenario):
    servico, rodada = cenario({})
    servico.parar()  # o serviço some: o POST falha
    roteiro = ensaiar.Roteiro(
        nome="a", passos=[ensaiar.Passo(cliente="1"), ensaiar.Passo(cliente="2")]
    )
    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)
    assert [p.veredito for p in resultado.passos] == [Veredito.ERRO]
    assert resultado.interrompido
    assert resultado.total == 2


def test_rodar_roteiro_com_o_servico_morto_levanta(cenario):
    _, rodada = cenario({})
    rodada.servico_vivo = lambda: False
    roteiro = ensaiar.Roteiro(nome="a", passos=[ensaiar.Passo(cliente="1")])
    with pytest.raises(ensaiar.ErroDoEnsaio, match="caiu"):
        ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)


def test_rodar_roteiro_fim_divergente(cenario):
    _, rodada = cenario({"oi": DIVIDA})
    roteiro = ensaiar.Roteiro(
        nome="a",
        passos=[ensaiar.Passo(cliente="oi")],
        fim=ensaiar.Fim(motivo=MotivoHumano.TETO),
    )
    resultado = ensaiar.rodar_roteiro(roteiro, 1, "+5583900000001", rodada)
    assert resultado.fim.veredito == Veredito.DIVERGIU
    assert "TETO" in resultado.fim.divergencias[0]


# --------------------------------------------------------------------------- relatório


def _resultado_de_exemplo():
    roteiro = ensaiar.Roteiro(
        nome="golpe",
        passos=[
            ensaiar.Passo(
                cliente="Isso é golpe?",
                espera=ensaiar.Espera(acao="HUMAN:DESCONFIANCA", etiqueta="desconfianca"),
            ),
            ensaiar.Passo(cliente="Bom dia", a_decidir=True),
        ],
        fim=ensaiar.Fim(motivo=MotivoHumano.DESCONFIANCA),
    )
    obtido = _obtido(
        acao_principal="NONE",
        resultado="SEM_ENVIO",
        texto_enviado="Linha 1\nLinha 2",
        jev3_reprovadas=[["sem_codigo"], []],
        registros_chatwoot=[
            _registro("nota", "Laudo do Jev"),
            _registro("etiquetas", ["baixa_confianca"]),
            _registro("status", "open"),
        ],
        erros_no_log=["ERROR boom"],
    )
    passos = [
        ensaiar.ResultadoPasso(
            1,
            roteiro.passos[0],
            Veredito.DIVERGIU,
            obtido,
            ["ação: esperava HUMAN:DESCONFIANCA, veio NONE"],
        ),
        ensaiar.ResultadoPasso(2, roteiro.passos[1], Veredito.A_DECIDIR, _obtido()),
    ]
    fim = ensaiar.ResultadoFim(Veredito.DIVERGIU, "ENGAJADO", None, ["motivo: esperava x"])
    return [ensaiar.ResultadoRoteiro(roteiro, "+5583900000004", passos, fim)]


def test_o_relatorio_traz_o_resumo_por_roteiro_e_o_detalhe_por_passo():
    relatorio = ensaiar.formatar_relatorio(_resultado_de_exemplo(), "2026-11-05 10:00")
    assert "| 1 | golpe | 0/3 |" in relatorio
    assert "2026-11-05 10:00" in relatorio
    assert "#### Passo 1: DIVERGIU" in relatorio
    assert "#### Passo 2: A DECIDIR" in relatorio
    assert "#### Fim: DIVERGIU" in relatorio
    assert "`Isso é golpe?`" in relatorio
    assert "ação HUMAN:DESCONFIANCA; etiqueta desconfianca" in relatorio
    assert "ação NONE" in relatorio
    assert "INFORM_DEBT (p=0.93)" in relatorio
    assert "tentativa 1: sem_codigo; tentativa 2: nenhuma" in relatorio
    assert "estado ENGAJADO" in relatorio
    assert "notas: Laudo do Jev" in relatorio
    assert "etiquetas: baixa_confianca" in relatorio
    assert "status: open" in relatorio
    assert "11.0 s" in relatorio
    assert "**No log:** `ERROR boom`" in relatorio
    assert "  > Linha 1\n  > Linha 2" in relatorio
    assert "ação: esperava HUMAN:DESCONFIANCA, veio NONE" in relatorio


def test_o_resumo_do_terminal_tem_uma_linha_por_roteiro():
    resumo = ensaiar.formatar_resumo(_resultado_de_exemplo())
    assert resumo == " 1. golpe: 0/3 OK  (2 diverg., 1 a decidir)"


def test_o_relatorio_de_um_roteiro_interrompido_mostra_o_motivo():
    roteiro = ensaiar.Roteiro(
        nome="a", passos=[ensaiar.Passo(cliente="1"), ensaiar.Passo(cliente="2")]
    )
    passo = ensaiar.ResultadoPasso(
        1, roteiro.passos[0], Veredito.ERRO, None, ["o webhook respondeu [500, 500]"]
    )
    resultado = ensaiar.ResultadoRoteiro(
        roteiro, "+5583900000001", [passo], None, "o webhook respondeu [500, 500]"
    )
    relatorio = ensaiar.formatar_relatorio([resultado])
    assert "INTERROMPIDO: o webhook respondeu [500, 500]" in relatorio
    assert "0/2" in relatorio


# --------------------------------------------------------------------------- CLI


def test_o_ensaio_recusa_uma_pasta_de_saida_que_ja_existe(tmp_path):
    saida = tmp_path / "ensaio"
    saida.mkdir()
    with pytest.raises(ensaiar.ErroDaDemo, match="já existe"):
        ensaiar.ensaiar(tmp_path, tmp_path, [_roteiro()], saida, 8010)


def test_o_ensaio_recusa_uma_porta_ocupada(tmp_path):
    ocupante = _ServidorQualquer()
    porta = ocupante.servidor.server_address[1]
    try:
        with pytest.raises(ensaiar.ErroDaDemo, match="ocupada"):
            ensaiar.ensaiar(tmp_path, tmp_path, [_roteiro()], tmp_path / "novo", porta)
    finally:
        ocupante.parar()
    assert not (tmp_path / "novo").exists()


def test_o_ensaio_sem_as_chaves_falha_antes_de_criar_a_pasta(tmp_path):
    with pytest.raises(ensaiar.ErroDaDemo, match="typesafe.local.json"):
        ensaiar.ensaiar(tmp_path, tmp_path, [_roteiro()], tmp_path / "novo", 18999)
    assert not (tmp_path / "novo").exists()


def test_o_cli_com_roteiro_desconhecido_sai_com_2(tmp_path, capsys):
    codigo = ensaiar.main(
        [
            "--kit",
            str(tmp_path),
            "--roteiro",
            "nao-existe",
            "--roteiros",
            str(_PASTA / "roteiros" / "demo.yaml"),
        ]
    )
    assert codigo == 2
    assert "nao-existe" in capsys.readouterr().err
