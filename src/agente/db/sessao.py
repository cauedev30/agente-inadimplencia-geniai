"""Motor e fábrica de sessões do banco."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def criar_motor(database_url: str) -> Engine:
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)
    argumentos: dict = {"connect_args": {"check_same_thread": False}}
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


def criar_sessao(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=criar_motor(database_url), expire_on_commit=False)
