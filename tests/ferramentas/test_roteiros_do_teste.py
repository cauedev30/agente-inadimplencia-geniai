"""Os roteiros do ensaio do modo teste (`roteiros/teste.yaml` e `roteiros/autonomia.yaml`) contra o
código, sem o Luna real.

O ensaio de verdade (`ensaiar.py --teste`) roda com o Luna real e só o Cauê o vê. Aqui o mesmo YAML
corre em processo: cada passo vira o evento do Chatwoot (o `evento_do_cliente` do ensaio), passa
pelo `receber_evento` e pelo turno, com um Luna falso que responde ao que o roteiro diz. Prova que
os rótulos, os textos, as etiquetas e os estados que o roteiro espera são os que o código produz; a
escolha do Luna real fica para o ensaio.
"""

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import select

from agente.api.chatwoot_webhook import receber_evento
from agente.config import Config
from agente.db.modelos import Conversa, Turno
from agente.integracoes.ctn_teste import CTNDeCasos
from agente.integracoes.llm_turno import LLMTurnoFalso
from agente.pagamento import confirmar
from agente.pagamento.concluir import concluir_pagamentos
from agente.politica import carregar_politica
from agente.turno.orquestrador import processar_turno
from tests.teste.auxiliares import montar_deps
from tests.turno.auxiliares import SEGUNDA_10H, _bruto

from ..conftest import RAIZ

_PASTA = RAIZ / "ferramentas" / "demo-whatsapp"
_spec = importlib.util.spec_from_file_location("demo_whatsapp_ensaiar_teste", _PASTA / "ensaiar.py")
ensaiar = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ensaiar
_spec.loader.exec_module(ensaiar)

POLITICA_TESTE = RAIZ / "deploy" / "dev2" / "politica-teste.exemplo.toml"
ROTEIROS_TESTE = _PASTA / "roteiros" / "teste.yaml"
ROTEIROS_AUTONOMIA = _PASTA / "roteiros" / "autonomia.yaml"
CHATWOOT_ID = 9001

# O que o Luna daria a cada fala do `teste.yaml` (a primeira chave com que as `novas` começam); o
# resto é INFORM_DEBT.
RESPOSTAS = {
    "já paguei": _bruto(alega_pagamento=True, acao="NONE", reply=""),
    "quais meses": _bruto(acao="NONE", reply=""),
}

# A passagem do Luna ao atendente: sem número, sem link e com a palavra "atendente".
PASSAGEM = "Vou pedir para um atendente da unidade continuar essa conversa com você."


def _passagem(**sinais):
    return _bruto(acao="NONE", reply=PASSAGEM, **sinais)


# A oferta do caso 1 (quem não paga Pix todo mês): o desconto vai com o Pix.
OFERTA_NO_PIX = (
    "Consigo {valor_final}, no Pix ou no link.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
)

# O mesmo para o `autonomia.yaml`: o resto é ANSWER. O jurídico e o golpe ("Procon", "golpe") nem
# chegam ao Luna, porque a palavra-chave vai ao atendente.
RESPOSTAS_DA_AUTONOMIA = {
    "tem desconto": _bruto(acao="OFFER_DISCOUNT", reply=OFERTA_NO_PIX),
    "qual a chave pix": _bruto(
        acao="GENERATE_PIX", reply="Aqui está.\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
    ),
    "e no pix, o desconto": _bruto(
        acao="ANSWER", reply="Vale no Pix também: o valor de {valor_final} é o final."
    ),
    "então esse desconto só existe": _bruto(
        acao="ANSWER",
        reply="Não precisa: o valor de {valor_final} vale no Pix e no link. O link já cadastra o "
        "cartão; sem cadastrar, é só pagar o Pix.",
    ),
    "não quero cadastrar": _bruto(
        acao="GENERATE_PIX",
        reply="Sem problema. O valor com desconto é {valor_final}.\n"
        "Para pagar, use o Pix CNPJ: {pix_copia_e_cola}",
    ),
    "tô desempregado": _bruto(acao="OFFER_DISCOUNT", reply=OFERTA_NO_PIX),
    "já paguei": _passagem(alega_pagamento=True),
    "quero cancelar": _passagem(quer_retencao=True),
    "quero falar com um atendente": _passagem(quer_humano=True),
    "meu marido faleceu": _passagem(vulnerabilidade=True),
    "fui muito mal atendida": _passagem(atrito_clinica=True),
    "quais meses": _passagem(sem_resposta=True),
}


