"""Os roteiros do ensaio do modo teste (`roteiros/teste.yaml`) contra o código, sem o Luna real.

O ensaio de verdade (`ensaiar.py --teste`) roda com o Luna real e só o Cauê o vê. Aqui o mesmo YAML
corre em processo: cada passo vira o evento do Chatwoot (o `evento_do_cliente` do ensaio), passa
pelo `receber_evento` e pelo turno, com um Luna falso que responde ao que o roteiro diz. Prova que
os rótulos, os textos e os estados que o roteiro espera são os que o código produz; a escolha do
Luna real fica para o ensaio.
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

POLITICA_TESTE = _PASTA / "politica-teste-ensaio.toml"
ROTEIROS_TESTE = _PASTA / "roteiros" / "teste.yaml"
CHATWOOT_ID = 9001

# O que o Luna daria a cada fala (a primeira chave contida nas `novas`); o resto é INFORM_DEBT.
RESPOSTAS = {
    "já paguei": _bruto(alega_pagamento=True, acao="NONE", reply=""),
}


@dataclass
class LunaDoRoteiro(LLMTurnoFalso):
    """O Luna falso: a resposta de `por_texto` para a fala do cliente; no resto, INFORM_DEBT com os
    marcadores que o próprio pedido pede (o Pix, ou o link para quem tem propensão ao cartão)."""

    por_texto: dict = field(default_factory=dict)

    def completar_json(self, sistema, usuario):
        entrada = json.loads(usuario)
        novas = " ".join(entrada["novas"]).casefold()
        resposta = next((r for k, r in self.por_texto.items() if k in novas), None)
        if resposta is None:
            informar = next(a for a in entrada["acoes"] if a["acao"] == "INFORM_DEBT")
            marcadores = " ".join("{" + m + "}" for m in informar["marcadores_obrigatorios"])
            resposta = _bruto(reply=f"Segue o que você pediu: {marcadores}")
        self.respostas = [resposta]
        self._usadas = 0
        return super().completar_json(sistema, usuario)


@pytest.fixture(autouse=True)
def _relogio_da_confirmacao(monkeypatch):
    monkeypatch.setattr(confirmar, "_agora", lambda: SEGUNDA_10H + timedelta(minutes=1))


def _roteiros():
    return ensaiar.carregar_roteiros(ROTEIROS_TESTE)


def _montar(tmp_path, roteiros):
    telefones = ensaiar.montar_kit(tmp_path, tmp_path / "ensaio", roteiros, POLITICA_TESTE)
    politica = carregar_politica(tmp_path / "ensaio" / "politica-demo.toml")
    casos = CTNDeCasos(
        politica.teste.casos, politica.mensalidade_centavos, lambda: SEGUNDA_10H.date()
    )
    deps = montar_deps(politica, casos)
    deps.llm_turno = LunaDoRoteiro(registrar=True, por_texto=RESPOSTAS)
    return telefones, politica, deps


def _divergencias(espera, turno, enviadas_do_passo):
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
        if passo.espera is not None:
            achadas += [
                f"{roteiro.nome} passo {indice}: {a}"
                for a in _divergencias(passo.espera, turno, enviadas)
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
    assert any(p.anexo == "image" and not p.cliente for r in roteiros for p in r.passos)
    # Cada telefone inventado do ensaio tem os 4 últimos dígitos distintos (são a matrícula).
    finais = {ensaiar.telefone_do_roteiro(n)[-4:] for n in range(1, len(roteiros) + 1)}
    assert len(finais) == len(roteiros)


@pytest.mark.parametrize("nome", [r.nome for r in _roteiros()])
def test_o_roteiro_do_teste_bate_com_o_codigo(tmp_path, sessao, nome):
    roteiros = [r for r in _roteiros() if r.nome == nome]
    telefones, politica, deps = _montar(tmp_path, roteiros)

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
