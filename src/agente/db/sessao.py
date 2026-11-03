"""Motor e fábrica de sessões do banco."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def criar_motor(database_url: str, ocultar_parametros: bool = False) -> Engine:
    """`ocultar_parametros` tira os valores do SQL da mensagem de erro (`[parameters: ...]`). A
    reprodução da pré-sombra o liga: os parâmetros são o texto das conversas dos clientes. No
    serviço fica desligado, porque lá o parâmetro ajuda a diagnosticar."""
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True, hide_parameters=ocultar_parametros)
    argumentos: dict = {
        "connect_args": {"check_same_thread": False},
        "hide_parameters": ocultar_parametros,
    }
    if database_url in ("sqlite://", "sqlite:///:memory:"):
        # Em memória, todas as sessões têm de ver o mesmo banco.
        argumentos["poolclass"] = StaticPool
    motor = create_engine(database_url, **argumentos)
    event.listen(motor, "connect", _ligar_chaves_estrangeiras)
    return motor


def _ligar_chaves_estrangeiras(conexao, _registro) -> None:
    cursor = conexao.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def criar_sessao(database_url: str, ocultar_parametros: bool = False) -> sessionmaker[Session]:
    motor = criar_motor(database_url, ocultar_parametros)
    return sessionmaker(bind=motor, expire_on_commit=False)
