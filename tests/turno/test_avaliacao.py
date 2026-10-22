"""A avaliação do turno e o laudo que causou a saída para o humano (auditoria)."""

from agente.db.modelos import Cobranca, Conversa
from agente.integracoes.jev import EstadoJev, JevFalso, JevIndisponivel, responder
from agente.politica import Regua
from agente.tipos import BatchJev, EstadoConversa, Laudo, MotivoHumano
from agente.turno.avaliacao import avaliar

ESTADO = EstadoJev((("CLIENTE", "oi"),), Regua.R3, 10020, EstadoConversa.ENGAJADO, None)


class SoJev1(JevFalso):
    """O Jev 1 responde, e o Jev 2 fica indisponível."""

    def avaliar(self, perguntas, estado):
        if perguntas[0].nome == "acao":
            raise JevIndisponivel("o Jev passou de 2000 ms")
        return super().avaliar(perguntas, estado)


def _avaliar(politica, jev, regua=Regua.R3, objecoes=0):
    cobranca = Cobranca(regua=regua, divida_centavos=10020, propenso_cartao=False)
    conversa = Conversa(degrau_atual=0, objecoes_sem_desconto=objecoes)
    return avaliar(jev, ESTADO, conversa, cobranca, politica)


def test_causa_do_gatilho_e_o_laudo_do_jev1(politica):
    avaliacao = _avaliar(politica, JevFalso([responder("risco_juridico", True, 0.91, 0.80)]))
    assert avaliacao.causa == (BatchJev.JEV1, Laudo("risco_juridico", True, 0.91, 0.80))


def test_jev2_indisponivel_nao_tem_causa(politica):
    avaliacao = _avaliar(politica, SoJev1())
    assert avaliacao.motivo is MotivoHumano.JEV_INDISPONIVEL
    assert avaliacao.leitura is not None
    assert avaliacao.causa is None


def test_objecao_sem_desconto_aponta_o_laudo_da_objecao(politica):
    jev = JevFalso([responder("objecao_valor", True, 0.88, 0.77), responder("acao", "INFORM_DEBT")])
    avaliacao = _avaliar(politica, jev, Regua.NR, objecoes=1)
    assert avaliacao.motivo is MotivoHumano.OBJECAO_SEM_DESCONTO
    assert avaliacao.causa == (BatchJev.JEV1, Laudo("objecao_valor", True, 0.88, 0.77))


def test_baixa_confianca_aponta_o_laudo_do_jev2(politica):
    avaliacao = _avaliar(politica, JevFalso([responder("acao", "GENERATE_PIX", 0.80, 0.9)]))
    assert avaliacao.motivo is MotivoHumano.BAIXA_CONFIANCA
    assert avaliacao.causa == (BatchJev.JEV2, Laudo("acao", "GENERATE_PIX", 0.80, 0.9))


def test_em_risco_sem_melhora_vai_ao_humano_com_o_laudo_do_risco(politica):
    cobranca = Cobranca(regua=Regua.R3, divida_centavos=10020, propenso_cartao=False)
    conversa = Conversa(estado=EstadoConversa.EM_RISCO, degrau_atual=0, objecoes_sem_desconto=0)
    jev = JevFalso([responder("repete_sem_avancar", True, 0.81, 0.7)])

    avaliacao = avaliar(jev, ESTADO, conversa, cobranca, politica)

    assert (avaliacao.motivo, avaliacao.decisao) == (MotivoHumano.SEM_MELHORA, None)
    assert avaliacao.causa == (BatchJev.JEV1, Laudo("repete_sem_avancar", True, 0.81, 0.7))


def test_fora_do_risco_o_mesmo_sinal_so_marca_o_turno(politica):
    avaliacao = _avaliar(politica, JevFalso([responder("tom", "impaciente")]))
    assert avaliacao.motivo is None
    assert avaliacao.em_risco is True