@dataclass
class LunaDoRoteiro(LLMTurnoFalso):
    """O Luna falso: a resposta de `por_texto` para a fala do cliente; no resto, a `acao_padrao`
    (INFORM_DEBT ou ANSWER) com os marcadores que o próprio pedido pede (o Pix, ou o link para quem
    tem propensão ao cartão)."""

    por_texto: dict = field(default_factory=dict)
    acao_padrao: str = "INFORM_DEBT"

    def completar_json(self, sistema, usuario, esquema):
        entrada = json.loads(usuario)
        novas = " ".join(entrada["novas"]).casefold()
        resposta = next((r for k, r in self.por_texto.items() if novas.startswith(k)), None)
        if resposta is None:
            padrao = next(a for a in entrada["acoes"] if a["acao"] == self.acao_padrao)
            obrigatorios = padrao["marcadores_obrigatorios"]
            marcadores = " ".join("{" + m + "}" for m in obrigatorios if m != "pix_copia_e_cola")
            pix = (
                "\nPara pagar, use o Pix CNPJ: {pix_copia_e_cola}"
                if "pix_copia_e_cola" in obrigatorios
                else ""
            )
            resposta = _bruto(
                acao=self.acao_padrao, reply=f"Segue o que você pediu: {marcadores}{pix}"
            )
        self.respostas = [resposta]
        self._usadas = 0
        return super().completar_json(sistema, usuario, esquema)


@pytest.fixture(autouse=True)
def _relogio_da_confirmacao(monkeypatch):
    monkeypatch.setattr(confirmar, "_agora", lambda: SEGUNDA_10H + timedelta(minutes=1))


# O arquivo de roteiros, o que o Luna falso responde a cada fala e a ação do resto.
ARQUIVOS = {
    "teste": (ROTEIROS_TESTE, RESPOSTAS, "INFORM_DEBT"),
    "autonomia": (ROTEIROS_AUTONOMIA, RESPOSTAS_DA_AUTONOMIA, "ANSWER"),
}


def _roteiros(arquivo="teste"):
    return ensaiar.carregar_roteiros(ARQUIVOS[arquivo][0])


def _pares():
    """Um par (arquivo, roteiro) para cada roteiro dos dois arquivos."""
    return [(arquivo, r.nome) for arquivo in ARQUIVOS for r in _roteiros(arquivo)]


def _montar(tmp_path, roteiros, arquivo="teste"):
    telefones = ensaiar.montar_kit(tmp_path, tmp_path / "ensaio", roteiros, POLITICA_TESTE)
    politica = carregar_politica(tmp_path / "ensaio" / "politica-demo.toml")
    casos = CTNDeCasos(
        politica.teste.casos, politica.mensalidade_centavos, lambda: SEGUNDA_10H.date()
    )
    deps = montar_deps(politica, casos)
    _, respostas, acao_padrao = ARQUIVOS[arquivo]
    deps.llm_turno = LunaDoRoteiro(registrar=True, por_texto=respostas, acao_padrao=acao_padrao)
    return telefones, politica, deps


def _divergencias(espera, turno, enviadas_do_passo, etiquetas_do_passo=()):
    achadas = []
    if espera.acao is not None and turno.acao_principal != espera.acao:
        achadas.append(f"ação: esperava {espera.acao}, veio {turno.acao_principal}")
    if espera.resultado is not None and turno.resultado.value != espera.resultado.value:
        achadas.append(f"resultado: esperava {espera.resultado.value}, veio {turno.resultado}")
    texto = (turno.texto_enviado or "").casefold()
    achadas += [f"texto sem «{t}»" for t in espera.texto_tem if t.casefold() not in texto]
    achadas += [f"texto com «{t}»" for t in espera.texto_nao_tem if t.casefold() in texto]
    achadas += [
        f"nenhuma mensagem com «{t}»"
        for t in espera.mensagem_tem
        if not any(t.casefold() in e.casefold() for e in enviadas_do_passo)
    ]
    if espera.etiqueta is not None and espera.etiqueta.casefold() not in etiquetas_do_passo:
        achadas.append(f"etiqueta «{espera.etiqueta}» não posta")
    return achadas


