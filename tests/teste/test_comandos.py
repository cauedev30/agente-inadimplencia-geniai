"""Os comandos do testador no turno (D56): /caso, /reset e a base dos demais."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.db.historico import registrar_mensagem
from agente.db.modelos import Cobranca, Conversa, Mensagem, Transicao, Turno
from agente.dominio.dinheiro import formatar_reais
from agente.integracoes.chatwoot import ChatwootFalso
from agente.integracoes.disparador import FalhaNoEnvio
from agente.teste.casos import ativar_caso
from agente.teste.comandos import Comando, ler_comando
from agente.tipos import (
    EstadoConversa,
    EstadoEnvio,
    OrigemMensagem,
    ResultadoTurno,
    StatusCobranca,
)
from agente.turno.fila import agendar_turno, mensagens_pendentes
from agente.turno.gates import humano_na_conversa
from agente.turno.orquestrador import _historico, _historico_e_novas, processar_turno
from tests.conftest import TESTADOR_2
from tests.teste.auxiliares import CHATWOOT_ID, Mandante, montar_deps
from tests.turno.test_orquestrador_gates import SEGUNDA_10H

# ---- ler_comando ----


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("/caso", Comando("caso", None, "")),
        ("/caso 3", Comando("caso", 3, "")),
        ("  /CASO   3  ", Comando("caso", 3, "")),
        ("/caso abc", Comando("caso", None, "abc")),
        ("/paguei", Comando("paguei", None, "")),
        ("/paguei 2", Comando("paguei", 2, "")),
        ("/Paguei dois", Comando("desconhecido", None, "/Paguei dois")),
        ("/nota o texto saiu seco", Comando("nota", None, "o texto saiu seco")),
        ("/nota", Comando("nota", None, "")),
        ("#reiniciar", Comando("reset", None, "")),
        ("  #REINICIAR ", Comando("reset", None, "")),
        ("/reset", Comando("desconhecido", None, "/reset")),
        ("/ajuda", Comando("desconhecido", None, "/ajuda")),
        ("/", Comando("desconhecido", None, "/")),
        ("Oi", None),
        ("", None),
        ("   ", None),
        ("quero pagar /caso", None),
    ],
)
def test_ler_comando(politica_de_teste, texto, esperado):
    assert ler_comando(texto, politica_de_teste) == esperado


# ---- o turno com comando ----


class ChatwootQueFalha(ChatwootFalso):
    """Falha no envio enquanto `falhas` for maior que zero."""

    def __init__(self, falhas=1):
        super().__init__()
        self.falhas = falhas

    def enviar_mensagem(self, conversa_id, texto, botoes=None):
        if self.falhas:
            self.falhas -= 1
            raise FalhaNoEnvio("chatwoot fora")
        return super().enviar_mensagem(conversa_id, texto, botoes)


def _ultimo_turno(sessao):
    return sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()


def _mensagens(sessao, conversa):
    return list(
        sessao.scalars(
            select(Mensagem).where(Mensagem.conversa_id == conversa.id).order_by(Mensagem.id)
        )
    )


def _conversa_do_chatwoot(sessao):
    return sessao.scalars(
        select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
    ).one()


def _nao_chamou_o_llm(deps):
    assert deps.jev.chamadas == []
    assert deps.redator.pedidos == []


def test_caso_sem_numero_lista_os_casos_sem_chamar_o_llm(
    sessao, testador, conversa_de_teste, deps, politica_de_teste
):
    testador.manda("/caso")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert resposta.startswith("[TESTE]")
    for caso in politica_de_teste.teste.casos:
        divida = formatar_reais(caso.meses_atraso * politica_de_teste.mensalidade_centavos)
        assert caso.titulo in resposta and divida in resposta
        assert f"{caso.numero}." in resposta
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:CASO", ResultadoTurno.FIXA)
    comando, reply = _mensagens(sessao, conversa_de_teste)
    assert (comando.origem, comando.texto) == (OrigemMensagem.TESTE, "/caso")
    assert (reply.origem, reply.conta_no_teto, reply.texto) == (
        OrigemMensagem.TESTE,
        False,
        resposta,
    )
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA
    _nao_chamou_o_llm(deps)


def test_caso_n_troca_o_caso_e_leva_a_conversa_do_chatwoot_para_a_nova(
    sessao, testador, conversa_de_teste, deps, politica_de_teste
):
    antiga = conversa_de_teste.cobranca
    testador.manda("/caso 3")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(conversa_de_teste)
    assert antiga.status is StatusCobranca.ENCERRADA
    assert antiga.encerrada_motivo == "troca_de_caso"
    assert conversa_de_teste.chatwoot_conversa_id is None
    nova = _conversa_do_chatwoot(sessao)
    assert nova.id != conversa_de_teste.id
    assert nova.cobranca.cliente.matricula_ctn == "SIM-3-7777"
    assert nova.cobranca.status is StatusCobranca.ATIVA
    assert nova.cobranca.mensalidades_abertas == 4
    caso3 = politica_de_teste.teste.casos[2]
    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert resposta == (
        f"[TESTE] Agora você é o caso 3: {caso3.titulo}. CPF fictício: {caso3.cpf}. "
        "Mande qualquer mensagem para o agente começar."
    )
    assert _ultimo_turno(sessao).acao_principal == "TESTE:CASO"
    _nao_chamou_o_llm(deps)


def test_reset_recomeca_o_caso_atual(sessao, testador, conversa_de_teste, deps, casos):
    casos.pagar_meses("SIM-1-7777", 2, SEGUNDA_10H, "k")
    antiga = conversa_de_teste.cobranca
    testador.manda("#reiniciar")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    nova = _conversa_do_chatwoot(sessao)
    assert nova.cobranca_id != antiga.id
    assert nova.cobranca.cliente_id == antiga.cliente_id
    assert nova.cobranca.cliente.matricula_ctn == "SIM-1-7777"
    # Os meses pagos voltam a estar em aberto.
    assert nova.cobranca.mensalidades_abertas == 3
    assert antiga.status is StatusCobranca.ENCERRADA
    assert _ultimo_turno(sessao).acao_principal == "TESTE:RESET"
    assert "caso 1" in deps.chatwoot.enviadas(CHATWOOT_ID)[0]
    _nao_chamou_o_llm(deps)


def test_caso_inexistente_mostra_a_lista_e_nao_troca(
    sessao, testador, conversa_de_teste, deps, politica_de_teste
):
    testador.manda("/caso 9")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert "9" in resposta
    assert all(caso.titulo in resposta for caso in politica_de_teste.teste.casos)
    assert conversa_de_teste.chatwoot_conversa_id == CHATWOOT_ID
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA


def test_comando_desconhecido_responde_a_lista_de_comandos(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("/ajuda")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert resposta.startswith("[TESTE]")
    for comando in ("/caso", "/paguei", "/nota", "#reiniciar"):
        assert comando in resposta
    assert _ultimo_turno(sessao).acao_principal == "TESTE:DESCONHECIDO"
    _nao_chamou_o_llm(deps)


@pytest.mark.parametrize("comando", ["/paguei", "/paguei 2", "/nota algo"])
def test_paguei_e_nota_ainda_nao_estao_ligados_na_base(testador, deps, comando):
    # As tarefas 2.2 e 2.3 ligam os dois; a base só garante que o comando não vai ao LLM.
    testador.manda(comando)
    assert testador.roda(deps) is ResultadoTurno.FIXA
    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert resposta.startswith("[TESTE]")
    _nao_chamou_o_llm(deps)


def test_a_rajada_caso_2_mais_oi_leva_o_oi_para_a_conversa_nova(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("/caso 2", "Oi")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    nova = _conversa_do_chatwoot(sessao)
    assert [m.texto for m in mensagens_pendentes(sessao, nova.id)] == ["Oi"]
    assert nova.turno_em is not None
    antigas = _mensagens(sessao, conversa_de_teste)
    assert [m.texto for m in antigas] == ["/caso 2", deps.chatwoot.enviadas(CHATWOOT_ID)[0]]
    assert _ultimo_turno(sessao).mensagens_ids == [antigas[0].id]
    assert nova.cobranca.cliente.matricula_ctn == "SIM-2-7777"


def test_o_oi_antes_do_comando_e_consumido_sem_resposta(sessao, testador, conversa_de_teste, deps):
    testador.manda("Oi", "/caso")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    assert len(deps.chatwoot.enviadas(CHATWOOT_ID)) == 1
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []
    _nao_chamou_o_llm(deps)


def test_dois_comandos_na_rajada_respondem_cada_um_na_ordem(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("/caso", "/ajuda")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    primeira, segunda = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert "Casos" in primeira
    assert "Comando" in segunda
    assert _ultimo_turno(sessao).acao_principal == "TESTE:DESCONHECIDO"


def test_o_comando_depois_do_handoff_funciona_e_devolve_a_conversa_ao_robo(
    sessao, testador, conversa_de_teste, deps
):
    conversa_de_teste.human_owned = True
    conversa_de_teste.estado = EstadoConversa.ATENDIMENTO_HUMANO
    sessao.commit()
    testador.manda("/caso 4")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(conversa_de_teste)
    # A conversa antiga ficou com o humano (é histórico); a do Chatwoot agora é outra, sem humano.
    assert conversa_de_teste.human_owned is True
    assert conversa_de_teste.chatwoot_conversa_id is None
    nova = _conversa_do_chatwoot(sessao)
    assert nova.id != conversa_de_teste.id
    assert nova.human_owned is False
    assert nova.estado is not EstadoConversa.ATENDIMENTO_HUMANO
    assert not humano_na_conversa(nova)
    assert ("mudar_status", CHATWOOT_ID, "pending") in deps.chatwoot.chamadas


def test_sem_humano_o_status_do_chatwoot_nao_muda(testador, deps):
    testador.manda("/caso 4")
    testador.roda(deps)
    assert not any(c[0] == "mudar_status" for c in deps.chatwoot.chamadas)


def test_o_comando_nao_conta_no_teto(sessao, testador, conversa_de_teste, deps):
    testador.manda("/caso")
    testador.roda(deps)
    assert all(not m.conta_no_teto for m in _mensagens(sessao, conversa_de_teste))


def test_o_llm_nao_ve_o_comando_nem_a_resposta(sessao, testador, conversa_de_teste, deps):
    testador.manda("/caso 2")
    testador.roda(deps)
    nova = Mandante(sessao, _conversa_do_chatwoot(sessao))
    nova.n = testador.n
    nova.manda("Oi")
    nova.roda(deps)  # o 1º contato do caso novo recebe a abertura (2.1), sem o LLM
    nova.manda("quero pagar")
    nova.roda(deps)
    assert deps.jev.chamadas, "o turno normal da conversa nova deveria rodar"
    for _perguntas, estado in deps.jev.chamadas:
        assert all(origem != "TESTE" for origem, _ in estado.historico)
        assert all("/caso" not in texto and "[TESTE]" not in texto for _, texto in estado.historico)


def test_o_historico_do_llm_deixa_de_fora_a_origem_teste(sessao, conversa_de_teste):
    conversa = conversa_de_teste
    for n, (origem, texto) in enumerate(
        [
            (OrigemMensagem.CLIENTE, "Oi"),
            (OrigemMensagem.TESTE, "/caso"),
            (OrigemMensagem.TESTE, "[TESTE] lista"),
            (OrigemMensagem.AGENTE, "Olá"),
            (OrigemMensagem.CLIENTE, "e agora?"),
        ]
    ):
        registrar_mensagem(sessao, conversa.id, origem, texto, f"h-{n}", False, SEGUNDA_10H)
    sessao.commit()
    ultimo = _mensagens(sessao, conversa)[-1]

    assert _historico(sessao, conversa.id) == [
        ("CLIENTE", "Oi"),
        ("AGENTE", "Olá"),
        ("CLIENTE", "e agora?"),
    ]
    historico, novas = _historico_e_novas(sessao, conversa.id, [ultimo.id])
    assert historico == [("CLIENTE", "Oi"), ("AGENTE", "Olá")]
    assert novas == ["e agora?"]


def test_comando_de_telefone_fora_do_piloto_vira_texto_comum(
    sessao, politica_de_teste, casos, deps
):
    cobranca = ativar_caso(sessao, casos, politica_de_teste, "+5583977776666", 1, SEGUNDA_10H)
    conversa = Conversa(cobranca=cobranca, chatwoot_conversa_id=777)
    sessao.add(conversa)
    sessao.commit()
    testador = Mandante(sessao, conversa)
    testador.manda("/caso 3")

    testador.roda(deps)

    # Sem a liberação em piloto o turno é o da sombra: nada sai e o comando segue como texto.
    assert deps.chatwoot.enviadas(777) == []
    assert _mensagens(sessao, conversa)[0].origem is OrigemMensagem.CLIENTE
    assert cobranca.status is StatusCobranca.ATIVA


def test_sem_o_modo_teste_o_comando_e_texto_comum(
    sessao, conversa_de_teste, politica_de_teste, casos
):
    deps = montar_deps(politica_de_teste, casos, com_casos=False)
    testador = Mandante(sessao, conversa_de_teste)
    testador.manda("/caso 3")

    testador.roda(deps)

    assert _mensagens(sessao, conversa_de_teste)[0].origem is OrigemMensagem.CLIENTE
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA


def test_o_caso_encerrado_responde_que_terminou_e_o_comando_ainda_funciona(
    sessao, testador, conversa_de_teste, deps
):
    conversa_de_teste.cobranca.status = StatusCobranca.PAGA
    sessao.commit()
    testador.manda("Oi")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    [resposta] = deps.chatwoot.enviadas(CHATWOOT_ID)
    assert resposta == (
        "[TESTE] Este caso terminou. Mande /caso para ver os casos ou /caso <n> para começar outro."
    )
    turno = _ultimo_turno(sessao)
    assert (turno.acao_principal, turno.resultado) == ("TESTE:ENCERRADO", ResultadoTurno.FIXA)
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []
    _nao_chamou_o_llm(deps)

    testador.manda("/caso 5")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    nova = _conversa_do_chatwoot(sessao)
    assert nova.cobranca.cliente.matricula_ctn == "SIM-5-7777"
    assert nova.cobranca.status is StatusCobranca.ATIVA


def test_o_caso_encerrado_vem_antes_do_humano_na_conversa(
    sessao, testador, conversa_de_teste, deps
):
    conversa_de_teste.cobranca.status = StatusCobranca.PAGA
    conversa_de_teste.human_owned = True
    sessao.commit()
    testador.manda("Oi")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    assert "terminou" in deps.chatwoot.enviadas(CHATWOOT_ID)[0]


def test_falha_no_chatwoot_desfaz_o_turno_e_o_comando_e_refeito(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    chatwoot = ChatwootQueFalha(falhas=1)
    deps = montar_deps(politica_de_teste, casos, chatwoot)
    antiga = conversa_de_teste.cobranca
    testador.manda("/caso 3")

    assert testador.roda(deps) is ResultadoTurno.ERRO

    # Nada do turno ficou: o caso não trocou, o comando segue pendente e a falha foi contada.
    sessao.refresh(conversa_de_teste)
    sessao.refresh(antiga)
    assert antiga.status is StatusCobranca.ATIVA
    assert conversa_de_teste.chatwoot_conversa_id == CHATWOOT_ID
    assert [m.texto for m in mensagens_pendentes(sessao, conversa_de_teste.id)] == ["/caso 3"]
    assert _mensagens(sessao, conversa_de_teste)[0].origem is OrigemMensagem.CLIENTE
    assert conversa_de_teste.falhas_turno == 1
    assert conversa_de_teste.turno_em is not None

    # O Chatwoot volta: o comando é refeito.
    testador.n += 5
    assert testador.roda(deps) is ResultadoTurno.FIXA
    nova = _conversa_do_chatwoot(sessao)
    assert nova.cobranca.cliente.matricula_ctn == "SIM-3-7777"
    assert sessao.get(Conversa, conversa_de_teste.id).falhas_turno == 0
    ativas = sessao.scalars(select(Cobranca).where(Cobranca.status == StatusCobranca.ATIVA)).all()
    assert len(ativas) == 1


def test_a_troca_de_caso_nao_mexe_na_conversa_de_outro_testador(
    sessao, politica_de_teste, casos, conversa_de_teste, testador, deps
):
    outra = ativar_caso(sessao, casos, politica_de_teste, TESTADOR_2, 2, SEGUNDA_10H)
    conversa_da_outra = Conversa(cobranca=outra, chatwoot_conversa_id=999)
    sessao.add(conversa_da_outra)
    sessao.commit()
    testador.manda("/caso 4")
    testador.roda(deps)
    assert outra.status is StatusCobranca.ATIVA
    assert conversa_da_outra.chatwoot_conversa_id == 999


def test_o_turno_do_comando_nao_muda_o_estado_da_conversa(
    sessao, testador, conversa_de_teste, deps
):
    antes = conversa_de_teste.estado
    testador.manda("/caso")
    testador.roda(deps)
    assert conversa_de_teste.estado is antes
    assert sessao.scalars(select(Transicao)).all() == []


# ---- as correções da revisão da PARADA 1 (lote C1) ----


class ChatwootQueNaoMudaOStatus(ChatwootFalso):
    def mudar_status(self, conversa_id, status):
        self.chamadas.append(("mudar_status", conversa_id, status))
        raise FalhaNoEnvio("chatwoot fora")


class ChatwootQueNaoMudaOStatusNaPrimeira(ChatwootFalso):
    def __init__(self):
        super().__init__()
        self.falhas = 1

    def mudar_status(self, conversa_id, status):
        if self.falhas:
            self.falhas -= 1
            self.chamadas.append(("mudar_status_falhou", conversa_id, status))
            raise FalhaNoEnvio("chatwoot fora")
        super().mudar_status(conversa_id, status)


def test_o_status_que_falha_na_1a_vez_passa_na_2a_tentativa(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    conversa_de_teste.human_owned = True
    conversa_de_teste.estado = EstadoConversa.ATENDIMENTO_HUMANO
    sessao.commit()
    chatwoot = ChatwootQueNaoMudaOStatusNaPrimeira()
    deps = montar_deps(politica_de_teste, casos, chatwoot)
    testador.manda("/caso 4")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    assert [c[0] for c in chatwoot.chamadas if c[0].startswith("mudar_status")] == [
        "mudar_status_falhou",
        "mudar_status",
    ]
    assert ("mudar_status", CHATWOOT_ID, "pending") in chatwoot.chamadas
    assert len(chatwoot.enviadas(CHATWOOT_ID)) == 1


def test_a_mensagem_que_chegou_na_conversa_ja_trocada_vai_para_a_viva_sem_chamar_o_chatwoot(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("/caso 3")
    assert testador.roda(deps) is ResultadoTurno.FIXA
    viva = _conversa_do_chatwoot(sessao)
    enviadas_antes = len(deps.chatwoot.chamadas)
    # O webhook ainda via a conversa antiga ligada (READ COMMITTED) e gravou o "Oi" nela.
    testador.manda("Oi")
    agendar_turno(sessao, conversa_de_teste.id, SEGUNDA_10H)
    sessao.commit()
    assert conversa_de_teste.chatwoot_conversa_id is None

    assert testador.roda(deps) is ResultadoTurno.FIXA

    # Nada saiu (o envio a `None` daria 404 e se repetiria) e o "Oi" é da conversa viva.
    assert len(deps.chatwoot.chamadas) == enviadas_antes
    assert deps.chatwoot.enviadas(None) == []
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []
    assert [m.texto for m in mensagens_pendentes(sessao, viva.id)] == ["Oi"]
    assert viva.turno_em is not None
    assert conversa_de_teste.turno_em is None
    assert conversa_de_teste.falhas_turno == 0

    # O turno da conversa viva responde ao "Oi" (o 1º contato do caso: a abertura, sem o LLM).
    resultado = processar_turno(sessao, viva.id, SEGUNDA_10H + timedelta(minutes=30), deps)
    assert resultado is ResultadoTurno.FIXA
    assert deps.chatwoot.enviadas(CHATWOOT_ID)[-1].startswith("Oi, Carla!")


def test_a_mensagem_na_conversa_morta_sem_conversa_viva_e_consumida_sem_envio(
    sessao, testador, conversa_de_teste, deps
):
    conversa_de_teste.chatwoot_conversa_id = None
    conversa_de_teste.cobranca.status = StatusCobranca.ENCERRADA
    sessao.commit()
    testador.manda("Oi")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    assert deps.chatwoot.chamadas == []
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []


def test_o_rollback_do_reset_devolve_o_estado_do_caso_na_memoria(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    casos.pagar_meses("SIM-1-7777", 2, SEGUNDA_10H, "k")
    abertas = [a.mes_ano for a in casos.mensalidades_em_aberto("SIM-1-7777")]
    assert len(abertas) == 1
    deps = montar_deps(politica_de_teste, casos, ChatwootQueFalha(falhas=1))
    testador.manda("#reiniciar")

    assert testador.roda(deps) is ResultadoTurno.ERRO

    # O banco desfez a troca; a memória tem de ter desfeito também (senão o caso mostraria 3 meses
    # em aberto e o `/paguei n` pagaria meses já pagos).
    sessao.refresh(conversa_de_teste.cobranca)
    assert conversa_de_teste.cobranca.status is StatusCobranca.ATIVA
    assert [a.mes_ano for a in casos.mensalidades_em_aberto("SIM-1-7777")] == abertas


def test_o_status_que_falha_depois_do_commit_nao_desfaz_a_troca_nem_repete_o_envio(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    conversa_de_teste.human_owned = True
    conversa_de_teste.estado = EstadoConversa.ATENDIMENTO_HUMANO
    sessao.commit()
    chatwoot = ChatwootQueNaoMudaOStatus()
    deps = montar_deps(politica_de_teste, casos, chatwoot)
    testador.manda("/caso 4")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    nova = _conversa_do_chatwoot(sessao)
    assert nova.cobranca.cliente.matricula_ctn == "SIM-4-7777"
    assert len(chatwoot.enviadas(CHATWOOT_ID)) == 1
    # Duas tentativas, as duas falham: só o log, o turno segue intacto e sem reenvio.
    assert [c for c in chatwoot.chamadas if c[0] == "mudar_status"] == [
        ("mudar_status", CHATWOOT_ID, "pending"),
        ("mudar_status", CHATWOOT_ID, "pending"),
    ]
    assert conversa_de_teste.falhas_turno == 0
    assert mensagens_pendentes(sessao, conversa_de_teste.id) == []


def _gravar_envio_pendente(sessao, conversa):
    mensagem = registrar_mensagem(
        sessao,
        conversa.id,
        OrigemMensagem.AGENTE,
        "Pague 3 meses por R$ 100",
        None,
        True,
        SEGUNDA_10H,
        envio=EstadoEnvio.PENDENTE,
    )
    sessao.commit()
    return mensagem


def test_a_troca_de_caso_descarta_a_mensagem_pendente_do_agente_da_conversa_antiga(
    sessao, testador, conversa_de_teste, deps
):
    pendente = _gravar_envio_pendente(sessao, conversa_de_teste)
    testador.manda("/caso 3")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(pendente)
    assert pendente.envio is EstadoEnvio.DESCARTADA
    assert pendente.conta_no_teto is False
    # Só a resposta do comando saiu; a pendente antiga não vai para a conversa nova.
    assert len(deps.chatwoot.enviadas(CHATWOOT_ID)) == 1


@pytest.mark.parametrize("comando", ["/caso", "/ajuda", "/caso 9"])
def test_o_comando_que_nao_troca_o_caso_mantem_o_retorno_do_envio_pendente(
    sessao, politica_de_teste, testador, conversa_de_teste, deps, comando
):
    _gravar_envio_pendente(sessao, conversa_de_teste)
    testador.manda(comando)

    assert testador.roda(deps) is ResultadoTurno.FIXA

    sessao.refresh(conversa_de_teste)
    agora = SEGUNDA_10H + timedelta(minutes=testador.n)
    assert conversa_de_teste.turno_em == agora + politica_de_teste.espera_integracao(1)


def test_o_comando_sem_envio_pendente_deixa_o_turno_zerado(
    sessao, testador, conversa_de_teste, deps
):
    testador.manda("/caso")
    testador.roda(deps)
    sessao.refresh(conversa_de_teste)
    assert conversa_de_teste.turno_em is None


def test_o_reinicio_com_o_comando_reset_da_politica_do_dev2(
    sessao, politica_de_teste, casos, conversa_de_teste, testador
):
    politica = politica_de_teste.model_copy(update={"comando_reinicio": "/reset"})
    deps = montar_deps(politica, casos)
    assert ler_comando("/reset", politica) == Comando("reset", None, "")
    assert ler_comando("#reiniciar", politica) is None
    antiga = conversa_de_teste.cobranca
    testador.manda("/reset")

    assert testador.roda(deps) is ResultadoTurno.FIXA

    nova = _conversa_do_chatwoot(sessao)
    assert nova.cobranca_id != antiga.id
    assert nova.cobranca.cliente.matricula_ctn == "SIM-1-7777"
    assert _ultimo_turno(sessao).acao_principal == "TESTE:RESET"
    _nao_chamou_o_llm(deps)
    ajuda = Mandante(sessao, nova)
    ajuda.n = testador.n
    ajuda.manda("/ajuda")
    ajuda.roda(deps)
    assert "/reset" in deps.chatwoot.enviadas(CHATWOOT_ID)[-1]
