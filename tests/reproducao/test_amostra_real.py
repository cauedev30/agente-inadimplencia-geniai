"""O adaptador contra a amostra real do Chatwoot: só estrutura e contagens, nunca o conteúdo."""

from collections import Counter

from agente.api.chatwoot_webhook import ler_evento
from agente.reproducao.chatwoot_leitura import conversa_lida, para_evento

from .amostra import ler_amostra

TIPOS_DE_ANEXO = {"image", "audio", "file", "video"}


def test_todas_as_mensagens_reais_passam_pelo_adaptador():
    # Cada asserção calcula o booleano antes e afirma com mensagem fixa: o pytest imprime os valores
    # comparados quando uma asserção falha, e aqui eles são conversa e telefone de cliente.
    amostra = ler_amostra()
    eventos = Counter()
    entregues = esperadas = anexos_da_api = anexos_lidos = 0

    for arquivo in amostra:
        lida = conversa_lida(arquivo["conversa"])
        tem_telefone = lida.telefone is not None
        assert tem_telefone, "conversa da amostra sem telefone"
        atividade_depois_da_criacao = lida.ultima_atividade_em >= lida.criada_em
        assert atividade_depois_da_criacao, "atividade anterior à criação da conversa"
        for bruta in arquivo["mensagens"]:
            dados = para_evento(bruta, lida)
            if bruta["message_type"] == 2 or bruta["private"]:
                virou_nada = dados is None
                assert virou_nada, "atividade ou nota privada virou evento"
                continue
            virou_evento = dados is not None
            assert virou_evento, "mensagem da amostra não virou evento"
            esperadas += 1
            evento = ler_evento(dados)
            foi_lido = evento is not None
            assert foi_lido, "o evento do adaptador não foi lido"
            entregues += 1
            eventos[(evento.tipo, evento.remetente)] += 1
            conversa_certa = evento.conversa_chatwoot_id == lida.id
            assert conversa_certa, "evento na conversa errada"
            id_certo = evento.mensagem_id == str(bruta["id"])
            assert id_certo, "evento com o id de outra mensagem"
            texto_e_str = isinstance(evento.texto, str)
            assert texto_e_str, "texto do evento não é string"
            if evento.tipo in ("incoming", "outgoing"):
                # O atendente também leva o telefone do contato (a cobrança se resolve por ele).
                telefone_certo = evento.telefone == lida.telefone
                assert telefone_certo, "evento com o telefone de outra conversa"
            anexos_da_api += len(bruta.get("attachments") or [])
            anexos_lidos += len(evento.anexos)
            tipos_conhecidos = {a["tipo"] for a in evento.anexos} <= TIPOS_DE_ANEXO
            assert tipos_conhecidos, "tipo de anexo desconhecido"

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