def _rodar(roteiro, telefone, sessao, politica, deps):
    """Roda o roteiro em processo e devolve as divergências, passo a passo, e a conversa viva."""
    config = Config(
        database_url="sqlite://", webhook_segredo="x", chatwoot_conta=91, chatwoot_inbox=280
    )
    achadas, agora = [], SEGUNDA_10H
    for indice, passo in enumerate(roteiro.passos, start=1):
        agora += timedelta(minutes=1)
        antes = len(deps.chatwoot.enviadas(CHATWOOT_ID))
        chamadas_antes = len(deps.chatwoot.chamadas)
        evento = ensaiar.evento_do_cliente(
            1000 + indice, CHATWOOT_ID, telefone, passo.cliente, 91, 280, passo.anexo
        )
        resultado = receber_evento(sessao, evento, politica, config, agora, deps.casos)
        assert resultado == "agendado", (roteiro.nome, indice, resultado)
        sessao.commit()
        conversa = sessao.scalars(
            select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
        ).one()
        processar_turno(sessao, conversa.id, agora, deps)
        if passo.cliente.startswith("/paguei"):
            # A confirmação do agente sai de uma rotina, depois do turno do comando.
            concluir_pagamentos(sessao, deps, agora + timedelta(minutes=1))
        turno = sessao.scalars(select(Turno).order_by(Turno.id.desc())).first()
        enviadas = deps.chatwoot.enviadas(CHATWOOT_ID)[antes:]
        etiquetas = [
            e.casefold()
            for chamada in deps.chatwoot.chamadas[chamadas_antes:]
            if chamada[0] == "etiquetar"
            for e in chamada[2]
        ]
        if passo.espera is not None:
            achadas += [
                f"{roteiro.nome} passo {indice}: {a}"
                for a in _divergencias(passo.espera, turno, enviadas, etiquetas)
            ]
    conversa = sessao.scalars(
        select(Conversa).where(Conversa.chatwoot_conversa_id == CHATWOOT_ID)
    ).one()
    sessao.refresh(conversa.cobranca)
    if roteiro.fim is not None:
        achadas += [
            f"{roteiro.nome} fim: {a}"
            for a in ensaiar.divergencias_do_fim(
                roteiro.fim,
                conversa.estado.value,
                conversa.motivo_humano.value if conversa.motivo_humano else None,
                conversa.cobranca.status.value,
                conversa.cobranca.mensalidades_abertas,
            )
        ]
    return achadas


def test_o_teste_yaml_real_carrega_e_traz_os_cinco_casos():
    roteiros = _roteiros()
    nomes = [r.nome for r in roteiros]
    assert len(nomes) == len(set(nomes))
    primeiras = " ".join(p.cliente for r in roteiros for p in r.passos)
    for numero in (2, 3, 4, 5):
        assert f"/caso {numero}" in primeiras
    for comando in ("/paguei 2", "/nota", "/caso", "/reset", "Pagar com Pix", "Já paguei"):
        assert comando in primeiras
    assert any(p.anexo == "image" and p.cliente == "[Image]" for r in roteiros for p in r.passos)
    # Cada telefone inventado do ensaio tem os 4 últimos dígitos distintos (são a matrícula).
    finais = {ensaiar.telefone_do_roteiro(n)[-4:] for n in range(1, len(roteiros) + 1)}
    assert len(finais) == len(roteiros)


