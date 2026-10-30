"""O adaptador contra a amostra real do Chatwoot: só estrutura e contagens, nunca o conteúdo."""

from collections import Counter

from agente.api.chatwoot_webhook import ler_evento
from agente.reproducao.chatwoot_leitura import conversa_lida, para_evento

from .amostra import ler_amostra

TIPOS_DE_ANEXO = {"image", "audio", "file", "video"}


def test_todas_as_mensagens_reais_passam_pelo_adaptador():
    amostra = ler_amostra()
    eventos = Counter()
    entregues = esperadas = anexos_da_api = anexos_lidos = 0

    for arquivo in amostra:
        lida = conversa_lida(arquivo["conversa"])
        assert lida.telefone is not None
        assert lida.ultima_atividade_em >= lida.criada_em
        for bruta in arquivo["mensagens"]:
            dados = para_evento(bruta, lida)
            if bruta["message_type"] == 2 or bruta["private"]:
                assert dados is None
                continue
            assert dados is not None
            esperadas += 1
            evento = ler_evento(dados)
            assert evento is not None
            entregues += 1
            eventos[(evento.tipo, evento.remetente)] += 1
            assert evento.conversa_chatwoot_id == lida.id
            assert evento.mensagem_id == str(bruta["id"])
            assert isinstance(evento.texto, str)
            if evento.tipo == "incoming":
                assert evento.telefone == lida.telefone
            anexos_da_api += len(bruta.get("attachments") or [])
            anexos_lidos += len(evento.anexos)
            assert {a["tipo"] for a in evento.anexos} <= TIPOS_DE_ANEXO

    assert entregues == esperadas > 0
    assert anexos_lidos == anexos_da_api
    # Só o cliente (contact) escreve como incoming e só o atendente (user) como outgoing.
    assert set(eventos) <= {("incoming", "contact"), ("outgoing", "user")}


def test_os_tipos_da_amostra_sao_os_que_o_adaptador_conhece():
    amostra = ler_amostra()
    tipos = Counter(m["message_type"] for arquivo in amostra for m in arquivo["mensagens"])

    # O 3 (template) não apareceu na amostra; o adaptador o trata mesmo assim.
    assert set(tipos) <= {0, 1, 2, 3}
    assert tipos[0] > 0
