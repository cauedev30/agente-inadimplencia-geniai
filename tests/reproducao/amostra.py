"""A amostra real do Chatwoot (30 conversas), que fica fora do repositório (LGPD).

Os testes que a usam só conferem a estrutura e as contagens, nunca o conteúdo, e se pulam sozinhos
quando a pasta não existe (no GitHub e na máquina do outro dev). `AGENTE_AMOSTRA_CHATWOOT` troca a
pasta.
"""

import json
import os
from pathlib import Path

# repo/tests/reproducao/amostra.py -> a raiz do workspace fica três pastas acima do `repo/`.
RAIZ_DO_WORKSPACE = Path(__file__).resolve().parents[3]


def pasta_da_amostra() -> Path:
    return Path(
        os.environ.get("AGENTE_AMOSTRA_CHATWOOT") or RAIZ_DO_WORKSPACE / "pre-sombra" / "amostra"
    )


def ler_amostra() -> list[dict]:
    """Cada arquivo é `{"conversa": {...}, "mensagens": [...]}`. Sem a pasta, pula o teste."""
    import pytest

    pasta = pasta_da_amostra()
    arquivos = sorted(pasta.glob("*.json")) if pasta.is_dir() else []
    if not arquivos:
        pytest.skip("a amostra real do Chatwoot não está nesta máquina")
    return [json.loads(arquivo.read_text(encoding="utf-8")) for arquivo in arquivos]