def test_o_autonomia_yaml_real_traz_o_que_a_spec_pede():
    roteiros = {r.nome: r for r in _roteiros("autonomia")}
    assert len(roteiros) == 1 + 4 + 1 + 1 + 8 + 1
    # Todo roteiro começa pelo "Oi" da abertura e cabe no teto de 5 mensagens do agente.
    for roteiro in roteiros.values():
        assert roteiro.passos[0].cliente == "Oi"
        assert roteiro.passos[0].espera.acao == "TESTE:ABERTURA"
        assert len(roteiro.passos) - 1 <= 5, roteiro.nome
    # O caso do Cauê, as 15 dúvidas (a 1ª de cada roteiro com a chave Pix), "não quero cartão" e a
    # dificuldade.
    assert [p.cliente for p in roteiros["desconto-no-pix-e-no-link"].passos[1:]] == [
        "Tem desconto?",
        "Então esse desconto só existe se eu cadastrar meu cartão?",
        "E no Pix, o desconto vale também?",
    ]
    duvidas = [r for nome, r in roteiros.items() if nome.startswith("duvidas-")]
    assert [r.nome for r in duvidas] == [f"duvidas-{n}" for n in range(1, 5)]
    assert sum(len(r.passos) - 1 for r in duvidas) == 15
    for roteiro in duvidas:
        falas = roteiro.passos[1:]
        assert all(p.espera.resultado.value == "ENVIADO" for p in falas)
        # A chave vai na abertura (o Pix integral é o meio vigente); uma resposta (ANSWER) não a
        # repete. No duvidas-1 a 1ª pergunta pode virar a oferta, que leva o Pix de novo: a chave
        # se confere na 2ª resposta. No duvidas-4 o cliente pede a chave e recebe o Pix.
        assert "29760112000122" in roteiro.passos[0].espera.mensagem_tem
        if roteiro.nome != "duvidas-4":
            conferida = falas[1] if roteiro.nome == "duvidas-1" else falas[0]
            assert "29760112000122" in conferida.espera.texto_nao_tem
        if roteiro.nome == "duvidas-1":
            assert roteiro.fim.cobranca.value == "ATIVA"
        else:
            esperado = "AGUARDANDO_PAGAMENTO" if roteiro.nome == "duvidas-4" else "ENGAJADO"
            assert roteiro.fim.estado.value == esperado
    assert roteiros["nao-quer-cartao"].passos[-1].espera.acao == "GENERATE_PIX"
    assert roteiros["dificuldade"].passos[-1].espera.acao == "OFFER_DISCOUNT"
    # Os 8 de pessoa: atendente, com o rótulo, a etiqueta e o motivo; a mensagem tem "atendente".
    pessoas = [
        r
        for r in roteiros.values()
        if r.fim and r.fim.estado and r.fim.estado.value == "ATENDIMENTO_HUMANO"
    ]
    assert {r.fim.motivo.value for r in pessoas} == {
        "CONCILIACAO",
        "RETENCAO",
        "JURIDICO",
        "DESCONFIANCA",
        "PEDIU_HUMANO",
        "CAUTELA",
        "ATRITO",
        "SEM_RESPOSTA",
    }
    for roteiro in pessoas:
        espera = roteiro.passos[-1].espera
        assert espera.resultado.value == "HANDOFF"
        assert espera.acao == f"HUMAN:{roteiro.fim.motivo.value}"
        assert espera.etiqueta == roteiro.fim.motivo.value.lower()
        assert any("atendente" in t for t in espera.mensagem_tem)
    # O link seguro fica a decidir, sem expectativa.
    link = roteiros["link-seguro"].passos[-1]
    assert link.a_decidir and link.espera is None


@pytest.mark.parametrize(("arquivo", "nome"), _pares())
def test_o_roteiro_do_teste_bate_com_o_codigo(tmp_path, sessao, arquivo, nome):
    roteiros = [r for r in _roteiros(arquivo) if r.nome == nome]
    telefones, politica, deps = _montar(tmp_path, roteiros, arquivo)

    achadas = _rodar(roteiros[0], telefones[nome], sessao, politica, deps)

    assert achadas == []


def test_o_roteiro_que_espera_o_que_o_codigo_nao_faz_diverge(tmp_path, sessao):
    # Prova que a conferência em processo enxerga a divergência (não passa em silêncio).
    roteiro = next(r for r in _roteiros() if r.nome == "caso-1")
    errado = roteiro.model_copy(
        update={
            "passos": [
                roteiro.passos[0].model_copy(
                    update={"espera": ensaiar.Espera(acao="TESTE:RESET", mensagem_tem=["999,99"])}
                )
            ],
            "fim": ensaiar.Fim(cobranca="PAGA"),
        }
    )
    telefones, politica, deps = _montar(tmp_path, [roteiro])

    achadas = _rodar(errado, telefones["caso-1"], sessao, politica, deps)

    assert len(achadas) == 3
    assert any("TESTE:RESET" in a for a in achadas) and any("999,99" in a for a in achadas)
    assert any("fim" in a for a in achadas)
