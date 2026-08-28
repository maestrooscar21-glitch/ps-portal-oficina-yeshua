import html
import io
import hashlib
import re
import unicodedata
import uuid
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import streamlit as st
from supabase import Client, create_client


# =========================================================
# CONFIGURAÇÃO
# =========================================================

st.set_page_config(
    page_title="Operações de Campo PS",
    page_icon="🚛",
    layout="wide",
)


# Cache global — precisa existir antes dos decorators
CACHE_TTL_SEGUNDOS = 60

CONSULTORES = [
    "Não definido",
    "Oscar Barbosa",
    "Paulo Castro",
    "Fábio Silva",
    "Fábio Silva*",
    "Marcos Bispo",
    "Roberto Rugel",
    "Gleci Nunes",
]

MAPA_CONSULTORES_UF = {
    "AL": "Oscar Barbosa",
    "BA": "Oscar Barbosa",
    "CE": "Oscar Barbosa",
    "MA": "Oscar Barbosa",
    "PB": "Oscar Barbosa",
    "PE": "Oscar Barbosa",
    "PI": "Oscar Barbosa",
    "RN": "Oscar Barbosa",
    "SE": "Oscar Barbosa",
    "SP": "Paulo Castro",
    "RJ": "Paulo Castro",
    "ES": "Paulo Castro",
    "MG": "Fábio Silva",
    "AC": "Fábio Silva*",
    "AM": "Fábio Silva*",
    "AP": "Fábio Silva*",
    "PA": "Fábio Silva*",
    "RO": "Fábio Silva*",
    "RR": "Fábio Silva*",
    "TO": "Fábio Silva*",
    "PR": "Roberto Rugel",
    "RS": "Marcos Bispo",
    "SC": "Marcos Bispo",
    "DF": "Gleci Nunes",
    "GO": "Gleci Nunes",
    "MT": "Gleci Nunes",
    "MS": "Gleci Nunes",
}

REGIOES_CONSULTORES = {
    "Oscar Barbosa": "Nordeste",
    "Paulo Castro": "Sudeste",
    "Fábio Silva": "Minas Gerais",
    "Fábio Silva*": "Norte",
    "Marcos Bispo": "Rio Grande do Sul / Santa Catarina",
    "Roberto Rugel": "Paraná",
    "Gleci Nunes": "Centro-Oeste",
    "Não definido": "Não definida",
}

TABELA_POR_TIPO = {
    "planejado": "atividades_planejadas",
    "resultado": "atividades_resultado",
}

FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")
DATA_CORTE_NOVA_REGRA = "2026-08-08"


# =========================================================
# SUPABASE
# =========================================================

def obter_supabase() -> tuple[Client | None, str | None]:
    """Cria a conexão e devolve também uma mensagem de erro legível."""
    try:
        url = str(st.secrets.get("SUPABASE_URL", "")).strip()
        chave = str(
            st.secrets.get(
                "SUPABASE_SERVICE_KEY",
                st.secrets.get("SUPABASE_KEY", ""),
            )
        ).strip()

        url = url.replace("/rest/v1/", "").replace("/rest/v1", "").rstrip("/")

        if not url or not chave:
            return None, (
                "Configure SUPABASE_URL e SUPABASE_SERVICE_KEY "
                "nos Secrets do Streamlit."
            )

        cliente = create_client(url, chave)

        # Teste simples da conexão.
        cliente.table("bases_importadas").select("id").limit(1).execute()
        return cliente, None

    except Exception as erro:
        return None, f"Falha na conexão com o Supabase: {erro}"


SUPABASE, ERRO_SUPABASE = obter_supabase()


def exigir_supabase() -> Client:
    if SUPABASE is None:
        st.error(ERRO_SUPABASE or "Supabase não conectado.")
        st.stop()

    return SUPABASE


def buscar_todos(
    tabela: str,
    colunas: str = "*",
    filtros: dict[str, Any] | None = None,
    ordem: str | None = None,
    desc: bool = False,
    tamanho_pagina: int = 1000,
) -> list[dict]:
    """Busca todos os registros, inclusive quando houver mais de 1.000 linhas."""
    cliente = exigir_supabase()
    registros: list[dict] = []
    inicio = 0

    while True:
        consulta = cliente.table(tabela).select(colunas)

        for coluna, valor in (filtros or {}).items():
            consulta = consulta.eq(coluna, valor)

        if ordem:
            consulta = consulta.order(ordem, desc=desc)

        resposta = consulta.range(
            inicio,
            inicio + tamanho_pagina - 1,
        ).execute()

        lote = resposta.data or []
        registros.extend(lote)

        if len(lote) < tamanho_pagina:
            break

        inicio += tamanho_pagina

    return registros


def inserir_em_lotes(
    tabela: str,
    registros: list[dict],
    tamanho_lote: int = 300,
) -> None:
    cliente = exigir_supabase()

    for inicio in range(0, len(registros), tamanho_lote):
        lote = registros[inicio : inicio + tamanho_lote]
        cliente.table(tabela).insert(lote).execute()



# =========================================================
# PRIVACIDADE / MINIMIZAÇÃO DE DADOS
# =========================================================

# Campos pessoais que não são necessários para Planejamento, Resultado,
# Pré-agenda ou Estoque não devem ser persistidos dentro do JSON bruto.
# A lista é deliberadamente conservadora e NÃO remove Recurso/Técnico
# das bases operacionais, pois esses campos ainda sustentam funcionalidades
# do painel (pré-agenda e revisão MD). No módulo de estoque, Técnico não é
# armazenado em nenhum campo.
CAMPOS_PESSOAIS_BLOQUEADOS = (
    "CPF",
    "RG",
    "CNH",
    "TELEFONE",
    "CELULAR",
    "WHATSAPP",
    "E-MAIL",
    "EMAIL",
)


def coluna_pessoal_bloqueada(nome_coluna: str) -> bool:
    nome = normalizar_texto(nome_coluna)
    return any(termo in nome for termo in CAMPOS_PESSOAIS_BLOQUEADOS)


def dados_sanitizados_linha(
    linha: pd.Series,
    excluir_adicionais: set[str] | None = None,
) -> dict:
    """Gera o JSON persistido removendo identificadores pessoais desnecessários."""
    excluir = {
        "Chave Ticket",
        "Chave Placa",
        "Chave OS",
        "Chave Atendimento",
    }
    excluir.update(excluir_adicionais or set())

    return {
        str(coluna): texto_limpo(valor)
        for coluna, valor in linha.items()
        if str(coluna) not in excluir
        and not coluna_pessoal_bloqueada(str(coluna))
    }


def colunas_pessoais_detectadas(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty:
        return []
    return [
        str(coluna)
        for coluna in df.columns
        if coluna_pessoal_bloqueada(str(coluna))
    ]


# =========================================================
# ESTOQUE — CONSUMO OFS / CATÁLOGO
# =========================================================

def ler_csv_equipamentos_ofs(arquivo) -> pd.DataFrame:
    """Lê o relatório Equipamentos Trocados do OFS sem persistir dados de técnico."""
    conteudo = arquivo.getvalue()
    ultimo_erro = None
    for encoding in ["utf-8-sig", "utf-8", "latin-1"]:
        for separador in [",", ";"]:
            try:
                df = pd.read_csv(
                    io.BytesIO(conteudo), encoding=encoding, sep=separador,
                    dtype=str, low_memory=False,
                )
                df = padronizar_colunas(df)
                if len(df.columns) <= 1:
                    continue
                obrigatorias = [
                    "OS", "Qde", "Cód. Equip.", "Nome Equip.", "Execução",
                    "Oficina", "Garantia", "Sinistro", "Motivo Envio", "Cliente",
                ]
                faltantes = [c for c in obrigatorias if c not in df.columns]
                if faltantes:
                    continue
                return limpar_colunas_texto(df, [c for c in df.columns if c != "Qde"])
            except Exception as erro:
                ultimo_erro = erro
    raise ValueError(
        "Arquivo não reconhecido como 'Equipamentos Trocados — OFS'. "
        f"Último erro: {ultimo_erro}"
    )


def _numero_estoque(valor) -> float:
    texto = texto_limpo(valor).replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except Exception:
        return 0.0


def _hash_sha256_bytes(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def _chave_consumo_ofs(linha: pd.Series) -> str:
    # Hash determinístico da linha operacional. O campo Técnico é deliberadamente excluído.
    campos = [
        texto_limpo(linha.get("OS", "")),
        texto_limpo(linha.get("Cód. Equip.", "")),
        texto_limpo(linha.get("Nome Equip.", "")),
        texto_limpo(linha.get("Execução", "")),
        texto_limpo(linha.get("Oficina", "")),
        texto_limpo(linha.get("Qde", "")),
        texto_limpo(linha.get("Garantia", "")),
        texto_limpo(linha.get("Sinistro", "")),
        texto_limpo(linha.get("Motivo Envio", "")),
        texto_limpo(linha.get("Cliente", "")),
    ]
    return hashlib.sha256("||".join(campos).encode("utf-8")).hexdigest()


def preparar_consumos_ofs(df: pd.DataFrame, importacao_id: int | None = None) -> list[dict]:
    registros = []
    for _, linha in df.iterrows():
        quantidade = _numero_estoque(linha.get("Qde", ""))
        codigo = texto_limpo(linha.get("Cód. Equip.", ""))
        data_execucao = pd.to_datetime(linha.get("Execução", ""), dayfirst=True, errors="coerce")
        if not codigo or quantidade <= 0 or pd.isna(data_execucao):
            continue
        dados_originais = {
            "OS": texto_limpo(linha.get("OS", "")),
            "Qde": texto_limpo(linha.get("Qde", "")),
            "Cód. Equip.": codigo,
            "Nome Equip.": texto_limpo(linha.get("Nome Equip.", "")),
            "Execução": texto_limpo(linha.get("Execução", "")),
            "Oficina": texto_limpo(linha.get("Oficina", "")),
            "Garantia": texto_limpo(linha.get("Garantia", "")),
            "Sinistro": texto_limpo(linha.get("Sinistro", "")),
            "Motivo Envio": texto_limpo(linha.get("Motivo Envio", "")),
            "Cliente": texto_limpo(linha.get("Cliente", "")),
        }
        registros.append({
            "os": texto_limpo(linha.get("OS", "")),
            "data_execucao": data_execucao.date().isoformat(),
            "codigo_item": codigo,
            "nome_item": texto_limpo(linha.get("Nome Equip.", "")),
            "quantidade": quantidade,
            "oficina": texto_limpo(linha.get("Oficina", "")),
            "garantia": texto_limpo(linha.get("Garantia", "")),
            "sinistro": texto_limpo(linha.get("Sinistro", "")),
            "motivo_envio": texto_limpo(linha.get("Motivo Envio", "")),
            "cliente": texto_limpo(linha.get("Cliente", "")),
            "chave_consumo": _chave_consumo_ofs(linha),
            "importacao_id": importacao_id,
            "dados_originais": dados_originais,
        })
    return registros


def salvar_consumos_ofs(arquivo, df: pd.DataFrame) -> dict:
    cliente = exigir_supabase()
    conteudo = arquivo.getvalue()
    hash_arquivo = _hash_sha256_bytes(conteudo)

    existente = (
        cliente.table("estoque_importacoes")
        .select("id,status")
        .eq("hash_arquivo", hash_arquivo)
        .limit(1).execute().data or []
    )
    if existente:
        return {"duplicado": True, "importados": 0, "ignorados": len(df), "catalogo": 0}

    datas = pd.to_datetime(df.get("Execução", pd.Series(dtype=str)), dayfirst=True, errors="coerce")
    data_ref = datas.max().date().isoformat() if datas.notna().any() else None
    cabecalho = {
        "fonte": "OFS",
        "nome_arquivo": arquivo.name,
        "data_referencia": data_ref,
        "quantidade_linhas": int(len(df)),
        "quantidade_importada": 0,
        "quantidade_ignorada": 0,
        "hash_arquivo": hash_arquivo,
        "status": "PROCESSANDO",
        "observacoes": "Equipamentos Trocados — OFS. Técnico não armazenado.",
    }
    resposta = cliente.table("estoque_importacoes").insert(cabecalho).execute()
    importacao_id = int(resposta.data[0]["id"])

    try:
        # Catálogo: código + nomenclatura; demais atributos serão enriquecidos depois.
        catalogo = (
            df[["Cód. Equip.", "Nome Equip."]].copy()
            .rename(columns={"Cód. Equip.": "codigo_item", "Nome Equip.": "nome_item"})
        )
        catalogo["codigo_item"] = catalogo["codigo_item"].apply(texto_limpo)
        catalogo["nome_item"] = catalogo["nome_item"].apply(texto_limpo)
        catalogo = catalogo[catalogo["codigo_item"] != ""].drop_duplicates("codigo_item", keep="last")
        registros_catalogo = catalogo.to_dict("records")
        for inicio in range(0, len(registros_catalogo), 300):
            cliente.table("estoque_catalogo_itens").upsert(
                registros_catalogo[inicio:inicio+300], on_conflict="codigo_item"
            ).execute()

        registros = preparar_consumos_ofs(df, importacao_id)
        chaves = [r["chave_consumo"] for r in registros]
        existentes = set()
        for inicio in range(0, len(chaves), 200):
            lote = chaves[inicio:inicio+200]
            if lote:
                dados = cliente.table("estoque_consumos_ofs").select("chave_consumo").in_("chave_consumo", lote).execute().data or []
                existentes.update(texto_limpo(x.get("chave_consumo")) for x in dados)
        novos = [r for r in registros if r["chave_consumo"] not in existentes]
        inserir_em_lotes("estoque_consumos_ofs", novos)
        ignorados = len(df) - len(novos)
        cliente.table("estoque_importacoes").update({
            "quantidade_importada": len(novos),
            "quantidade_ignorada": ignorados,
            "status": "PROCESSADO",
        }).eq("id", importacao_id).execute()
        st.cache_data.clear()
        return {"duplicado": False, "importados": len(novos), "ignorados": ignorados, "catalogo": len(registros_catalogo)}
    except Exception as erro:
        cliente.table("estoque_importacoes").update({
            "status": "ERRO", "observacoes": f"Falha na importação: {erro}"
        }).eq("id", importacao_id).execute()
        raise


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_consumos_estoque() -> pd.DataFrame:
    registros = buscar_todos("estoque_consumos_ofs", ordem="data_execucao", desc=True)
    if not registros:
        return pd.DataFrame()
    df = pd.DataFrame(registros)
    df["data_execucao"] = pd.to_datetime(df["data_execucao"], errors="coerce")
    df["quantidade"] = pd.to_numeric(df["quantidade"], errors="coerce").fillna(0)
    return df


def classificar_curva_abc(consumo_item: pd.DataFrame) -> pd.DataFrame:
    base = consumo_item.copy().sort_values("Quantidade", ascending=False).reset_index(drop=True)
    total = float(base["Quantidade"].sum())
    if total <= 0:
        base["Curva ABC"] = "C"
        return base
    base["% acumulado"] = base["Quantidade"].cumsum() / total * 100
    # Classificação inicial operacional: A até 80%, B até 95%, C restante.
    base["Curva ABC"] = base["% acumulado"].apply(lambda x: "A" if x <= 80 else ("B" if x <= 95 else "C"))
    return base


# =========================================================
# REVISÃO GERENCIAL EXCLUSIVA DA MD
# =========================================================

@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_revisoes_md() -> pd.DataFrame:
    registros = buscar_todos(
        "revisoes_md_improdutivas",
        ordem="atualizado_em",
        desc=True,
    )

    if not registros:
        return pd.DataFrame(
            columns=[
                "data_operacional",
                "chave_atendimento",
                "motivo_original",
                "classificacao_md",
                "motivo_md_revisado",
                "justificativa",
                "revisor",
                "criado_em",
                "atualizado_em",
            ]
        )

    return pd.DataFrame(registros)


def salvar_revisao_md(
    data_operacional: str,
    chave_atendimento: str,
    motivo_original: str,
    classificacao_md: str,
    motivo_md_revisado: str,
    justificativa: str,
    revisor: str,
) -> None:
    cliente = exigir_supabase()

    registro = {
        "data_operacional": data_operacional,
        "chave_atendimento": chave_atendimento,
        "motivo_original": motivo_original,
        "classificacao_md": classificacao_md,
        "motivo_md_revisado": motivo_md_revisado,
        "justificativa": justificativa,
        "revisor": revisor,
        "atualizado_em": datetime.now(FUSO_BRASIL).isoformat(),
    }

    cliente.table(
        "revisoes_md_improdutivas"
    ).upsert(
        registro,
        on_conflict="data_operacional,chave_atendimento",
    ).execute()

    st.cache_data.clear()


def excluir_revisao_md(
    data_operacional: str,
    chave_atendimento: str,
) -> None:
    cliente = exigir_supabase()

    cliente.table(
        "revisoes_md_improdutivas"
    ).delete().eq(
        "data_operacional",
        data_operacional,
    ).eq(
        "chave_atendimento",
        chave_atendimento,
    ).execute()

    st.cache_data.clear()


def aplicar_revisoes_md(
    base: pd.DataFrame,
) -> pd.DataFrame:
    """
    Camada gerencial exclusiva da MD.
    Não altera o dado original do OFS nem a MCI.
    """
    if base is None or base.empty:
        return base

    resultado = base.copy()

    resultado["Razao_improdutiva_md"] = resultado.get(
        "Razao_improdutiva",
        pd.Series("", index=resultado.index, dtype=str),
    ).apply(texto_limpo)

    resultado["Classificacao_gerencial_MD"] = "Improdutiva"
    resultado["Revisao_MD"] = False
    resultado["Justificativa_revisao_MD"] = ""
    resultado["Revisor_MD"] = ""

    revisoes = carregar_revisoes_md()

    if revisoes.empty:
        return resultado

    mapa = {
        (
            texto_limpo(linha.get("data_operacional", "")),
            texto_limpo(linha.get("chave_atendimento", "")),
        ): linha
        for _, linha in revisoes.iterrows()
    }

    for indice, linha in resultado.iterrows():
        chave = (
            texto_limpo(linha.get("Data Operacional", "")),
            texto_limpo(linha.get("Chave Atendimento", "")),
        )

        revisao = mapa.get(chave)
        if revisao is None:
            continue

        classificacao = (
            texto_limpo(revisao.get("classificacao_md", ""))
            or "Improdutiva"
        )
        motivo = texto_limpo(
            revisao.get("motivo_md_revisado", "")
        )

        resultado.at[
            indice, "Classificacao_gerencial_MD"
        ] = classificacao

        if motivo:
            resultado.at[
                indice, "Razao_improdutiva_md"
            ] = motivo

        resultado.at[indice, "Revisao_MD"] = True
        resultado.at[
            indice, "Justificativa_revisao_MD"
        ] = texto_limpo(
            revisao.get("justificativa", "")
        )
        resultado.at[
            indice, "Revisor_MD"
        ] = texto_limpo(
            revisao.get("revisor", "")
        )

    return resultado


# =========================================================
# LIMPEZA E LEITURA
# =========================================================

def texto_limpo(valor) -> str:
    if valor is None:
        return ""

    try:
        if pd.isna(valor):
            return ""
    except (TypeError, ValueError):
        pass

    texto = str(valor).strip()

    if texto.lower() in {"nan", "none", "null", "nat"}:
        return ""

    return texto


def normalizar_texto(valor) -> str:
    texto = texto_limpo(valor).upper()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def padronizar_uf(valor) -> str:
    return re.sub(r"[^A-Z]", "", normalizar_texto(valor))[:2]


def limpar_telefone(valor) -> str:
    numeros = re.sub(r"\D", "", texto_limpo(valor))

    if numeros.startswith("0") and len(numeros) > 10:
        numeros = numeros[1:]

    if numeros and not numeros.startswith("55"):
        numeros = f"55{numeros}"

    return numeros


def extrair_primeiro_celular(valor) -> str:
    texto = texto_limpo(valor)

    candidatos = re.findall(
        r"(?:\+?55[\s.-]*)?"
        r"(?:\(?\d{2}\)?[\s.-]*)?"
        r"9\d{4}[\s.-]?\d{4}",
        texto,
    )

    for candidato in candidatos:
        telefone = limpar_telefone(candidato)

        if len(telefone) in {12, 13}:
            return telefone

    return ""


def padronizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = df.columns.astype(str).str.strip()
    return df


def limpar_colunas_texto(
    df: pd.DataFrame,
    colunas: list[str],
) -> pd.DataFrame:
    df = df.copy()

    for coluna in colunas:
        if coluna in df.columns:
            df[coluna] = df[coluna].apply(texto_limpo)

    return df


def contar_unicos(df: pd.DataFrame, coluna: str) -> int:
    if df is None or coluna not in df.columns:
        return 0

    serie = df[coluna].apply(texto_limpo)
    return int(serie[serie != ""].nunique())


def ler_csv_ofs(arquivo) -> pd.DataFrame:
    conteudo = arquivo.getvalue()

    tentativas = [
        ("utf-8-sig", ","),
        ("utf-8", ","),
        ("latin-1", ","),
        ("utf-8-sig", ";"),
        ("utf-8", ";"),
        ("latin-1", ";"),
    ]

    ultimo_erro = None

    for encoding, separador in tentativas:
        try:
            df = pd.read_csv(
                io.BytesIO(conteudo),
                encoding=encoding,
                sep=separador,
                dtype=str,
                low_memory=False,
            )

            if len(df.columns) > 1:
                return limpar_colunas_texto(
                    padronizar_colunas(df),
                    [
                        "ID da Atividade",
                        "Ticket Jira",
                        "OS",
                        "Placa",
                        "Oficina",
                        "Cliente",
                        "Estado",
                        "Cidade",
                        "Tipo de Atividade",
                        "Status da Atividade",
                        "Data",
                        "Recurso",
                    ],
                )

        except Exception as erro:
            ultimo_erro = erro

    raise ValueError(
        f"Não foi possível ler o CSV. Último erro: {ultimo_erro}"
    )


def ler_cadastro_oficinas(arquivo) -> pd.DataFrame:
    nome = arquivo.name.lower()
    conteudo = arquivo.getvalue()

    if nome.endswith(".ods"):
        df = pd.read_excel(
            io.BytesIO(conteudo),
            engine="odf",
            dtype=str,
        )
    elif nome.endswith(".xlsx"):
        df = pd.read_excel(
            io.BytesIO(conteudo),
            engine="openpyxl",
            dtype=str,
        )
    elif nome.endswith(".csv"):
        return ler_csv_ofs(arquivo)
    else:
        raise ValueError("Formato não permitido. Use ODS, XLSX ou CSV.")

    return padronizar_colunas(df)


def localizar_coluna(
    df: pd.DataFrame,
    possibilidades: list[str],
):
    mapa = {
        normalizar_texto(coluna): coluna
        for coluna in df.columns
    }

    for possibilidade in possibilidades:
        chave = normalizar_texto(possibilidade)

        if chave in mapa:
            return mapa[chave]

    return None


def serie_coluna(
    df: pd.DataFrame,
    coluna,
    valor_padrao="",
) -> pd.Series:
    if coluna is None:
        return pd.Series(
            [valor_padrao] * len(df),
            index=df.index,
        )

    return df[coluna].apply(texto_limpo)


def preparar_cadastro(df_original: pd.DataFrame) -> pd.DataFrame:
    df = df_original.copy()

    coluna_id = localizar_coluna(
        df,
        ["ID", "ID Oficina", "Código", "Codigo"],
    )
    coluna_nome = localizar_coluna(
        df,
        ["Nome Fantasia", "Oficina", "Nome da Oficina", "Nome"],
    )
    coluna_cidade = localizar_coluna(
        df,
        ["Cidade-base", "Cidade Base", "Cidade", "Município", "Municipio"],
    )
    coluna_uf = localizar_coluna(
        df,
        ["UF-base", "UF Base", "UF", "Estado"],
    )
    coluna_contatos = localizar_coluna(
        df,
        [
            "Contatos originais",
            "Contatos",
            "Contato",
            "Telefones",
            "Telefone",
            "Celular",
        ],
    )
    coluna_whatsapp = localizar_coluna(
        df,
        ["WhatsApp", "Whatsapp", "Whats App"],
    )
    coluna_consultor = localizar_coluna(
        df,
        ["Consultor", "Consultor responsável", "Consultor Responsavel"],
    )
    coluna_prioridade = localizar_coluna(df, ["Prioridade"])
    coluna_status = localizar_coluna(df, ["Ativa", "Ativa?", "Status"])
    coluna_observacoes = localizar_coluna(
        df,
        ["Observações", "Observacoes", "Observação", "Observacao"],
    )

    if coluna_nome is None:
        raise ValueError("Não encontrei a coluna com o nome da oficina.")

    cadastro = pd.DataFrame(index=df.index)
    cadastro["ID"] = serie_coluna(df, coluna_id)
    cadastro["Oficina"] = serie_coluna(df, coluna_nome)
    cadastro["Cidade-base"] = serie_coluna(df, coluna_cidade)
    cadastro["UF-base"] = serie_coluna(df, coluna_uf).apply(padronizar_uf)

    cadastro["Consultor automático"] = (
        cadastro["UF-base"]
        .map(MAPA_CONSULTORES_UF)
        .fillna("Não definido")
    )

    cadastro["Consultor"] = serie_coluna(df, coluna_consultor)
    cadastro.loc[
        ~cadastro["Consultor"].isin(CONSULTORES),
        "Consultor",
    ] = ""
    cadastro.loc[
        cadastro["Consultor"] == "",
        "Consultor",
    ] = cadastro["Consultor automático"]

    cadastro["Contatos originais"] = serie_coluna(df, coluna_contatos)
    cadastro["WhatsApp"] = (
        serie_coluna(df, coluna_whatsapp)
        .apply(limpar_telefone)
    )
    whatsapp_extraido = cadastro["Contatos originais"].apply(
        extrair_primeiro_celular
    )
    cadastro.loc[
        cadastro["WhatsApp"] == "",
        "WhatsApp",
    ] = whatsapp_extraido

    cadastro["Prioridade"] = serie_coluna(
        df,
        coluna_prioridade,
        "Normal",
    )
    cadastro.loc[
        ~cadastro["Prioridade"].isin(["Alta", "Normal", "Baixa"]),
        "Prioridade",
    ] = "Normal"

    cadastro["Ativa"] = serie_coluna(df, coluna_status, "Sim")
    cadastro["Observações"] = serie_coluna(df, coluna_observacoes)
    cadastro["Chave Oficina"] = cadastro["Oficina"].apply(normalizar_texto)

    cadastro = cadastro[
        cadastro["Chave Oficina"] != ""
    ].drop_duplicates(
        subset=["Chave Oficina"],
        keep="first",
    )

    return cadastro.sort_values("Oficina").reset_index(drop=True)


def filtrar_somente_manutencoes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Mantém somente atividades cujo Tipo de Atividade contenha
    a palavra MANUTEN, cobrindo manutenção, manutenções,
    manutenção corretiva, preventiva e demais variações.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=df.columns if df is not None else [])

    if "Tipo de Atividade" not in df.columns:
        raise ValueError(
            "A base não possui a coluna 'Tipo de Atividade'. "
            "Não foi possível filtrar somente manutenções."
        )

    base = df.copy()

    mascara = base["Tipo de Atividade"].apply(
        lambda valor: "MANUTEN" in normalizar_texto(valor)
    )

    return base[mascara].copy().reset_index(drop=True)


# =========================================================
# CHAVES, CONCILIAÇÃO E INDICADORES
# =========================================================

def criar_chaves(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for coluna in ["Ticket Jira", "Placa", "OS"]:
        if coluna not in df.columns:
            df[coluna] = ""

        df[coluna] = df[coluna].apply(texto_limpo)

    df["Chave Ticket"] = df["Ticket Jira"].apply(normalizar_texto)
    df["Chave Placa"] = df["Placa"].apply(normalizar_texto)
    df["Chave OS"] = df["OS"].apply(normalizar_texto)

    # Os indicadores do painel são baseados em OS.
    # Quando houver OS, ela será a chave principal. A regra anterior
    # usava Ticket + Placa e podia agrupar OS diferentes.
    possui_os = df["Chave OS"] != ""

    df["Chave Atendimento"] = ""

    df.loc[possui_os, "Chave Atendimento"] = (
        "OS|"
        + df.loc[possui_os, "Chave OS"]
        + "|"
        + df.loc[possui_os, "Chave Placa"]
    )

    sem_os = ~possui_os

    df.loc[sem_os, "Chave Atendimento"] = (
        "TICKET|"
        + df.loc[sem_os, "Chave Ticket"]
        + "|"
        + df.loc[sem_os, "Chave Placa"]
    )

    sem_identificador = (
        (df["Chave Ticket"] == "")
        & (df["Chave OS"] == "")
        & (df["Chave Placa"] == "")
    )

    df.loc[sem_identificador, "Chave Atendimento"] = (
        "LINHA|" + df.index[sem_identificador].astype(str)
    )

    return df


def status_normalizado(valor) -> str:
    return normalizar_texto(valor)


def status_executado(valor) -> bool:
    status = status_normalizado(valor)
    return any(
        termo in status
        for termo in [
            "CONCLUID",
            "EXECUTAD",
            "FINALIZAD",
            "COMPLET",
            "REALIZAD",
        ]
    )


def status_improdutivo(valor) -> bool:
    status = status_normalizado(valor)
    return any(
        termo in status
        for termo in [
            "NAO CONCLUIDO",
            "NAO CONCLUIDA",
            "IMPRODUTIVO",
            "IMPRODUTIVA",
            "SEM SUCESSO",
        ]
    )


def status_cancelado(valor) -> bool:
    return "CANCEL" in status_normalizado(valor)


def juntar_unicos(valores) -> str:
    itens = sorted(
        {
            texto_limpo(valor)
            for valor in valores
            if texto_limpo(valor)
        }
    )
    return " | ".join(itens)


def conciliar_bases(
    planejado: pd.DataFrame,
    resultado: pd.DataFrame,
) -> pd.DataFrame:
    """
    Regra híbrida:
    - datas anteriores a DATA_CORTE_NOVA_REGRA usam a lógica histórica;
    - a partir da data de corte, usa primeira aparição da OS para
      classificar Agendada x Extra/Encaixe.
    """
    planejado = filtrar_somente_manutencoes(planejado)
    resultado = filtrar_somente_manutencoes(resultado)

    planejado = criar_chaves(planejado)
    resultado = criar_chaves(resultado)

    if "Status da Atividade" not in resultado.columns:
        resultado["Status da Atividade"] = ""

    if "Status da Atividade" not in planejado.columns:
        planejado["Status da Atividade"] = ""

    # Compatibilidade com todo o histórico já salvo:
    # se uma importação antiga não tiver estes campos, o painel continua
    # funcionando e exibe o detalhe como vazio, sem alterar os indicadores.
    if "Razão da Improdutiva" not in resultado.columns:
        resultado["Razão da Improdutiva"] = ""

    if "Observação do Técnico (Improdutiva)" not in resultado.columns:
        resultado["Observação do Técnico (Improdutiva)"] = ""

    # Campos usados na auditoria de conformidade temporal das improdutivas.
    for coluna_auditoria in [
        "Intervalo de Tempo",
        "Janela de Serviço",
        "Janela de Serviço.1",
        "Início",
        "Fim",
        "Duração",
        "Recurso",
        "Cliente",
        "Cidade",
    ]:
        if coluna_auditoria not in resultado.columns:
            resultado[coluna_auditoria] = ""

    if "__Ativa no Planejamento" not in planejado.columns:
        planejado["__Ativa no Planejamento"] = True

    if "__Planejamento Base" not in planejado.columns:
        planejado["__Planejamento Base"] = False

    if "__Primeira Aparição Data" not in planejado.columns:
        planejado["__Primeira Aparição Data"] = ""

    if "__Data Operacional" not in planejado.columns:
        planejado["__Data Operacional"] = ""

    resumo_resultado = (
        resultado
        .groupby("Chave Atendimento", dropna=False)
        .agg(
            Ticket_resultado=("Ticket Jira", "first"),
            Placa_resultado=("Placa", "first"),
            OS_resultado=("OS", juntar_unicos),
            Oficina_resultado=("Oficina", "first"),
            Status_resultado=("Status da Atividade", juntar_unicos),
            Razao_improdutiva=(
                "Razão da Improdutiva",
                juntar_unicos,
            ),
            Observacao_tecnico_improdutiva=(
                "Observação do Técnico (Improdutiva)",
                juntar_unicos,
            ),
            Intervalo_tempo=(
                "Intervalo de Tempo",
                "first",
            ),
            Janela_servico=(
                "Janela de Serviço",
                "first",
            ),
            Janela_servico_detalhada=(
                "Janela de Serviço.1",
                "first",
            ),
            Inicio_real=(
                "Início",
                "first",
            ),
            Fim_real=(
                "Fim",
                "first",
            ),
            Duracao_real=(
                "Duração",
                "first",
            ),
            Tecnico_recurso=(
                "Recurso",
                "first",
            ),
            Cliente_resultado=(
                "Cliente",
                "first",
            ),
            Cidade_resultado=(
                "Cidade",
                "first",
            ),
            Qtd_resultado=("Chave Atendimento", "size"),
        )
        .reset_index()
    )

    resumo_planejado = (
        planejado
        .groupby("Chave Atendimento", dropna=False)
        .agg(
            Ticket_planejado=("Ticket Jira", "first"),
            Placa_planejada=("Placa", "first"),
            OS_planejada=("OS", juntar_unicos),
            Oficina_planejada=("Oficina", "first"),
            Status_planejado=("Status da Atividade", juntar_unicos),
            Primeira_aparicao=(
                "__Primeira Aparição",
                "first",
            ),
            Primeira_aparicao_data=(
                "__Primeira Aparição Data",
                "first",
            ),
            Data_operacional_planejada=(
                "__Data Operacional",
                "first",
            ),
            Ativa_planejamento=(
                "__Ativa no Planejamento",
                "max",
            ),
            Planejamento_base=(
                "__Planejamento Base",
                "max",
            ),
            Qtd_planejada=("Chave Atendimento", "size"),
        )
        .reset_index()
    )

    conciliacao = resumo_planejado.merge(
        resumo_resultado,
        on="Chave Atendimento",
        how="outer",
        indicator=True,
    )

    # Compatibilidade histórica:
    # datas importadas antes da criação da coluna planejamento_base
    # podem ter todos os registros com False. Nessa situação, não
    # devemos zerar o planejado nem transformar tudo em extra.
    #
    # Como conciliar_bases() é executada uma vez por data operacional,
    # basta verificar se existe ao menos uma OS-base válida nesta data.
    tem_planejamento_base_persistido = bool(
        "Planejamento_base" in resumo_planejado.columns
        and resumo_planejado["Planejamento_base"]
        .fillna(False)
        .astype(bool)
        .any()
    )

    # Auditoria de substituição de OS:
    # se a OS planejada não aparece pelo mesmo atendimento, procuramos
    # outra OS no resultado com o MESMO Ticket Jira + MESMA placa.
    # Nessa situação, não declaramos perda de OS automaticamente.
    def chave_ticket_placa(ticket, placa) -> str:
        ticket_norm = normalizar_texto(ticket)
        placa_norm = normalizar_texto(placa)

        if not ticket_norm or not placa_norm:
            return ""

        return f"{ticket_norm}||{placa_norm}"

    chaves_resultado_ticket_placa = set()

    for _, item in resumo_resultado.iterrows():
        chave = chave_ticket_placa(
            item.get("Ticket_resultado", ""),
            item.get("Placa_resultado", ""),
        )
        if chave:
            chaves_resultado_ticket_placa.add(chave)

    def encontrou_substituicao_ticket_placa(linha) -> bool:
        if linha.get("_merge") != "left_only":
            return False

        chave = chave_ticket_placa(
            linha.get("Ticket_planejado", ""),
            linha.get("Placa_planejada", ""),
        )

        return bool(
            chave
            and chave in chaves_resultado_ticket_placa
        )

    conciliacao["Possível substituição de OS"] = conciliacao.apply(
        encontrou_substituicao_ticket_placa,
        axis=1,
    )

    def data_referencia_linha(linha) -> str | None:
        data_operacional = converter_data_operacional(
            linha.get("Data_operacional_planejada", "")
        )
        return data_operacional

    def usar_regra_nova(linha) -> bool:
        data_operacional = data_referencia_linha(linha)

        if not data_operacional:
            return False

        # A regra nova só pode ser aplicada quando a data possui
        # planejamento_base efetivamente reconstruído/persistido.
        # Caso contrário, preservamos a regra histórica para não
        # zerar o planejado de 08 a 12/08.
        return bool(
            data_operacional >= DATA_CORTE_NOVA_REGRA
            and tem_planejamento_base_persistido
        )

    # -----------------------------------------------------
    # REGRA DE AGENDAMENTO — v2.4.6
    # -----------------------------------------------------
    # A partir desta versão, a fonte de verdade é a coluna
    # planejamento_base persistida no Supabase.
    #
    # - Primeira fotografia da data:
    #   OS não cancelada -> planejamento_base = True
    # - Atualizações posteriores:
    #   preservam True para quem já era base
    #   e novas OS entram como False (extra/encaixe).
    #
    # Assim, cancelamentos, retiradas e atualizações posteriores
    # nunca apagam o compromisso original do dia.

    def eh_agendada_nova(linha) -> bool:
        if linha.get("_merge") == "right_only":
            return False

        return bool(
            linha.get("Planejamento_base", False)
        )

    def eh_agendada_historica(linha) -> bool:
        # Na regra antiga, toda manutenção válida presente no planejado
        # era considerada agendada, independentemente de primeira aparição.
        if linha.get("_merge") == "right_only":
            return False

        status_planejado = linha.get("Status_planejado", "")

        if status_cancelado(status_planejado):
            return False

        return True

    def eh_agendada(linha) -> bool:
        if usar_regra_nova(linha):
            return eh_agendada_nova(linha)

        return eh_agendada_historica(linha)

    conciliacao["Origem Agendamento"] = conciliacao.apply(
        lambda linha: (
            "Agendada"
            if eh_agendada(linha)
            else "Extra / encaixe"
        ),
        axis=1,
    )

    def classificar(linha) -> str:
        origem_merge = linha["_merge"]
        status_resultado = linha.get("Status_resultado", "")
        status_planejado = linha.get("Status_planejado", "")
        agendada = linha["Origem Agendamento"] == "Agendada"
        ativa = bool(linha.get("Ativa_planejamento", False))
        nova_regra = usar_regra_nova(linha)

        # Histórico antigo preserva a lógica anterior.
        if not nova_regra:
            if origem_merge == "left_only":
                if status_cancelado(status_planejado):
                    return "Cancelada no agendamento"
                if bool(
                    linha.get(
                        "Possível substituição de OS",
                        False,
                    )
                ):
                    return "Possível substituição de OS"
                return "OS Perdida"

            if origem_merge == "right_only":
                if status_improdutivo(status_resultado):
                    return "Improdutiva extra"
                if status_cancelado(status_resultado):
                    return "Cancelada extra"
                if status_executado(status_resultado):
                    return "Executada extra"
                return "Evento extra"

            if status_cancelado(status_planejado):
                return "Cancelada no agendamento"

            if status_improdutivo(status_resultado):
                return "Improdutiva agendada"

            if status_cancelado(status_resultado):
                return "Cancelada"

            if status_executado(status_resultado):
                return "Executada agendada"

            return "Status intermediário agendado"

        # Nova regra, a partir da data de corte.
        # A classificação do destino não altera o denominador original
        # do planejamento: uma OS que estava na fotografia inicial
        # continua sendo planejada, mesmo que depois seja cancelada
        # ou retirada de uma fotografia posterior.
        if (
            origem_merge in {"left_only", "both"}
            and agendada
            and status_cancelado(status_planejado)
        ):
            return "Cancelada no agendamento"

        if origem_merge == "left_only" and not ativa:
            return "Retirada do agendamento"

        if origem_merge == "left_only":
            if agendada:
                if bool(
                    linha.get(
                        "Possível substituição de OS",
                        False,
                    )
                ):
                    return "Possível substituição de OS"
                return "OS Perdida"
            return "Encaixe não realizado"

        if origem_merge == "right_only":
            if status_improdutivo(status_resultado):
                return "Improdutiva extra"
            if status_cancelado(status_resultado):
                return "Cancelada extra"
            if status_executado(status_resultado):
                return "Executada extra"
            return "Evento extra"

        if status_improdutivo(status_resultado):
            return (
                "Improdutiva agendada"
                if agendada
                else "Improdutiva extra"
            )

        if status_cancelado(status_resultado):
            return (
                "Cancelada"
                if agendada
                else "Cancelada extra"
            )

        if status_executado(status_resultado):
            return (
                "Executada agendada"
                if agendada
                else "Executada extra"
            )

        return (
            "Status intermediário agendado"
            if agendada
            else "Status intermediário extra"
        )

    conciliacao["Classificação"] = conciliacao.apply(
        classificar,
        axis=1,
    )

    def explicar_classificacao(linha) -> str:
        classificacao = linha.get("Classificação", "")
        status_planejado = texto_limpo(
            linha.get("Status_planejado", "")
        )
        status_resultado = texto_limpo(
            linha.get("Status_resultado", "")
        )
        primeira = texto_limpo(
            linha.get("Primeira_aparicao_data", "")
        )
        data_operacional = texto_limpo(
            linha.get("Data_operacional_planejada", "")
        )
        nova_regra = usar_regra_nova(linha)

        if not nova_regra:
            return (
                "Histórico anterior à data de corte: classificação "
                "preservada pela lógica antiga do painel."
            )

        if classificacao == "Executada agendada":
            return (
                "Manutenção reconhecida como parte do planejamento vigente "
                f"de {data_operacional} "
                f"(primeira aparição registrada: {primeira}) e foi executada."
            )
        if classificacao == "Executada extra":
            return (
                "Manutenção não tinha prova de agendamento para essa data "
                "antes do início do dia e foi executada como extra/encaixe."
            )
        if classificacao == "Improdutiva agendada":
            return (
                "Manutenção já estava agendada antes do dia e terminou "
                f"improdutiva/não concluída: {status_resultado}"
            )
        if classificacao == "Improdutiva extra":
            return (
                "Manutenção extra/encaixe terminou improdutiva/não concluída: "
                f"{status_resultado}"
            )
        if classificacao == "Cancelada":
            return (
                "Manutenção estava agendada válida e apareceu cancelada "
                f"posteriormente no resultado: {status_resultado}"
            )
        if classificacao == "Cancelada extra":
            return (
                "Cancelamento de manutenção sem prova de agendamento "
                "anterior para essa mesma data."
            )
        if classificacao == "Cancelada no agendamento":
            return (
                "A manutenção já estava cancelada na fotografia vigente "
                f"do agendamento: {status_planejado}"
            )
        if classificacao == "Possível substituição de OS":
            return (
                "A OS agendada não apareceu pelo mesmo atendimento, mas "
                "foi localizada outra OS no resultado com o mesmo Ticket "
                "Jira + mesma placa. O caso foi retirado de OS Perdidas para "
                "auditoria de possível troca/substituição de OS."
            )
        if classificacao == "OS Perdida":
            return (
                "Manutenção estava no planejamento-base, mas a própria OS não apareceu "
                "no resultado e nenhuma substituição pelo mesmo Ticket Jira + "
                "placa foi localizada. O painel registra como OS Perdida; a "
                "causa não é atribuída automaticamente ao técnico."
            )
        if classificacao == "Encaixe não realizado":
            return (
                "A manutenção surgiu no próprio dia como extra/encaixe "
                "e não apareceu no resultado; não conta como OS Perdida."
            )
        if classificacao == "Retirada do agendamento":
            return (
                "A OS fazia parte do planejamento-base do dia, mas deixou "
                "de aparecer em uma fotografia posterior. Ela continua "
                "contando no total originalmente planejado."
            )

        return (
            "Status não reconhecido pelas regras principais. "
            f"Planejado: {status_planejado}; resultado: {status_resultado}."
        )

    conciliacao["Motivo da Classificação"] = conciliacao.apply(
        explicar_classificacao,
        axis=1,
    )

    conciliacao["Regra Aplicada"] = conciliacao.apply(
        lambda linha: (
            "Nova regra"
            if usar_regra_nova(linha)
            else "Regra histórica"
        ),
        axis=1,
    )

    conciliacao["Troca de OS"] = conciliacao.apply(
        lambda linha: (
            "Sim"
            if (
                linha["_merge"] == "both"
                and texto_limpo(
                    linha.get("OS_planejada", "")
                )
                != texto_limpo(
                    linha.get("OS_resultado", "")
                )
            )
            else "Não"
        ),
        axis=1,
    )

    conciliacao["Oficina"] = conciliacao[
        "Oficina_planejada"
    ].fillna(conciliacao["Oficina_resultado"])

    conciliacao["Ticket"] = conciliacao[
        "Ticket_planejado"
    ].fillna(conciliacao["Ticket_resultado"])

    conciliacao["Placa"] = conciliacao[
        "Placa_planejada"
    ].fillna(conciliacao["Placa_resultado"])

    return conciliacao



# =========================================================
# REGRA DE EXPURGO MCI / MD — vigente a partir de 19/08/2026
# Aplicada retroativamente a todo o histórico já armazenado.
# As OS continuam classificadas e visíveis no painel de improdutividade.
# =========================================================

def motivo_expurgado_mci_md(valor) -> bool:
    texto = normalizar_texto(valor)

    if not texto:
        return False

    motivos_expurgados = [
        "PROBLEMAS TECNICOS COM SISTEMAS",
        "PROBLEMA TECNICO COM SISTEMA",
        "PROBLEMAS TECNICOS EM SISTEMAS",
        "PROBLEMA TECNICO EM SISTEMA",
        "PROBLEMAS TECNICOS COM VEICULOS",
        "PROBLEMA TECNICO COM VEICULO",
        "PROBLEMAS TECNICOS COM O VEICULO",
        "PROBLEMAS TECNICOS EM VEICULOS",
        "PROBLEMA TECNICO EM VEICULO",
    ]

    return any(
        motivo in texto
        for motivo in motivos_expurgados
    )


def mascara_improdutiva_expurgada_mci(
    base: pd.DataFrame,
) -> pd.Series:
    """
    MCI continua usando exclusivamente o motivo original do OFS.
    A revisão manual não altera MCI.
    """
    if base is None or base.empty:
        return pd.Series(dtype=bool)

    motivos = base.get(
        "Razao_improdutiva",
        pd.Series("", index=base.index, dtype=str),
    ).fillna("")

    mascara_improdutiva = base["Classificação"].isin(
        [
            "Improdutiva agendada",
            "Improdutiva extra",
        ]
    )

    return (
        mascara_improdutiva
        & motivos.apply(motivo_expurgado_mci_md)
    )


def mascara_improdutiva_expurgada_md(
    base: pd.DataFrame,
) -> pd.Series:
    """
    MD usa o motivo revisado quando existir.
    Sem revisão, usa o motivo original.
    """
    if base is None or base.empty:
        return pd.Series(dtype=bool)

    motivos = base.get(
        "Razao_improdutiva_md",
        base.get(
            "Razao_improdutiva",
            pd.Series("", index=base.index, dtype=str),
        ),
    ).fillna("")

    mascara_improdutiva = base["Classificação"].isin(
        [
            "Improdutiva agendada",
            "Improdutiva extra",
        ]
    )

    return (
        mascara_improdutiva
        & motivos.apply(motivo_expurgado_mci_md)
    )


def calcular_indicadores(conciliacao: pd.DataFrame) -> dict:
    # Fonte de verdade do denominador original:
    # toda OS reconhecida como parte da fotografia-base do planejamento.
    if "Origem Agendamento" in conciliacao.columns:
        manutencoes_agendadas = int(
            (
                conciliacao["Origem Agendamento"]
                == "Agendada"
            ).sum()
        )
    else:
        agendadas_validas = {
            "Executada agendada",
            "Improdutiva agendada",
            "Cancelada",
            "Cancelada no agendamento",
            "Retirada do agendamento",
            "Possível substituição de OS",
            "OS Perdida",
            "Status intermediário agendado",
        }
        manutencoes_agendadas = int(
            conciliacao["Classificação"].isin(
                agendadas_validas
            ).sum()
        )

    agendadas_executadas = int(
        (
            conciliacao["Classificação"]
            == "Executada agendada"
        ).sum()
    )

    executadas_extras = int(
        (
            conciliacao["Classificação"]
            == "Executada extra"
        ).sum()
    )

    improdutivas_agendadas = int(
        (
            conciliacao["Classificação"]
            == "Improdutiva agendada"
        ).sum()
    )

    improdutivas_extras = int(
        (
            conciliacao["Classificação"]
            == "Improdutiva extra"
        ).sum()
    )

    improdutivas_totais = (
        improdutivas_agendadas
        + improdutivas_extras
    )

    mascara_expurgada_mci = mascara_improdutiva_expurgada_mci(
        conciliacao
    )
    mascara_expurgada_md = mascara_improdutiva_expurgada_md(
        conciliacao
    )

    improdutivas_expurgadas_mci = int(
        mascara_expurgada_mci.sum()
    )
    improdutivas_expurgadas_md = int(
        mascara_expurgada_md.sum()
    )

    improdutivas_expurgadas_agendadas_mci = int(
        (
            mascara_expurgada_mci
            & (
                conciliacao["Classificação"]
                == "Improdutiva agendada"
            )
        ).sum()
    )

    improdutivas_expurgadas_extras_md = int(
        (
            mascara_expurgada_md
            & (
                conciliacao["Classificação"]
                == "Improdutiva extra"
            )
        ).sum()
    )

    classificacao_gerencial = conciliacao.get(
        "Classificacao_gerencial_MD",
        pd.Series(
            "Improdutiva",
            index=conciliacao.index,
            dtype=str,
        ),
    ).fillna("Improdutiva")

    mascara_retirada_md = (
        conciliacao["Classificação"].isin(
            ["Improdutiva agendada", "Improdutiva extra"]
        )
        & classificacao_gerencial.isin(
            ["No-show técnico", "No-show cliente", "Cancelada"]
        )
    )

    improdutivas_retiradas_md = int(
        mascara_retirada_md.sum()
    )

    mascara_fora_md = (
        mascara_expurgada_md
        | mascara_retirada_md
    )

    improdutivas_consideradas = max(
        improdutivas_totais
        - int(mascara_fora_md.sum()),
        0,
    )

    # MCI permanece regida exclusivamente pelo motivo original.
    planejadas_elegiveis_mci = max(
        manutencoes_agendadas
        - improdutivas_expurgadas_agendadas_mci,
        0,
    )

    canceladas = int(
        (
            conciliacao["Classificação"]
            == "Cancelada"
        ).sum()
    )

    no_show = int(
        (
            conciliacao["Classificação"]
            == "OS Perdida"
        ).sum()
    )

    total_executadas = (
        agendadas_executadas
        + executadas_extras
    )

    mci = (
        total_executadas
        / planejadas_elegiveis_mci
        * 100
        if planejadas_elegiveis_mci
        else 0.0
    )

    # MD: as duas causas expurgadas não entram nem no numerador
    # nem no denominador da medida.
    base_md = (
        total_executadas
        + improdutivas_consideradas
    )

    md = (
        improdutivas_consideradas
        / base_md
        * 100
        if base_md
        else 0.0
    )

    return {
        "Planejadas": manutencoes_agendadas,
        "Planejadas elegíveis MCI": planejadas_elegiveis_mci,
        "Executadas planejadas": agendadas_executadas,
        "Improdutivas": improdutivas_totais,
        "Improdutivas agendadas": improdutivas_agendadas,
        "Improdutivas extras": improdutivas_extras,
        "Improdutivas consideradas MD": improdutivas_consideradas,
        "Improdutivas reclassificadas fora da MD": improdutivas_retiradas_md,
        "Improdutivas expurgadas": improdutivas_expurgadas_md,
        "Improdutivas expurgadas MD": improdutivas_expurgadas_md,
        "Improdutivas expurgadas MCI": improdutivas_expurgadas_mci,
        "Improdutivas expurgadas agendadas": improdutivas_expurgadas_agendadas_mci,
        "Improdutivas expurgadas extras": improdutivas_expurgadas_extras_md,
        "Base MD": base_md,
        "Canceladas": canceladas,
        "OS Perdidas": no_show,
        "Possíveis substituições de OS": int(
            (
                conciliacao["Classificação"]
                == "Possível substituição de OS"
            ).sum()
        ),
        "Executadas extras": executadas_extras,
        "MCI": mci,
        "MD": md,
        # OS Perdidas e cancelamento permanecem sobre o planejamento-base
        # original. A nova regra altera somente MCI e MD.
        "Taxa de perda de OS": (
            no_show
            / manutencoes_agendadas
            * 100
            if manutencoes_agendadas
            else 0.0
        ),
        "Índice cancelamento": (
            canceladas
            / manutencoes_agendadas
            * 100
            if manutencoes_agendadas
            else 0.0
        ),
        "Execução total": (
            total_executadas
            / planejadas_elegiveis_mci
            * 100
            if planejadas_elegiveis_mci
            else 0.0
        ),
    }


def enriquecer_com_cadastro(
    base: pd.DataFrame,
    cadastro: pd.DataFrame,
) -> pd.DataFrame:
    resultado = base.copy()

    if "Oficina" not in resultado.columns:
        resultado["Oficina"] = ""

    resultado["Chave Oficina"] = resultado["Oficina"].apply(
        normalizar_texto
    )

    colunas = [
        "Chave Oficina",
        "Cidade-base",
        "UF-base",
        "Consultor",
        "WhatsApp",
        "Prioridade",
    ]

    resultado = resultado.merge(
        cadastro[colunas].drop_duplicates(
            subset=["Chave Oficina"]
        ),
        on="Chave Oficina",
        how="left",
    )

    resultado["Consultor"] = resultado["Consultor"].fillna(
        "Não definido"
    )
    resultado["Cidade-base"] = resultado["Cidade-base"].fillna("")
    resultado["UF-base"] = resultado["UF-base"].fillna("")
    resultado["WhatsApp"] = resultado["WhatsApp"].fillna("")
    resultado["Prioridade"] = resultado["Prioridade"].fillna("Normal")

    return resultado



# =========================================================
# PRÉ-AGENDA / ACEITE E ALOCAÇÃO
# =========================================================

def valor_booleano_ofs(valor) -> bool:
    texto = normalizar_texto(valor)
    return texto in {
        "SIM", "S", "TRUE", "1", "YES", "REJEITADA", "REJEITADO"
    }


def classificar_pre_agenda(linha: pd.Series) -> str:
    """
    Regra preventiva:
    - motivo/flag de rejeição => Rejeitada pela oficina;
    - recurso vazio ou igual à oficina => Sem técnico / requer alocação;
    - recurso diferente da oficina => Atribuída ao técnico;
    - dados insuficientes => A validar.
    """
    oficina = texto_limpo(linha.get("Oficina", ""))
    recurso = texto_limpo(linha.get("Recurso", ""))
    motivo_rejeicao = texto_limpo(
        linha.get("Motivos de Rejeição da OS", "")
    )
    flag_rejeitada = linha.get("Flag OS Rejeitada", "")

    if motivo_rejeicao or valor_booleano_ofs(flag_rejeitada):
        return "Rejeitada pela oficina"

    if not oficina:
        return "A validar"

    if not recurso:
        return "Sem técnico / requer alocação"

    if normalizar_texto(recurso) == normalizar_texto(oficina):
        return "Sem técnico / requer alocação"

    return "Atribuída ao técnico"


def preparar_pre_agenda_para_banco(
    df: pd.DataFrame,
    nome_arquivo: str,
    importacao_id: str,
    importado_em: str,
) -> list[dict]:
    """
    Prepara uma fotografia independente do processo de aceite/alocação.
    Não altera atividades_planejadas nem planejamento_base.
    """
    # O Controle Pré-Agenda é preventivo e acompanha aceite/alocação
    # de todas as atividades do relatório, não apenas manutenções.
    # Isso NÃO altera Planejado, Resultado, MCI, MD ou planejamento_base.
    base = df.copy()

    if base is None or base.empty:
        return []

    colunas_obrigatorias = ["OS", "Oficina", "Recurso"]
    faltantes = [
        coluna
        for coluna in colunas_obrigatorias
        if coluna not in base.columns
    ]
    if faltantes:
        raise ValueError(
            "O arquivo de pré-agenda não possui as colunas obrigatórias: "
            + ", ".join(faltantes)
        )

    # As colunas abaixo podem existir vazias no OFS.
    for coluna in [
        "Data",
        "Ticket Jira",
        "Placa",
        "Status da Atividade",
        "Tipo de Atividade",
        "Aceite OS",
        "Flag OS Rejeitada",
        "Motivos de Rejeição da OS",
    ]:
        if coluna not in base.columns:
            base[coluna] = ""

    base = criar_chaves(base)
    base = base.drop_duplicates(
        subset=["Chave Atendimento"],
        keep="last",
    )

    registros = []

    for _, linha in base.iterrows():
        data_operacional = converter_data_operacional(
            linha.get("Data", "")
        )

        dados = dados_sanitizados_linha(linha)

        registros.append(
            {
                "importacao_id": importacao_id,
                "importado_em": importado_em,
                "nome_arquivo": nome_arquivo,
                "data_operacional": data_operacional,
                "chave_atendimento": texto_limpo(
                    linha.get("Chave Atendimento", "")
                ),
                "ticket_jira": texto_limpo(
                    linha.get("Ticket Jira", "")
                ),
                "os": texto_limpo(linha.get("OS", "")),
                "placa": texto_limpo(linha.get("Placa", "")),
                "oficina": texto_limpo(linha.get("Oficina", "")),
                "recurso": texto_limpo(linha.get("Recurso", "")),
                "status_atividade": texto_limpo(
                    linha.get("Status da Atividade", "")
                ),
                "tipo_atividade": texto_limpo(
                    linha.get("Tipo de Atividade", "")
                ),
                "aceite_os": texto_limpo(
                    linha.get("Aceite OS", "")
                ),
                "flag_os_rejeitada": texto_limpo(
                    linha.get("Flag OS Rejeitada", "")
                ),
                "motivo_rejeicao": texto_limpo(
                    linha.get("Motivos de Rejeição da OS", "")
                ),
                "situacao_pre_agenda": classificar_pre_agenda(linha),
                "dados": dados,
            }
        )

    return registros


def salvar_pre_agenda(
    nome_arquivo: str,
    df: pd.DataFrame,
) -> tuple[str, int]:
    """
    Salva uma fotografia append-only.
    O painel usa a fotografia mais recente, preservando as anteriores
    para futura auditoria/evolução.
    """
    importacao_id = str(uuid.uuid4())
    importado_em = datetime.now(FUSO_BRASIL).isoformat()

    registros = preparar_pre_agenda_para_banco(
        df,
        nome_arquivo,
        importacao_id,
        importado_em,
    )

    if not registros:
        raise ValueError(
            "Nenhuma atividade encontrada no arquivo de pré-agenda."
        )

    inserir_em_lotes(
        "pre_agenda_aceite",
        registros,
    )

    invalidar_cache_dados()
    return importacao_id, len(registros)


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_pre_agenda_mais_recente() -> pd.DataFrame:
    registros = buscar_todos(
        "pre_agenda_aceite",
        ordem="importado_em",
        desc=True,
    )

    if not registros:
        return pd.DataFrame()

    mais_recente = registros[0]
    importacao_id = texto_limpo(
        mais_recente.get("importacao_id", "")
    )

    if not importacao_id:
        return pd.DataFrame()

    fotografia = [
        registro
        for registro in registros
        if texto_limpo(registro.get("importacao_id", ""))
        == importacao_id
    ]

    base = pd.DataFrame(fotografia)

    if base.empty:
        return base

    renomear = {
        "data_operacional": "Data operacional",
        "ticket_jira": "Ticket Jira",
        "os": "OS",
        "placa": "Placa",
        "oficina": "Oficina",
        "recurso": "Recurso",
        "status_atividade": "Status",
        "tipo_atividade": "Tipo de Atividade",
        "chave_atendimento": "Chave Atendimento",
        "aceite_os": "Aceite OS",
        "flag_os_rejeitada": "Flag rejeitada",
        "motivo_rejeicao": "Motivo rejeição",
        "situacao_pre_agenda": "Situação",
        "nome_arquivo": "Arquivo",
        "importado_em": "Importado em",
    }
    base = base.rename(columns=renomear)

    return base



@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_duas_fotografias_pre_agenda() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Retorna (fotografia_atual, fotografia_anterior).
    As fotografias são identificadas por importacao_id e preservadas
    em modo append-only na tabela pre_agenda_aceite.
    """
    registros = buscar_todos(
        "pre_agenda_aceite",
        ordem="importado_em",
        desc=True,
    )

    if not registros:
        return pd.DataFrame(), pd.DataFrame()

    ids = []
    for registro in registros:
        ident = texto_limpo(registro.get("importacao_id", ""))
        if ident and ident not in ids:
            ids.append(ident)
        if len(ids) >= 2:
            break

    def montar(importacao_id: str) -> pd.DataFrame:
        if not importacao_id:
            return pd.DataFrame()

        fotografia = [
            registro
            for registro in registros
            if texto_limpo(registro.get("importacao_id", ""))
            == importacao_id
        ]
        base = pd.DataFrame(fotografia)
        if base.empty:
            return base

        renomear = {
            "data_operacional": "Data operacional",
            "ticket_jira": "Ticket Jira",
            "os": "OS",
            "placa": "Placa",
            "oficina": "Oficina",
            "recurso": "Recurso",
            "status_atividade": "Status",
            "tipo_atividade": "Tipo de Atividade",
            "aceite_os": "Aceite OS",
            "flag_os_rejeitada": "Flag rejeitada",
            "motivo_rejeicao": "Motivo rejeição",
            "situacao_pre_agenda": "Situação",
            "nome_arquivo": "Arquivo",
            "importado_em": "Importado em",
            "chave_atendimento": "Chave Atendimento",
        }
        return base.rename(columns=renomear)

    atual = montar(ids[0] if ids else "")
    anterior = montar(ids[1] if len(ids) > 1 else "")
    return atual, anterior


def chave_comparacao_pre_agenda(linha: pd.Series) -> str:
    """
    Usa OS como chave preferencial porque é a referência operacional
    mais estável para comparar fotografias. Se faltar OS, usa a chave
    de atendimento persistida.
    """
    os_numero = texto_limpo(linha.get("OS", ""))
    if os_numero:
        return "OS:" + normalizar_texto(os_numero)

    chave = texto_limpo(linha.get("Chave Atendimento", ""))
    if chave:
        return "CHAVE:" + normalizar_texto(chave)

    return ""


def comparar_fotografias_pre_agenda(
    atual: pd.DataFrame,
    anterior: pd.DataFrame,
) -> dict:
    resultado = {
        "alocadas_desde_anterior": pd.DataFrame(),
        "persistem_sem_tecnico": pd.DataFrame(),
        "novas_sem_tecnico": pd.DataFrame(),
        "rejeitadas_novas": pd.DataFrame(),
    }

    if atual.empty:
        return resultado

    atual = atual.copy()
    atual["__chave_comp"] = atual.apply(
        chave_comparacao_pre_agenda,
        axis=1,
    )
    atual = atual[atual["__chave_comp"] != ""].copy()

    if anterior.empty:
        resultado["novas_sem_tecnico"] = atual[
            atual["Situação"] == "Sem técnico / requer alocação"
        ].copy()
        resultado["rejeitadas_novas"] = atual[
            atual["Situação"] == "Rejeitada pela oficina"
        ].copy()
        return resultado

    anterior = anterior.copy()
    anterior["__chave_comp"] = anterior.apply(
        chave_comparacao_pre_agenda,
        axis=1,
    )
    anterior = anterior[
        anterior["__chave_comp"] != ""
    ].drop_duplicates(
        subset=["__chave_comp"],
        keep="last",
    )

    atual = atual.drop_duplicates(
        subset=["__chave_comp"],
        keep="last",
    )

    mapa_anterior = anterior.set_index("__chave_comp")["Situação"].to_dict()
    chaves_anterior = set(anterior["__chave_comp"])

    atual["Situação anterior"] = atual["__chave_comp"].map(
        mapa_anterior
    ).fillna("Não estava na fotografia anterior")

    resultado["alocadas_desde_anterior"] = atual[
        (atual["Situação"] == "Atribuída ao técnico")
        & (
            atual["Situação anterior"]
            == "Sem técnico / requer alocação"
        )
    ].copy()

    resultado["persistem_sem_tecnico"] = atual[
        (atual["Situação"] == "Sem técnico / requer alocação")
        & (
            atual["Situação anterior"]
            == "Sem técnico / requer alocação"
        )
    ].copy()

    resultado["novas_sem_tecnico"] = atual[
        (atual["Situação"] == "Sem técnico / requer alocação")
        & (~atual["__chave_comp"].isin(chaves_anterior))
    ].copy()

    resultado["rejeitadas_novas"] = atual[
        (atual["Situação"] == "Rejeitada pela oficina")
        & (
            atual["Situação anterior"]
            != "Rejeitada pela oficina"
        )
    ].copy()

    return resultado


def enriquecer_pre_agenda_com_cadastro(
    base: pd.DataFrame,
    cadastro: pd.DataFrame,
) -> pd.DataFrame:
    resultado = base.copy()

    if resultado.empty:
        return resultado

    resultado["Chave Oficina"] = resultado["Oficina"].apply(
        normalizar_texto
    )

    if cadastro.empty:
        resultado["Consultor"] = "Não definido"
        resultado["Região"] = "Não definida"
        return resultado

    apoio = cadastro.copy()
    if "Chave Oficina" not in apoio.columns:
        apoio["Chave Oficina"] = apoio["Oficina"].apply(
            normalizar_texto
        )

    colunas = [
        coluna
        for coluna in [
            "Chave Oficina",
            "Consultor",
            "UF-base",
            "Cidade-base",
        ]
        if coluna in apoio.columns
    ]

    resultado = resultado.merge(
        apoio[colunas].drop_duplicates(
            subset=["Chave Oficina"]
        ),
        on="Chave Oficina",
        how="left",
    )

    resultado["Consultor"] = resultado.get(
        "Consultor",
        pd.Series(index=resultado.index, dtype=str),
    ).fillna("Não definido")

    resultado["Região"] = resultado["Consultor"].map(
        REGIOES_CONSULTORES
    ).fillna("Não definida")

    return resultado


def prioridade_pre_agenda(data_operacional) -> tuple[str, int | None]:
    data = pd.to_datetime(
        data_operacional,
        errors="coerce",
    )
    if pd.isna(data):
        return "⚪ Data inválida", None

    hoje = datetime.now(FUSO_BRASIL).date()
    diferenca = (data.date() - hoje).days

    if diferenca < 0:
        return "⚫ Vencida", diferenca
    if diferenca == 0:
        return "🔴 D0 — Urgente", diferenca
    if diferenca == 1:
        return "🔴 D+1 — Crítico", diferenca
    if diferenca == 2:
        return "🟠 D+2 — Atenção", diferenca

    return f"⚪ D+{diferenca}", diferenca


# =========================================================
# PERSISTÊNCIA
# =========================================================

def registro_atividade(
    linha: pd.Series,
    data_operacional: str,
    incluir_status: bool,
    primeira_aparicao: str | None = None,
    primeira_aparicao_data: str | None = None,
    ultima_aparicao: str | None = None,
    ativa_no_planejamento: bool | None = None,
    nome_arquivo_primeira_aparicao: str | None = None,
    planejamento_base: bool | None = None,
) -> dict:
    dados = dados_sanitizados_linha(linha)

    registro = {
        "data_operacional": data_operacional,
        "chave_atendimento": texto_limpo(linha["Chave Atendimento"]),
        "ticket_jira": texto_limpo(linha.get("Ticket Jira", "")),
        "os": texto_limpo(linha.get("OS", "")),
        "placa": texto_limpo(linha.get("Placa", "")),
        "oficina": texto_limpo(linha.get("Oficina", "")),
        "cliente": texto_limpo(linha.get("Cliente", "")),
        "estado": texto_limpo(linha.get("Estado", "")),
        "cidade": texto_limpo(linha.get("Cidade", "")),
        "tipo_atividade": texto_limpo(
            linha.get("Tipo de Atividade", "")
        ),
        "recurso": texto_limpo(linha.get("Recurso", "")),
        "dados": dados,
    }

    if incluir_status:
        registro["status_atividade"] = texto_limpo(
            linha.get("Status da Atividade", "")
        )

    if primeira_aparicao is not None:
        registro["primeira_aparicao"] = primeira_aparicao

    if primeira_aparicao_data is not None:
        registro["primeira_aparicao_data"] = primeira_aparicao_data

    if ultima_aparicao is not None:
        registro["ultima_aparicao"] = ultima_aparicao

    if ativa_no_planejamento is not None:
        registro["ativa_no_planejamento"] = ativa_no_planejamento

    if nome_arquivo_primeira_aparicao is not None:
        registro["nome_arquivo_primeira_aparicao"] = (
            nome_arquivo_primeira_aparicao
        )

    if planejamento_base is not None:
        registro["planejamento_base"] = bool(
            planejamento_base
        )

    return registro


def preparar_atividades_para_banco(
    df: pd.DataFrame,
    data_operacional: str,
    incluir_status: bool,
) -> list[dict]:
    base = criar_chaves(df)

    base = base.drop_duplicates(
        subset=["Chave Atendimento"],
        keep="last",
    )

    return [
        registro_atividade(
            linha,
            data_operacional,
            incluir_status,
        )
        for _, linha in base.iterrows()
    ]


def converter_data_operacional(valor) -> str | None:
    texto = texto_limpo(valor)

    if not texto:
        return None

    for dayfirst in (True, False):
        try:
            data_convertida = pd.to_datetime(
                texto,
                dayfirst=dayfirst,
                errors="raise",
            )
            return data_convertida.date().isoformat()
        except Exception:
            pass

    return None


def separar_planejamento_por_data(
    df: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """
    Recebe o CSV de janela móvel do OFS e separa automaticamente
    as manutenções pela coluna Data.
    """
    base = filtrar_somente_manutencoes(df)

    if "Data" not in base.columns:
        raise ValueError(
            "A base não possui a coluna 'Data'. "
            "Não é possível separar a janela por dia."
        )

    base = base.copy()
    base["__Data Operacional"] = base["Data"].apply(
        converter_data_operacional
    )

    invalidas = base["__Data Operacional"].isna().sum()

    if invalidas:
        raise ValueError(
            f"{invalidas} linha(s) de manutenção possuem Data inválida."
        )

    return {
        str(data_operacional): grupo.drop(
            columns=["__Data Operacional"]
        ).reset_index(drop=True)
        for data_operacional, grupo in base.groupby(
            "__Data Operacional"
        )
    }


def salvar_planejamento_janela(
    nome_arquivo: str,
    df: pd.DataFrame,
) -> dict[str, int]:
    """
    Salva uma fotografia de 1, 3 ou mais dias do planejamento.

    A OS é identificada por OS (+ placa quando disponível).
    Para cada OS/data, preserva a primeira vez em que ela apareceu.
    Registros que existiam em uma fotografia anterior daquela data,
    mas desapareceram na fotografia atual, ficam inativos em vez de
    serem apagados. Isso preserva o histórico sem tratá-los como
    agendamento vigente.
    """
    cliente = exigir_supabase()
    grupos = separar_planejamento_por_data(df)
    agora = datetime.now(FUSO_BRASIL)
    agora_iso = agora.isoformat()
    hoje_iso = agora.date().isoformat()
    resumo: dict[str, int] = {}

    for data_operacional, grupo in grupos.items():
        base = criar_chaves(grupo).drop_duplicates(
            subset=["Chave Atendimento"],
            keep="last",
        )

        existentes = buscar_todos(
            "atividades_planejadas",
            filtros={"data_operacional": data_operacional},
        )

        mapa_existentes = {
            texto_limpo(registro.get("chave_atendimento", "")): registro
            for registro in existentes
        }

        # A primeira fotografia-base é aquela salva quando ainda
        # não existe nenhuma OS marcada como planejamento_base=True
        # para a data. Isso também permite reconstruir corretamente
        # datas históricas existentes antes da criação dessa coluna.
        #
        # Depois que ao menos uma OS-base existe, importações seguintes
        # preservam o denominador e novas OS entram como encaixe/extra.
        primeira_fotografia_da_data = not any(
            bool(registro.get("planejamento_base", False))
            for registro in existentes
        )

        # Tudo que não reaparecer nesta fotografia deixa de ser
        # agendamento vigente, mas continua salvo para auditoria.
        if existentes:
            cliente.table("atividades_planejadas").update(
                {
                    "ativa_no_planejamento": False,
                    "ultima_aparicao": agora_iso,
                }
            ).eq(
                "data_operacional",
                data_operacional,
            ).execute()

        registros = []

        for _, linha in base.iterrows():
            chave = texto_limpo(linha["Chave Atendimento"])
            anterior = mapa_existentes.get(chave)

            primeira_aparicao = (
                anterior.get("primeira_aparicao")
                if anterior
                else agora_iso
            )
            primeira_aparicao_data = (
                anterior.get("primeira_aparicao_data")
                if anterior
                else hoje_iso
            )
            arquivo_primeira = (
                anterior.get("nome_arquivo_primeira_aparicao")
                if anterior
                else nome_arquivo
            )

            if anterior:
                eh_planejamento_base = bool(
                    anterior.get(
                        "planejamento_base",
                        False,
                    )
                )
            else:
                # Na primeira fotografia da data, somente OS ainda
                # válidas/não canceladas formam o compromisso original.
                eh_planejamento_base = bool(
                    primeira_fotografia_da_data
                    and not status_cancelado(
                        linha.get(
                            "Status da Atividade",
                            "",
                        )
                    )
                )

            registro = registro_atividade(
                linha,
                data_operacional,
                incluir_status=True,
                primeira_aparicao=primeira_aparicao,
                primeira_aparicao_data=primeira_aparicao_data,
                ultima_aparicao=agora_iso,
                ativa_no_planejamento=True,
                nome_arquivo_primeira_aparicao=arquivo_primeira,
                planejamento_base=eh_planejamento_base,
            )
            registros.append(registro)

        for inicio in range(0, len(registros), 300):
            cliente.table("atividades_planejadas").upsert(
                registros[inicio : inicio + 300],
                on_conflict="data_operacional,chave_atendimento",
            ).execute()

        cliente.table("bases_importadas").delete().eq(
            "tipo",
            "planejado",
        ).eq(
            "data_operacional",
            data_operacional,
        ).execute()

        cliente.table("bases_importadas").insert(
            {
                "tipo": "planejado",
                "data_operacional": data_operacional,
                "nome_arquivo": nome_arquivo,
                "quantidade_registros": len(registros),
                "atualizado_em": agora_iso,
            }
        ).execute()

        resumo[data_operacional] = len(registros)

    invalidar_cache_dados()
    return resumo


def salvar_base(
    tipo: str,
    data_operacional: date,
    nome_arquivo: str,
    df: pd.DataFrame,
) -> None:
    """
    Mantido para Resultado. Planejamento usa salvar_planejamento_janela().
    """
    if tipo == "planejado":
        salvar_planejamento_janela(nome_arquivo, df)
        return

    cliente = exigir_supabase()
    data_texto = data_operacional.isoformat()
    tabela = TABELA_POR_TIPO[tipo]

    base = filtrar_somente_manutencoes(df)
    registros = preparar_atividades_para_banco(
        base,
        data_texto,
        incluir_status=True,
    )

    cliente.table(tabela).delete().eq(
        "data_operacional",
        data_texto,
    ).execute()

    cliente.table("bases_importadas").delete().eq(
        "tipo",
        tipo,
    ).eq(
        "data_operacional",
        data_texto,
    ).execute()

    inserir_em_lotes(tabela, registros)

    cliente.table("bases_importadas").insert(
        {
            "tipo": tipo,
            "data_operacional": data_texto,
            "nome_arquivo": nome_arquivo,
            "quantidade_registros": len(registros),
            "atualizado_em": datetime.now(FUSO_BRASIL).isoformat(),
        }
    ).execute()

    invalidar_cache_dados()


def salvar_oficinas(cadastro: pd.DataFrame) -> None:
    cliente = exigir_supabase()

    registros = []

    for _, linha in cadastro.iterrows():
        ativa_texto = normalizar_texto(linha.get("Ativa", "SIM"))
        ativa = ativa_texto not in {"NAO", "N", "INATIVA", "INATIVO", "0"}

        registros.append(
            {
                "chave_oficina": texto_limpo(linha["Chave Oficina"]),
                "codigo_oficina": texto_limpo(linha.get("ID", "")),
                "nome_oficina": texto_limpo(linha.get("Oficina", "")),
                "cidade": texto_limpo(linha.get("Cidade-base", "")),
                "uf": texto_limpo(linha.get("UF-base", "")),
                "consultor": texto_limpo(
                    linha.get("Consultor", "Não definido")
                ) or "Não definido",
                "whatsapp": limpar_telefone(linha.get("WhatsApp", "")),
                "prioridade": texto_limpo(
                    linha.get("Prioridade", "Normal")
                ) or "Normal",
                "ativa": ativa,
                "observacoes": texto_limpo(
                    linha.get("Observações", "")
                ),
                "atualizado_em": datetime.now().isoformat(),
            }
        )

    for inicio in range(0, len(registros), 300):
        cliente.table("oficinas").upsert(
            registros[inicio : inicio + 300],
            on_conflict="chave_oficina",
        ).execute()

    invalidar_cache_dados()


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_oficinas() -> pd.DataFrame:
    registros = buscar_todos(
        "oficinas",
        ordem="nome_oficina",
    )

    if not registros:
        return pd.DataFrame()

    return pd.DataFrame(
        {
            "ID": [r.get("codigo_oficina", "") for r in registros],
            "Oficina": [r.get("nome_oficina", "") for r in registros],
            "Cidade-base": [r.get("cidade", "") for r in registros],
            "UF-base": [r.get("uf", "") for r in registros],
            "Consultor": [r.get("consultor", "Não definido") for r in registros],
            "WhatsApp": [r.get("whatsapp", "") for r in registros],
            "Prioridade": [r.get("prioridade", "Normal") for r in registros],
            "Ativa": ["Sim" if r.get("ativa", True) else "Não" for r in registros],
            "Observações": [r.get("observacoes", "") for r in registros],
            "Chave Oficina": [r.get("chave_oficina", "") for r in registros],
        }
    )


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_base(
    tipo: str,
    data_operacional: str,
) -> pd.DataFrame:
    tabela = TABELA_POR_TIPO[tipo]
    registros = buscar_todos(
        tabela,
        filtros={"data_operacional": data_operacional},
        ordem="id",
    )

    linhas = []

    for registro in registros:
        dados = dict(registro.get("dados") or {})

        padrao = {
            "Ticket Jira": registro.get("ticket_jira", ""),
            "OS": registro.get("os", ""),
            "Placa": registro.get("placa", ""),
            "Oficina": registro.get("oficina", ""),
            "Cliente": registro.get("cliente", ""),
            "Estado": registro.get("estado", ""),
            "Cidade": registro.get("cidade", ""),
            "Tipo de Atividade": registro.get("tipo_atividade", ""),
            "Status da Atividade": registro.get(
                "status_atividade",
                "",
            ),
            "Recurso": registro.get("recurso", ""),
            "__Data Operacional": registro.get(
                "data_operacional",
                data_operacional,
            ),
        }

        if tipo == "planejado":
            padrao.update(
                {
                    "__Primeira Aparição": registro.get(
                        "primeira_aparicao",
                        "",
                    ),
                    "__Primeira Aparição Data": registro.get(
                        "primeira_aparicao_data",
                        "",
                    ),
                    "__Última Aparição": registro.get(
                        "ultima_aparicao",
                        "",
                    ),
                    "__Ativa no Planejamento": bool(
                        registro.get(
                            "ativa_no_planejamento",
                            True,
                        )
                    ),
                    "__Arquivo Primeira Aparição": registro.get(
                        "nome_arquivo_primeira_aparicao",
                        "",
                    ),
                    "__Planejamento Base": bool(
                        registro.get(
                            "planejamento_base",
                            False,
                        )
                    ),
                }
            )

        dados.update(padrao)
        linhas.append(dados)

    return pd.DataFrame(linhas)


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def listar_bases() -> pd.DataFrame:
    registros = buscar_todos(
        "bases_importadas",
        ordem="data_operacional",
        desc=True,
    )
    return pd.DataFrame(registros)


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def listar_datas_completas_reais() -> list[str]:
    """
    Retorna somente datas com registros reais nas duas tabelas:
    atividades_planejadas e atividades_resultado.

    Isso evita que o seletor dependa exclusivamente de
    bases_importadas, que pode ficar desatualizada após reparos.
    """
    planejados = buscar_todos(
        "atividades_planejadas",
        colunas="data_operacional",
        ordem="data_operacional",
    )
    resultados = buscar_todos(
        "atividades_resultado",
        colunas="data_operacional",
        ordem="data_operacional",
    )

    datas_planejado = {
        texto_limpo(r.get("data_operacional", ""))
        for r in planejados
        if texto_limpo(r.get("data_operacional", ""))
    }
    datas_resultado = {
        texto_limpo(r.get("data_operacional", ""))
        for r in resultados
        if texto_limpo(r.get("data_operacional", ""))
    }

    return sorted(
        datas_planejado & datas_resultado,
        reverse=True,
    )


def excluir_base(tipo: str, data_operacional: str) -> None:
    cliente = exigir_supabase()
    tabela = TABELA_POR_TIPO[tipo]

    cliente.table(tabela).delete().eq(
        "data_operacional",
        data_operacional,
    ).execute()

    cliente.table("bases_importadas").delete().eq(
        "tipo",
        tipo,
    ).eq(
        "data_operacional",
        data_operacional,
    ).execute()

    invalidar_cache_dados()


@st.cache_data(ttl=CACHE_TTL_SEGUNDOS, show_spinner=False)
def carregar_consolidado(datas: list[str]) -> pd.DataFrame:
    """Concilia todas as datas completas e cria a visão histórica geral."""
    partes = []

    for data_operacional in datas:
        planejado = carregar_base("planejado", data_operacional)
        resultado = carregar_base("resultado", data_operacional)

        if planejado.empty and resultado.empty:
            continue

        conciliacao_data = conciliar_bases(
            planejado,
            resultado,
        )
        conciliacao_data.insert(
            0,
            "Data Operacional",
            data_operacional,
        )
        partes.append(conciliacao_data)

    if not partes:
        return pd.DataFrame()

    consolidado = pd.concat(
        partes,
        ignore_index=True,
        sort=False,
    )

    return aplicar_revisoes_md(
        consolidado
    )



# =========================================================
# AUDITORIA DE QUALIDADE DAS IMPRODUTIVAS
# =========================================================

def minutos_hhmm(valor) -> int | None:
    texto = texto_limpo(valor)

    if not texto:
        return None

    encontrado = re.search(
        r"(\d{1,2}):(\d{2})",
        texto,
    )

    if not encontrado:
        return None

    hora = int(encontrado.group(1))
    minuto = int(encontrado.group(2))

    if hora > 23 or minuto > 59:
        return None

    return hora * 60 + minuto


def minutos_duracao(valor) -> int | None:
    texto = texto_limpo(valor)

    if not texto:
        return None

    encontrado = re.search(
        r"(\d{1,3}):(\d{2})",
        texto,
    )

    if not encontrado:
        return None

    horas = int(encontrado.group(1))
    minutos = int(encontrado.group(2))

    return horas * 60 + minutos


def janela_contratual_minutos(valor) -> tuple[int | None, int | None]:
    """
    Interpreta formatos do OFS como:
    08 - 09
    8 - 9
    08:00 - 09:00
    """
    texto = texto_limpo(valor)

    if not texto:
        return None, None

    horas = re.findall(
        r"\b(\d{1,2})(?::(\d{2}))?\b",
        texto,
    )

    if len(horas) < 2:
        return None, None

    def converter(par):
        h = int(par[0])
        m = int(par[1] or 0)

        if h > 23 or m > 59:
            return None

        return h * 60 + m

    inicio = converter(horas[0])
    fim = converter(horas[1])

    return inicio, fim


def categorizar_texto_improdutiva(texto) -> set[str]:
    normal = normalizar_texto(texto)

    if not normal:
        return set()

    categorias = set()

    regras = {
        "Veículo indisponível": [
            "VEICULO",
            "CARRO",
            "CAMINHAO",
            "FROTA",
            "EM ROTA",
            "NAO ESTA NO LOCAL",
            "NAO CHEGOU",
            "INDISPONIVEL",
        ],
        "Equipamento / material": [
            "EQUIPAMENTO",
            "KIT",
            "MATERIAL",
            "PECA",
            "FERRAMENTA",
            "CHICOTE",
            "CABO",
            "INSUMO",
        ],
        "OS / cadastro / direcionamento": [
            "OS ABERTA",
            "OS ERRADA",
            "OS INCORRETA",
            "PROBLEMA NA OS",
            "ITEM INCORRETO",
            "CADASTRO",
            "NAO E MINHA REGIAO",
            "REGIAO",
            "DIRECION",
        ],
        "Sistema / suporte": [
            "VSERVICE",
            "V SERVICES",
            "SISTEMA",
            "TRAVOU",
            "SUPORTE",
            "APLICATIVO",
        ],
        "Portaria / acesso": [
            "PORTARIA",
            "LIBERACAO",
            "LIBERADO",
            "SEGURANCA",
            "ACESSO",
        ],
        "Cliente / agendamento": [
            "CLIENTE NAO SABIA",
            "NAO SABIA DO AGENDAMENTO",
            "AGENDAMENTO",
            "REAGENDAR",
            "OUTRA DATA",
            "CLIENTE SOLICITOU",
            "CLIENTE INFORMOU",
        ],
        "Técnico / capacidade": [
            "TECNICO",
            "EQUIPE",
            "CAPACIDADE",
            "MAO DE OBRA",
            "INDISPONIBILIDADE DO TECNICO",
        ],
        "Deslocamento / veículo do técnico": [
            "MEU VEICULO",
            "MEU CARRO",
            "MEU CAMINHAO",
            "NO MEIO DO CAMINHO",
            "NO CAMINHO",
            "A CAMINHO",
            "TIVE QUE REBOCAR",
            "GUINCHO",
            "PNEU FURADO",
            "PROBLEMA NO MEU VEICULO",
            "PROBLEMA COM MEU VEICULO",
            "PROBLEMA NO MEU CARRO",
            "PROBLEMA COM MEU CARRO",
        ],
        "Sinistro": [
            "SINISTRO",
            "ACIDENTE",
            "COLISAO",
        ],
    }

    for categoria, termos in regras.items():
        if any(
            termo in normal
            for termo in termos
        ):
            categorias.add(categoria)

    return categorias


def categorizar_razao_ofs(valor) -> set[str]:
    texto = normalizar_texto(valor)

    if not texto:
        return set()

    categorias = set()

    if any(
        termo in texto
        for termo in [
            "CARRO INDISPONIVEL",
            "VEICULO INDISPONIVEL",
            "INDISPONIBILIDADE DO VEICULO",
        ]
    ):
        categorias.add("Veículo indisponível")

    if any(
        termo in texto
        for termo in [
            "FALTA DE EQUIPAMENTO",
            "EQUIPAMENTO",
            "FERRAMENTA",
            "MATERIAL",
        ]
    ):
        categorias.add("Equipamento / material")

    if any(
        termo in texto
        for termo in [
            "SISTEMA",
            "PROBLEMAS TECNICOS COM SISTEMAS",
        ]
    ):
        categorias.add("Sistema / suporte")

    if any(
        termo in texto
        for termo in [
            "PROBLEMAS TECNICOS COM VEICULOS",
            "PROBLEMA TECNICO COM VEICULO",
        ]
    ):
        categorias.add("Veículo indisponível")

    if any(
        termo in texto
        for termo in [
            "PORTARIA",
            "NAO LIBERADO",
        ]
    ):
        categorias.add("Portaria / acesso")

    if any(
        termo in texto
        for termo in [
            "CLIENTE INFORMOU COM ANTECEDENCIA",
            "CLIENTE SOLICITOU",
        ]
    ):
        categorias.add("Cliente / agendamento")

    if any(
        termo in texto
        for termo in [
            "TECNICO",
            "EQUIPE",
            "CAPACIDADE",
        ]
    ):
        categorias.add("Técnico / capacidade")

    if "SINISTRO" in texto:
        categorias.add("Sinistro")

    return categorias


def analisar_qualidade_apontamento(
    motivo_ofs,
    observacao,
) -> tuple[str, str, str]:
    motivo = texto_limpo(motivo_ofs)
    obs = texto_limpo(observacao)

    if not obs:
        return (
            "⚪ Sem informação",
            "Observação técnica não preenchida.",
            "",
        )

    obs_norm = normalizar_texto(obs)

    justificativas_fracas = [
        "SO PRA TIRAR DA AGENDA",
        "SO PARA TIRAR DA AGENDA",
        "RETIRAR DA AGENDA",
        "TIRAR DA AGENDA",
        "NAO FOI POSSIVEL",
        "NAO DEU",
    ]

    if any(
        termo in obs_norm
        for termo in justificativas_fracas
    ):
        return (
            "🟠 Justificativa insuficiente",
            "O texto não descreve uma causa operacional suficiente.",
            "",
        )

    # Regra contextual prioritária:
    # expressões em primeira pessoa indicam problema do técnico/deslocamento,
    # e não indisponibilidade do veículo do cliente.
    sinais_veiculo_tecnico = [
        "MEU VEICULO",
        "MEU CARRO",
        "MEU CAMINHAO",
        "PROBLEMA NO MEU VEICULO",
        "PROBLEMA COM MEU VEICULO",
        "PROBLEMA NO MEU CARRO",
        "PROBLEMA COM MEU CARRO",
        "NO MEIO DO CAMINHO",
        "TIVE QUE REBOCAR",
        "A CAMINHO",
        "GUINCHO",
        "PNEU FURADO",
    ]

    contexto_veiculo_tecnico = any(
        termo in obs_norm
        for termo in sinais_veiculo_tecnico
    )

    cat_motivo = categorizar_razao_ofs(
        motivo
    )
    cat_obs = categorizar_texto_improdutiva(
        obs
    )

    if contexto_veiculo_tecnico:
        # Remove uma possível classificação genérica causada apenas
        # pelas palavras "veículo/carro".
        cat_obs.discard("Veículo indisponível")
        cat_obs.add("Deslocamento / veículo do técnico")

        if "Deslocamento / veículo do técnico" not in cat_motivo:
            return (
                "🔴 Divergente",
                (
                    "A observação indica problema no deslocamento ou no "
                    "veículo do próprio técnico, e não indisponibilidade "
                    "do veículo do cliente."
                ),
                "Deslocamento / veículo do técnico",
            )

    if not cat_obs:
        return (
            "🟡 Revisar",
            "A observação existe, mas a causa não pôde ser classificada com segurança.",
            "",
        )

    if not cat_motivo:
        sugerido = " / ".join(
            sorted(cat_obs)
        )
        return (
            "🟡 Revisar",
            "O motivo OFS não foi reconhecido pelo motor de regras.",
            sugerido,
        )

    intersecao = (
        cat_motivo
        & cat_obs
    )

    if intersecao:
        return (
            "🟢 Coerente",
            "Motivo selecionado e observação apresentam causa compatível.",
            "",
        )

    sugerido = " / ".join(
        sorted(cat_obs)
    )

    return (
        "🔴 Divergente",
        "A observação aponta para uma causa diferente do motivo selecionado no OFS.",
        sugerido,
    )


def analisar_conformidade_temporal(
    intervalo,
    inicio_real,
    fim_real,
    duracao,
) -> tuple[str, str, str]:
    janela_inicio, janela_fim = (
        janela_contratual_minutos(
            intervalo
        )
    )

    inicio = minutos_hhmm(
        inicio_real
    )
    fim = minutos_hhmm(
        fim_real
    )
    duracao_min = minutos_duracao(
        duracao
    )

    problemas = []

    if (
        janela_inicio is None
        or janela_fim is None
    ):
        status_janela = "Janela não informada"
    elif inicio is None:
        status_janela = "Início não informado"
        problemas.append(
            "Sem horário de início para validar a janela."
        )
    elif (
        inicio < janela_inicio
        or inicio > janela_fim
    ):
        status_janela = "Fora da janela"
        problemas.append(
            "Início da improdutiva fora da janela contratada."
        )
    else:
        status_janela = "Dentro da janela"

    if duracao_min is None:
        status_tempo = "Duração não informada"
        problemas.append(
            "Sem duração para validar os 30 minutos."
        )
    elif duracao_min < 30:
        status_tempo = "Tempo inferior a 30 min"
        problemas.append(
            f"Duração registrada de {duracao_min} min, abaixo dos 30 min."
        )
    else:
        status_tempo = "30 min ou mais"

    if (
        status_janela == "Dentro da janela"
        and status_tempo == "30 min ou mais"
    ):
        return (
            "🟢 Conforme",
            (
                "Início dentro da janela e tempo de improdutividade "
                "igual ou superior a 30 minutos."
            ),
            "Elegível para análise de cobrança",
        )

    if (
        status_janela in {
            "Janela não informada",
            "Início não informado",
        }
        or status_tempo == "Duração não informada"
    ):
        return (
            "🟡 Revisar",
            " ".join(problemas)
            or "Não há informação suficiente para concluir.",
            "Revisar evidências",
        )

    return (
        "🔴 Risco de faturamento",
        " ".join(problemas),
        "Possivelmente não faturável",
    )


def enriquecer_auditoria_improdutivas(
    df: pd.DataFrame,
) -> pd.DataFrame:
    resultado = df.copy()

    qualidade = resultado.apply(
        lambda linha: analisar_qualidade_apontamento(
            linha.get(
                "Razao_improdutiva",
                "",
            ),
            linha.get(
                "Observacao_tecnico_improdutiva",
                "",
            ),
        ),
        axis=1,
        result_type="expand",
    )

    qualidade.columns = [
        "Qualidade apontamento",
        "Leitura qualidade",
        "Motivo sugerido",
    ]

    resultado = pd.concat(
        [
            resultado.reset_index(
                drop=True
            ),
            qualidade.reset_index(
                drop=True
            ),
        ],
        axis=1,
    )

    temporal = resultado.apply(
        lambda linha: analisar_conformidade_temporal(
            linha.get(
                "Intervalo_tempo",
                "",
            ),
            linha.get(
                "Inicio_real",
                "",
            ),
            linha.get(
                "Fim_real",
                "",
            ),
            linha.get(
                "Duracao_real",
                "",
            ),
        ),
        axis=1,
        result_type="expand",
    )

    temporal.columns = [
        "Conformidade temporal",
        "Leitura temporal",
        "Elegibilidade",
    ]

    resultado = pd.concat(
        [
            resultado.reset_index(
                drop=True
            ),
            temporal.reset_index(
                drop=True
            ),
        ],
        axis=1,
    )

    return resultado




# =========================================================
# APRESENTAÇÃO E DETALHAMENTO CLICÁVEL
# =========================================================

def definir_filtro_detalhe(
    escopo: str,
    classificacao: str,
) -> None:
    st.session_state["detalhe_ativo"] = {
        "escopo": escopo,
        "filtro": classificacao,
    }


def exibir_card_clicavel(
    coluna,
    titulo: str,
    valor: int,
    filtro: str,
    prefixo: str,
    ajuda: str | None = None,
) -> None:
    coluna.metric(titulo, valor, help=ajuda)

    coluna.button(
        "🔎 Ver OS",
        key=f"{prefixo}_{normalizar_texto(filtro)}",
        on_click=definir_filtro_detalhe,
        args=(prefixo, filtro),
        use_container_width=True,
    )


def exibir_cards_indicadores(
    indicadores: dict,
    prefixo: str,
) -> None:
    colunas = st.columns(6)

    configuracoes = [
        (
            "Manutenções agendadas",
            "Planejadas",
            "Manutenções agendadas",
            (
                "Total de manutenções reconhecidas na fotografia-base do "
                "planejamento. É o denominador da MCI, da perda de OS e do "
                "cancelamento."
            ),
        ),
        (
            "Agendadas executadas",
            "Executadas planejadas",
            "Executada agendada",
            (
                "Manutenções concluídas que já faziam parte do "
                "planejamento-base. Continuam separadas das extras para "
                "mostrar a qualidade do agendamento."
            ),
        ),
        (
            "Improdutivas",
            "Improdutivas",
            "Improdutivas",
            (
                "Total de atendimentos improdutivos: improdutivas agendadas "
                "+ improdutivas extras. Entram no cálculo da MD."
            ),
        ),
        (
            "Canceladas",
            "Canceladas",
            "Cancelada",
            (
                "Manutenções do planejamento-base classificadas como "
                "canceladas. Índice de cancelamento = Canceladas ÷ "
                "Manutenções agendadas × 100."
            ),
        ),
        (
            "OS Perdidas",
            "OS Perdidas",
            "OS Perdida",
            (
                "Manutenções do planejamento-base em que o atendimento não "
                "teve o desfecho localizado pelas regras do painel. "
                "Taxa de perda de OS = OS Perdidas ÷ "
                "Manutenções agendadas × 100."
            ),
        ),
        (
            "Executadas extras",
            "Executadas extras",
            "Executada extra",
            (
                "Manutenções concluídas que não pertenciam ao "
                "planejamento-base. Entram na MCI e na base da MD, mas "
                "permanecem separadas para evidenciar encaixes e oportunidades "
                "de melhoria do agendamento."
            ),
        ),
    ]

    for coluna, (rotulo, chave_indicador, filtro, ajuda) in zip(
        colunas,
        configuracoes,
    ):
        exibir_card_clicavel(
            coluna,
            rotulo,
            indicadores[chave_indicador],
            filtro,
            prefixo,
            ajuda,
        )

    st.markdown("#### Indicadores de desempenho")
    i1, i2, i3, i4, i5 = st.columns(5)

    i1.metric(
        "MCI — Execução",
        f'{indicadores["MCI"]:.1f}%',
        help=(
            "Fórmula vigente: (Agendadas executadas + Executadas extras) ÷ "
            "Planejadas elegíveis MCI × 100. Planejadas elegíveis MCI = "
            "Manutenções agendadas menos improdutivas agendadas com os motivos "
            "'Problemas técnicos com sistemas' e 'Problemas técnicos com veículos'. "
            "Essas OS permanecem visíveis no painel de improdutividade, mas não "
            "penalizam a MCI."
        ),
    )
    i2.metric(
        "MD — Improdutividade",
        f'{indicadores["MD"]:.1f}%',
        help=(
            "Fórmula vigente: Improdutivas consideradas ÷ "
            "(Executadas agendadas + Executadas extras + Improdutivas consideradas) "
            "× 100. São expurgadas da MD as improdutivas com os motivos "
            "'Problemas técnicos com sistemas' e 'Problemas técnicos com veículos'. "
            "As OS continuam visíveis no detalhamento. Quando houver revisão gerencial, somente a MD usa o motivo validado; o restante do sistema preserva o motivo original do OFS."
        ),
    )
    i3.metric(
        "Taxa de perda de OS",
        f'{indicadores["Taxa de perda de OS"]:.1f}%',
        help=(
            "Percentual do planejamento-base classificado como OS Perdida. "
            "Fórmula: OS Perdidas ÷ Manutenções agendadas × 100."
        ),
    )
    i4.metric(
        "Índice de cancelamento",
        f'{indicadores["Índice cancelamento"]:.1f}%',
        help=(
            "Percentual do planejamento-base que foi cancelado. "
            "Fórmula: Canceladas ÷ Manutenções agendadas × 100."
        ),
    )
    i5.metric(
        "Execução total",
        f'{indicadores["Execução total"]:.1f}%',
        help=(
            "Usa a mesma base matemática da MCI. Fórmula: "
            "(Agendadas executadas + Executadas extras) ÷ Planejadas elegíveis MCI "
            "× 100, após o expurgo das improdutivas agendadas por problemas "
            "técnicos com sistemas e com veículos."
        ),
    )

    if indicadores.get("Improdutivas expurgadas", 0):
        st.caption(
            "ℹ️ Regra vigente: "
            f'{indicadores["Improdutivas expurgadas"]} improdutiva(s) '
            "foram mantidas no histórico/detalhamento, mas expurgadas de MCI/MD "
            "por motivo técnico de sistema ou veículo."
        )


def filtrar_detalhes(
    conciliacao: pd.DataFrame,
    filtro: str,
) -> pd.DataFrame:
    # Compatibilidade da nomenclatura do card (plural) com a
    # classificação das linhas da conciliação (singular).
    if filtro == "OS Perdidas":
        filtro = "OS Perdida"

    if filtro in {"Planejadas", "Manutenções agendadas"}:
        if "Origem Agendamento" in conciliacao.columns:
            return conciliacao[
                conciliacao["Origem Agendamento"]
                == "Agendada"
            ].copy()

        classes = [
            "Executada agendada",
            "Improdutiva agendada",
            "Cancelada",
            "Cancelada no agendamento",
            "Retirada do agendamento",
            "Possível substituição de OS",
            "OS Perdida",
            "Status intermediário agendado",
        ]
        return conciliacao[
            conciliacao["Classificação"].isin(classes)
        ].copy()

    if filtro == "Improdutivas":
        return conciliacao[
            conciliacao["Classificação"].isin(
                [
                    "Improdutiva agendada",
                    "Improdutiva extra",
                ]
            )
        ].copy()

    return conciliacao[
        conciliacao["Classificação"] == filtro
    ].copy()


def exibir_detalhamento(
    conciliacao: pd.DataFrame,
    escopo: str,
    contexto: str,
) -> None:
    detalhe_ativo = st.session_state.get("detalhe_ativo")

    if not detalhe_ativo:
        return

    if detalhe_ativo.get("escopo") != escopo:
        return

    filtro = detalhe_ativo.get("filtro")

    if not filtro:
        return

    detalhe = filtrar_detalhes(
        conciliacao,
        filtro,
    )

    st.markdown("---")
    st.subheader(f"🔎 Conferência das OS — {filtro}")
    st.caption(
        f"Contexto: {contexto}. Foram encontrados "
        f"{len(detalhe)} atendimento(s)."
    )

    colunas = [
        "Data Operacional",
        "Classificação",
        "Ticket",
        "Placa",
        "Oficina",
        "Consultor",
        "UF-base",
        "OS_planejada",
        "OS_resultado",
        "Troca de OS",
        "Regra Aplicada",
        "Origem Agendamento",
        "Planejamento_base",
        "Primeira_aparicao_data",
        "Data_operacional_planejada",
        "Status_planejado",
        "Status_resultado",
        "Razao_improdutiva",
        "Observacao_tecnico_improdutiva",
        "Motivo da Classificação",
        "Qtd_planejada",
        "Qtd_resultado",
    ]
    colunas = [
        coluna
        for coluna in colunas
        if coluna in detalhe.columns
    ]

    detalhe_exibicao = detalhe[colunas].copy()

    detalhe_exibicao = detalhe_exibicao.rename(
        columns={
            "Razao_improdutiva": "Razão da Improdutiva",
            "Observacao_tecnico_improdutiva": (
                "Observação do Técnico (Improdutiva)"
            ),
        }
    )

    st.dataframe(
        detalhe_exibicao,
        use_container_width=True,
        hide_index=True,
        height=480,
    )

    a, b = st.columns([1, 4])

    if a.button(
        "✖ Fechar",
        key=f"fechar_{escopo}",
        use_container_width=True,
    ):
        st.session_state["detalhe_ativo"] = None
        st.rerun()

    nome_filtro = (
        normalizar_texto(filtro)
        .lower()
        .replace(" ", "_")
    )

    b.download_button(
        "⬇️ Baixar OS filtradas em Excel",
        data=dataframe_para_excel(
            detalhe,
            "OS_filtradas",
        ),
        file_name=f"os_{nome_filtro}.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        key=f"download_{escopo}_{nome_filtro}",
        use_container_width=True,
    )


def dataframe_para_excel(
    df: pd.DataFrame,
    aba: str = "Dados",
) -> bytes:
    buffer = io.BytesIO()

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(
            writer,
            index=False,
            sheet_name=aba[:31],
        )

    buffer.seek(0)
    return buffer.getvalue()



# =========================================================
# FOLLOW — WHATSAPP, FORMULÁRIO E MÉTRICAS
# =========================================================

MOTIVOS_IMPEDIMENTO = [
    "Veículo indisponível",
    "Cliente solicitou alteração da data",
    "Falta de equipamento ou ferramenta",
    "Falta de peça ou insumo",
    "Problema técnico sem solução",
    "Técnico/equipe indisponível",
    "Oficina sem capacidade para a data",
    "Dificuldade de acesso ou deslocamento",
    "Dados/OS insuficientes para executar",
    "Outro",
]


def obter_url_publica_app() -> str:
    """
    Usa a URL atual do Streamlit para montar o link público do formulário.
    Se APP_PUBLIC_URL estiver configurada nos Secrets, ela tem prioridade.
    """
    configurada = texto_limpo(
        st.secrets.get("APP_PUBLIC_URL", "")
    )

    if configurada:
        return configurada.rstrip("/")

    try:
        atual = str(st.context.url)
        partes = urlsplit(atual)
        return urlunsplit(
            (
                partes.scheme,
                partes.netloc,
                partes.path,
                "",
                "",
            )
        ).rstrip("/")
    except Exception:
        return ""


def montar_url_formulario_follow(token: str) -> str:
    base = obter_url_publica_app()

    if not base:
        return ""

    return f"{base}?follow_token={quote(token)}"


def buscar_follow_por_token(token: str) -> dict | None:
    cliente = exigir_supabase()
    resposta = (
        cliente.table("follow_contatos")
        .select("*")
        .eq("token", token)
        .limit(1)
        .execute()
    )

    if not resposta.data:
        return None

    return resposta.data[0]


def obter_ou_criar_follow(
    data_manutencao: str,
    chave_oficina: str,
    oficina: str,
    consultor: str,
    telefone: str,
    qtd_agendadas: int,
    os_agendadas: list[str],
) -> dict:
    cliente = exigir_supabase()

    resposta = (
        cliente.table("follow_contatos")
        .select("*")
        .eq("data_manutencao", data_manutencao)
        .eq("chave_oficina", chave_oficina)
        .eq("consultor", consultor)
        .limit(1)
        .execute()
    )

    token = (
        texto_limpo(resposta.data[0].get("token"))
        if resposta.data
        else str(uuid.uuid4())
    )

    url_formulario = montar_url_formulario_follow(token)

    mensagem = (
        f"Olá! Sua oficina possui {qtd_agendadas} "
        f"manutenção(ões) agendada(s) para "
        f"{pd.to_datetime(data_manutencao).strftime('%d/%m/%Y')}. "
        f"Precisamos confirmar se existe algum impedimento para a execução. "
        f"Por favor, responda o formulário: {url_formulario}"
    )

    registro = {
        "token": token,
        "data_follow": datetime.now(
            FUSO_BRASIL
        ).date().isoformat(),
        "data_manutencao": data_manutencao,
        "chave_oficina": chave_oficina,
        "oficina": oficina,
        "consultor": consultor or "Não definido",
        "telefone": telefone,
        "qtd_agendadas": int(qtd_agendadas),
        "os_agendadas": os_agendadas,
        "mensagem": mensagem,
        "ultima_atualizacao": datetime.now(
            FUSO_BRASIL
        ).isoformat(),
    }

    if resposta.data:
        cliente.table("follow_contatos").update(
            registro
        ).eq(
            "id",
            resposta.data[0]["id"],
        ).execute()
    else:
        registro["status"] = "Preparado"
        registro["preparado_em"] = datetime.now(
            FUSO_BRASIL
        ).isoformat()
        cliente.table("follow_contatos").insert(
            registro
        ).execute()

    atualizado = (
        cliente.table("follow_contatos")
        .select("*")
        .eq("token", token)
        .limit(1)
        .execute()
    )

    return atualizado.data[0]


def registrar_envio_follow(follow_id: int) -> None:
    cliente = exigir_supabase()
    agora = datetime.now(FUSO_BRASIL).isoformat()

    cliente.table("follow_contatos").update(
        {
            "status": "Enviado",
            "enviado_em": agora,
            "ultima_atualizacao": agora,
        }
    ).eq("id", follow_id).execute()

    invalidar_cache_dados()


def botao_whatsapp_web(
    telefone: str,
    mensagem: str,
    identificador: str,
) -> None:
    """
    Abre exclusivamente o WhatsApp Web.
    Usa o componente nativo do Streamlit para evitar exibição de HTML bruto.
    Não chama whatsapp:// nem WhatsApp.exe.
    """
    numero = limpar_telefone(telefone)

    if not numero:
        st.warning("Sem WhatsApp cadastrado.")
        return

    texto_url = quote(mensagem)
    link_web = (
        f"https://web.whatsapp.com/send"
        f"?phone={numero}&text={texto_url}"
    )

    st.link_button(
        "📱 Abrir no WhatsApp Web",
        link_web,
        use_container_width=True,
    )


def exibir_formulario_publico_follow() -> bool:
    """
    Se houver ?follow_token=..., a página vira somente o formulário
    público da oficina. O técnico não precisa acessar o restante do painel.
    """
    token = texto_limpo(
        st.query_params.get("follow_token", "")
    )

    if not token:
        return False

    exigir_supabase()
    follow = buscar_follow_por_token(token)

    st.title("✅ Confirmação de Manutenção")
    st.caption(
        "Formulário de confirmação preventiva da oficina."
    )

    if follow is None:
        st.error(
            "Este formulário não foi encontrado ou o link é inválido."
        )
        return True

    data_formatada = pd.to_datetime(
        follow["data_manutencao"]
    ).strftime("%d/%m/%Y")

    st.info(
        f"Oficina: **{texto_limpo(follow.get('oficina'))}** · "
        f"Data: **{data_formatada}** · "
        f"Manutenções: **{int(follow.get('qtd_agendadas') or 0)}**"
    )

    os_disponiveis = [
        texto_limpo(valor)
        for valor in (follow.get("os_agendadas") or [])
        if texto_limpo(valor)
    ]

    with st.form("form_follow_publico"):
        nome_respondente = st.text_input(
            "Seu nome"
        )

        equipamentos = st.radio(
            "Você possui todos os equipamentos/ferramentas necessários?",
            ["Sim", "Não"],
            horizontal=True,
        )

        veiculo = st.radio(
            "O(s) veículo(s) estará(ão) disponível(is) para atendimento?",
            ["Sim", "Não", "Não sei"],
            horizontal=True,
        )

        capacidade = st.radio(
            "A oficina possui técnico/capacidade para atender na data?",
            ["Sim", "Não"],
            horizontal=True,
        )

        impedimento = st.radio(
            "Existe algum impedimento que possa impedir a execução?",
            ["Não, está tudo OK", "Sim, existe impedimento"],
        )

        tem_impedimento = (
            impedimento == "Sim, existe impedimento"
        )

        motivos = []
        os_afetadas = []
        observacao = ""
        previsao = ""

        if tem_impedimento:
            motivos = st.multiselect(
                "Qual(is) o(s) impedimento(s)?",
                MOTIVOS_IMPEDIMENTO,
            )

            if os_disponiveis:
                os_afetadas = st.multiselect(
                    "Quais OS podem ser afetadas?",
                    os_disponiveis,
                )

            observacao = st.text_area(
                "Explique o impedimento",
                placeholder=(
                    "Descreva o que pode impedir a execução e "
                    "qual apoio seria necessário."
                ),
            )

            previsao = st.text_input(
                "Previsão para solução/regularização (opcional)"
            )
        else:
            observacao = st.text_area(
                "Observação (opcional)"
            )

        enviar = st.form_submit_button(
            "Enviar confirmação",
            type="primary",
            use_container_width=True,
        )

    if enviar:
        if not texto_limpo(nome_respondente):
            st.error("Informe seu nome antes de enviar.")
            return True

        if tem_impedimento and not motivos:
            st.error(
                "Selecione ao menos um motivo do impedimento."
            )
            return True

        cliente = exigir_supabase()
        agora = datetime.now(FUSO_BRASIL).isoformat()

        cliente.table("follow_respostas").insert(
            {
                "follow_id": follow["id"],
                "token": token,
                "nome_respondente": nome_respondente,
                "equipamentos_ok": equipamentos == "Sim",
                "veiculo_disponivel": veiculo,
                "capacidade_ok": capacidade == "Sim",
                "tem_impedimento": tem_impedimento,
                "motivos": motivos,
                "os_afetadas": os_afetadas,
                "observacao": observacao,
                "previsao_solucao": previsao,
                "respondido_em": agora,
            }
        ).execute()

        status_resposta = (
            "Com impedimento"
            if tem_impedimento
            else "Sem impedimento"
        )

        cliente.table("follow_contatos").update(
            {
                "status": "Respondido",
                "respondido_em": agora,
                "tem_impedimento": tem_impedimento,
                "status_resposta": status_resposta,
                "ultima_atualizacao": agora,
            }
        ).eq(
            "id",
            follow["id"],
        ).execute()

        st.success(
            "Resposta registrada. Obrigado pela confirmação."
        )

        if tem_impedimento:
            st.warning(
                "O impedimento foi registrado para acompanhamento."
            )
        else:
            st.success(
                "Atendimento confirmado sem impedimento informado."
            )

    return True


def carregar_metricas_follow(
    consultor: str,
    data_manutencao: str,
) -> pd.DataFrame:
    registros = buscar_todos(
        "follow_contatos",
        filtros={
            "consultor": consultor,
            "data_manutencao": data_manutencao,
        },
        ordem="id",
    )

    return pd.DataFrame(registros)


def exibir_metricas_follow(
    consultor: str,
    data_manutencao: str,
    total_oficinas: int,
) -> None:
    contatos = carregar_metricas_follow(
        consultor,
        data_manutencao,
    )

    if contatos.empty:
        preparados = enviados = respondidos = 0
        com_impedimento = sem_impedimento = 0
    else:
        preparados = len(contatos)
        enviados = int(
            contatos["enviado_em"].notna().sum()
            if "enviado_em" in contatos.columns
            else 0
        )
        respondidos = int(
            contatos["respondido_em"].notna().sum()
            if "respondido_em" in contatos.columns
            else 0
        )
        com_impedimento = int(
            (
                contatos.get(
                    "tem_impedimento",
                    pd.Series(dtype=bool),
                )
                == True
            ).sum()
        )
        sem_impedimento = int(
            (
                contatos.get(
                    "tem_impedimento",
                    pd.Series(dtype=bool),
                )
                == False
            ).sum()
        )

    sem_resposta = max(enviados - respondidos, 0)

    st.markdown("#### Métricas do Follow")
    c1, c2, c3, c4, c5, c6 = st.columns(6)

    c1.metric("Oficinas no follow", total_oficinas)
    c2.metric("Preparadas", preparados)
    c3.metric("Enviadas", enviados)
    c4.metric("Respondidas", respondidos)
    c5.metric("Com impedimento", com_impedimento)
    c6.metric("Sem resposta", sem_resposta)

    if respondidos:
        taxa = respondidos / enviados * 100 if enviados else 0
        st.caption(
            f"Taxa de resposta: **{taxa:.1f}%** · "
            f"Sem impedimento: **{sem_impedimento}**."
        )


def exibir_respostas_follow(
    consultor: str,
    data_manutencao: str,
) -> None:
    contatos = carregar_metricas_follow(
        consultor,
        data_manutencao,
    )

    if contatos.empty:
        return

    respondidos = contatos[
        contatos["respondido_em"].notna()
    ].copy()

    if respondidos.empty:
        return

    st.markdown("### Respostas recebidas")
    st.caption(
        "Detalhamento das confirmações e dos impedimentos "
        "informados pelas oficinas."
    )

    cliente = exigir_supabase()

    for _, contato in respondidos.sort_values(
        "respondido_em",
        ascending=False,
    ).iterrows():
        follow_id = int(contato["id"])

        resposta_db = (
            cliente.table("follow_respostas")
            .select("*")
            .eq("follow_id", follow_id)
            .order("respondido_em", desc=True)
            .limit(1)
            .execute()
        )

        resposta = (
            resposta_db.data[0]
            if resposta_db.data
            else {}
        )

        tem_impedimento = bool(
            contato.get("tem_impedimento")
        )

        oficina = texto_limpo(
            contato.get("oficina")
        )
        qtd = int(
            contato.get("qtd_agendadas") or 0
        )

        status_visual = (
            "⚠️ COM IMPEDIMENTO"
            if tem_impedimento
            else "✅ SEM IMPEDIMENTO"
        )

        # Supabase grava timestamptz em UTC; convertemos para Brasília.
        respondido_em = contato.get("respondido_em")
        data_hora = "Não informado"

        if respondido_em:
            try:
                dt = pd.to_datetime(
                    respondido_em,
                    utc=True,
                ).tz_convert(FUSO_BRASIL)
                data_hora = dt.strftime(
                    "%d/%m/%Y às %H:%M"
                )
            except Exception:
                data_hora = texto_limpo(
                    respondido_em
                )

        with st.container(border=True):
            st.markdown(
                f"#### {oficina} — {status_visual}"
            )

            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Manutenções no Follow",
                qtd,
            )
            c2.metric(
                "Situação",
                (
                    "Com impedimento"
                    if tem_impedimento
                    else "Sem impedimento"
                ),
            )
            c3.metric(
                "Respondido",
                data_hora,
            )

            nome = texto_limpo(
                resposta.get("nome_respondente")
            )
            if nome:
                st.caption(
                    f"Respondente: **{nome}**"
                )

            equipamentos_ok = resposta.get(
                "equipamentos_ok"
            )
            veiculo = texto_limpo(
                resposta.get("veiculo_disponivel")
            )
            capacidade_ok = resposta.get(
                "capacidade_ok"
            )

            d1, d2, d3 = st.columns(3)

            if equipamentos_ok is True:
                d1.success("🧰 Equipamentos: OK")
            elif equipamentos_ok is False:
                d1.error(
                    "🧰 Equipamentos: NÃO"
                )
            else:
                d1.info(
                    "🧰 Equipamentos: não informado"
                )

            if veiculo == "Sim":
                d2.success(
                    "🚚 Veículo disponível: SIM"
                )
            elif veiculo == "Não":
                d2.error(
                    "🚚 Veículo disponível: NÃO"
                )
            else:
                d2.warning(
                    f"🚚 Veículo disponível: "
                    f"{veiculo or 'não informado'}"
                )

            if capacidade_ok is True:
                d3.success(
                    "👷 Capacidade técnica: OK"
                )
            elif capacidade_ok is False:
                d3.error(
                    "👷 Capacidade técnica: NÃO"
                )
            else:
                d3.info(
                    "👷 Capacidade técnica: não informada"
                )

            motivos = resposta.get(
                "motivos"
            ) or []
            os_afetadas = resposta.get(
                "os_afetadas"
            ) or []
            observacao = texto_limpo(
                resposta.get("observacao")
            )
            previsao = texto_limpo(
                resposta.get("previsao_solucao")
            )

            if tem_impedimento:
                st.markdown("**Impedimento informado**")

                if motivos:
                    st.write(
                        " • ".join(
                            str(motivo)
                            for motivo in motivos
                        )
                    )
                else:
                    st.write(
                        "Motivo não detalhado."
                    )

                if os_afetadas:
                    st.markdown(
                        "**OS possivelmente afetadas:** "
                        + ", ".join(
                            str(os_numero)
                            for os_numero in os_afetadas
                        )
                    )

                if observacao:
                    st.markdown(
                        f"**Observação:** {observacao}"
                    )

                if previsao:
                    st.markdown(
                        f"**Previsão de solução:** {previsao}"
                    )
            elif observacao:
                st.markdown(
                    f"**Observação:** {observacao}"
                )

    st.markdown("#### Resumo do acompanhamento")

    total = len(contatos)
    qtd_respondidos = len(respondidos)
    com_impedimento = int(
        (
            respondidos["tem_impedimento"]
            == True
        ).sum()
    )
    sem_impedimento = int(
        (
            respondidos["tem_impedimento"]
            == False
        ).sum()
    )
    sem_resposta = max(
        total - qtd_respondidos,
        0,
    )

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Respondidas", qtd_respondidos)
    r2.metric(
        "Sem impedimento",
        sem_impedimento,
    )
    r3.metric(
        "Com impedimento",
        com_impedimento,
    )
    r4.metric(
        "Sem resposta",
        sem_resposta,
    )


def atualizar_acao_follow(
    follow_id: int,
    status_acao: str,
    responsavel_acao: str,
    acao_tomada: str,
    prazo_acao: str,
    observacao_acao: str,
) -> None:
    cliente = exigir_supabase()
    agora = datetime.now(FUSO_BRASIL).isoformat()

    cliente.table("follow_contatos").update(
        {
            "status_acao": status_acao,
            "responsavel_acao": responsavel_acao,
            "acao_tomada": acao_tomada,
            "prazo_acao": prazo_acao or None,
            "observacao_acao": observacao_acao,
            "acao_atualizada_em": agora,
            "ultima_atualizacao": agora,
        }
    ).eq("id", follow_id).execute()

    invalidar_cache_dados()


def carregar_resposta_mais_recente_follow(follow_id: int) -> dict:
    cliente = exigir_supabase()

    resposta = (
        cliente.table("follow_respostas")
        .select("*")
        .eq("follow_id", follow_id)
        .order("respondido_em", desc=True)
        .limit(1)
        .execute()
    )

    if not resposta.data:
        return {}

    return resposta.data[0]


def classificar_risco_follow(contato: dict, resposta: dict) -> str:
    """
    Classificação operacional simples para priorização do retorno.
    Não altera MCI/MD; serve apenas para gestão de ações.
    """
    if not contato.get("respondido_em"):
        return "Sem resposta"

    if not bool(contato.get("tem_impedimento")):
        return "Baixo"

    equipamentos_ok = resposta.get("equipamentos_ok")
    capacidade_ok = resposta.get("capacidade_ok")
    veiculo = texto_limpo(resposta.get("veiculo_disponivel", ""))
    motivos = resposta.get("motivos") or []

    if (
        equipamentos_ok is False
        or capacidade_ok is False
        or veiculo == "Não"
        or len(motivos) >= 2
    ):
        return "Alto"

    return "Médio"


def exibir_cards_follow_acao(df: pd.DataFrame) -> None:
    total = len(df)

    sem_resposta = int(
        (df["Risco"] == "Sem resposta").sum()
    )
    com_impedimento = int(
        (df["Tem impedimento"] == True).sum()
    )
    alto = int(
        (df["Risco"] == "Alto").sum()
    )
    pendentes_acao = int(
        df["Status ação"].isin(
            ["Não iniciado", "Em andamento"]
        ).sum()
    )
    concluidas = int(
        (df["Status ação"] == "Concluído").sum()
    )

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Oficinas", total)
    c2.metric("Sem resposta", sem_resposta)
    c3.metric("Com impedimento", com_impedimento)
    c4.metric("Risco alto", alto)
    c5.metric("Ações pendentes", pendentes_acao)
    c6.metric("Ações concluídas", concluidas)




# =========================================================
# FOLLOW × RESULTADO OFS — COERÊNCIA E EFETIVIDADE
# =========================================================


def categorizar_motivo_md_dashboard(valor: str) -> str:
    """Categoria executiva dos motivos de improdutividade, sem alterar a regra operacional."""
    texto = normalizar_texto(valor)

    if not texto:
        return "Não informado"

    if any(termo in texto for termo in [
        "PROBLEMAS TECNICOS COM SISTEMAS",
        "PROBLEMA TECNICO COM SISTEMA",
        "SISTEMA",
        "APLICATIVO",
        "APP",
        "VSERVICE",
        "V SERVICES",
    ]):
        return "Problemas sistêmicos"

    if any(termo in texto for termo in [
        "PROBLEMAS TECNICOS COM VEICULOS",
        "PROBLEMA TECNICO COM VEICULO",
        "PROBLEMAS TECNICOS COM O VEICULO",
    ]):
        return "Problemas técnicos com veículos"

    if any(termo in texto for termo in [
        "CARRO INDISPONIVEL",
        "VEICULO INDISPONIVEL",
        "INDISPONIBILIDADE DO VEICULO",
        "VEICULO NAO DISPONIVEL",
        "CARRO NAO DISPONIVEL",
    ]):
        return "Veículo indisponível"

    if any(termo in texto for termo in [
        "PORTARIA",
        "NAO LIBERADO",
        "NAO FOI LIBERADO",
        "ACESSO",
    ]):
        return "Portaria / acesso"

    if any(termo in texto for termo in [
        "EQUIPAMENTO",
        "FERRAMENTA",
        "MATERIAL",
        "PECA",
        "INSUMO",
        "CABO",
        "CHICOTE",
    ]):
        return "Equipamento / material"

    if any(termo in texto for termo in [
        "TECNICO",
        "EQUIPE",
        "CAPACIDADE",
        "MAO DE OBRA",
    ]):
        return "Técnico / capacidade"

    if any(termo in texto for termo in [
        "CLIENTE",
        "SOLICITOU",
        "AGENDAMENTO",
        "DESAGEND",
    ]):
        return "Cliente / agendamento"

    if "SINISTRO" in texto or "ACIDENT" in texto:
        return "Sinistro"

    valor_limpo = texto_limpo(valor)
    return valor_limpo if valor_limpo else "Outro"


def preparar_diagnostico_md(base: pd.DataFrame) -> pd.DataFrame:
    if base.empty:
        return pd.DataFrame()

    improdutivas = base[
        base["Classificação"].isin([
            "Improdutiva agendada",
            "Improdutiva extra",
        ])
    ].copy()

    if improdutivas.empty:
        return improdutivas

    if "Consultor" not in improdutivas.columns:
        improdutivas["Consultor"] = "Não definido"

    improdutivas["Região"] = improdutivas["Consultor"].map(
        REGIOES_CONSULTORES
    ).fillna("Não definida")

    if "Razao_improdutiva" not in improdutivas.columns:
        improdutivas["Razao_improdutiva"] = ""

    improdutivas["Motivo MD"] = improdutivas[
        "Razao_improdutiva"
    ].apply(categorizar_motivo_md_dashboard)

    return improdutivas


def resumo_motivos_md(improdutivas: pd.DataFrame) -> pd.DataFrame:
    if improdutivas.empty:
        return pd.DataFrame(columns=[
            "Motivo", "Quantidade", "% das improdutivas"
        ])

    resumo = (
        improdutivas["Motivo MD"]
        .fillna("Não informado")
        .replace("", "Não informado")
        .value_counts()
        .rename_axis("Motivo")
        .reset_index(name="Quantidade")
    )

    total = int(resumo["Quantidade"].sum())
    resumo["% das improdutivas"] = (
        resumo["Quantidade"] / total * 100 if total else 0.0
    )
    return resumo


def ranking_md_dimensao(base: pd.DataFrame, dimensao: str, minimo_base_md: int = 1) -> pd.DataFrame:
    if base.empty or dimensao not in base.columns:
        return pd.DataFrame()

    linhas = []
    for valor, grupo in base.groupby(dimensao, dropna=False):
        nome = texto_limpo(valor) or "Não definido"
        indicadores = calcular_indicadores(grupo)
        executadas = (
            indicadores["Executadas planejadas"]
            + indicadores["Executadas extras"]
        )
        improdutivas = indicadores["Improdutivas"]
        improdutivas_consideradas = indicadores[
            "Improdutivas consideradas MD"
        ]
        expurgadas = indicadores["Improdutivas expurgadas"]
        base_md = indicadores["Base MD"]

        if base_md < minimo_base_md:
            continue

        imp_grupo = preparar_diagnostico_md(grupo)
        motivos = resumo_motivos_md(imp_grupo)
        if motivos.empty:
            principal_motivo = "Sem improdutivas"
            perc_motivo = 0.0
        else:
            principal_motivo = str(motivos.iloc[0]["Motivo"])
            perc_motivo = float(motivos.iloc[0]["% das improdutivas"])

        linha = {
            dimensao: nome,
            "Executadas": executadas,
            "Improdutivas": improdutivas,
            "Improdutivas consideradas": improdutivas_consideradas,
            "Expurgadas": expurgadas,
            "Base MD": base_md,
            "MD (%)": indicadores["MD"],
            "Principal motivo": principal_motivo,
            "% motivo": perc_motivo,
        }

        if dimensao == "Oficina":
            consultores = grupo["Consultor"].dropna().astype(str).map(texto_limpo)
            consultores = consultores[consultores != ""]
            consultor = consultores.mode().iloc[0] if not consultores.empty else "Não definido"
            linha["Consultor"] = consultor
            linha["Região"] = REGIOES_CONSULTORES.get(consultor, "Não definida")

        linhas.append(linha)

    if not linhas:
        return pd.DataFrame()

    return pd.DataFrame(linhas).sort_values(
        ["MD (%)", "Improdutivas", "Base MD"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def exibir_diagnostico_md_semanal(base_semana: pd.DataFrame, inicio_semana, fim_semana) -> None:
    st.markdown("### 🧭 MD — Diagnóstico da semana")
    st.caption(
        "Leitura executiva da improdutividade. A MD expurga problemas técnicos "
        "com sistemas e problemas técnicos com veículos, mas o ranking de motivos "
        "continua exibindo todas as improdutivas para análise e plano de ação. "
        "Canceladas e OS Perdidas não entram na MD."
    )

    indicadores = calcular_indicadores(base_semana)
    improdutivas = preparar_diagnostico_md(base_semana)
    motivos = resumo_motivos_md(improdutivas)

    if motivos.empty:
        principal_motivo = "Sem improdutivas"
        principal_qtd = 0
        principal_perc = 0.0
    else:
        principal_motivo = str(motivos.iloc[0]["Motivo"])
        principal_qtd = int(motivos.iloc[0]["Quantidade"])
        principal_perc = float(motivos.iloc[0]["% das improdutivas"])

    base_regiao = base_semana.copy()
    if "Consultor" not in base_regiao.columns:
        base_regiao["Consultor"] = "Não definido"
    base_regiao["Região"] = base_regiao["Consultor"].map(
        REGIOES_CONSULTORES
    ).fillna("Não definida")

    ranking_regiao = ranking_md_dimensao(base_regiao, "Região", minimo_base_md=1)
    ranking_oficina = ranking_md_dimensao(base_semana, "Oficina", minimo_base_md=3)
    if ranking_oficina.empty:
        ranking_oficina = ranking_md_dimensao(base_semana, "Oficina", minimo_base_md=1)

    st.markdown('<div id="md-diagnostico-cards"></div>', unsafe_allow_html=True)
    st.markdown(
        """
        <style>
        div[data-testid="stVerticalBlock"]:has(#md-diagnostico-cards)
        div[data-testid="stMetricValue"] {
            font-size: 1.55rem !important;
            line-height: 1.12 !important;
        }
        div[data-testid="stVerticalBlock"]:has(#md-diagnostico-cards)
        div[data-testid="stMetricLabel"] {
            font-size: 0.84rem !important;
        }
        div[data-testid="stVerticalBlock"]:has(#md-diagnostico-cards)
        div[data-testid="stMetricDelta"] {
            font-size: 0.76rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "MD geral",
        f'{indicadores["MD"]:.1f}%',
        help=(
            "Improdutivas totais ÷ (Executadas agendadas + "
            "Executadas extras + Improdutivas totais) × 100."
        ),
    )
    c2.metric(
        "Principal motivo",
        principal_motivo,
        delta=(f"{principal_perc:.1f}% das improdutivas" if principal_qtd else None),
        help=(
            "Participação do motivo dentro do total de improdutivas da semana. "
            "Não é a própria MD."
        ),
    )

    if ranking_regiao.empty:
        c3.metric("Região com maior MD", "Sem dados")
    else:
        reg = ranking_regiao.iloc[0]
        c3.metric(
            "Região com maior MD",
            str(reg["Região"]),
            delta=f'{reg["MD (%)"]:.1f}%',
            help=(
                "Região com maior taxa MD na semana, calculada sobre "
                "executadas + improdutivas da própria região."
            ),
        )

    if ranking_oficina.empty:
        c4.metric("Oficina com maior MD", "Sem dados")
    else:
        of = ranking_oficina.iloc[0]
        c4.metric(
            "Oficina com maior MD",
            str(of["Oficina"]),
            delta=f'{of["MD (%)"]:.1f}%',
            help=(
                "Oficina com maior taxa MD. Quando possível, o ranking "
                "considera somente oficinas com pelo menos 3 atendimentos "
                "na base MD, evitando distorção por um único caso."
            ),
        )

    st.markdown("#### Conexão do principal desvio")
    conexoes = []
    if not ranking_regiao.empty:
        reg = ranking_regiao.iloc[0]
        conexoes.append({
            "Nível": "Região crítica",
            "Nome": reg["Região"],
            "MD": f'{reg["MD (%)"]:.1f}%',
            "Principal motivo": reg["Principal motivo"],
            "% do motivo nas improdutivas": f'{reg["% motivo"]:.1f}%',
            "Mesmo motivo do Brasil?": "Sim" if reg["Principal motivo"] == principal_motivo else "Não",
        })
    if not ranking_oficina.empty:
        of = ranking_oficina.iloc[0]
        conexoes.append({
            "Nível": "Oficina crítica",
            "Nome": of["Oficina"],
            "MD": f'{of["MD (%)"]:.1f}%',
            "Principal motivo": of["Principal motivo"],
            "% do motivo nas improdutivas": f'{of["% motivo"]:.1f}%',
            "Mesmo motivo do Brasil?": "Sim" if of["Principal motivo"] == principal_motivo else "Não",
        })
    if conexoes:
        st.dataframe(pd.DataFrame(conexoes), use_container_width=True, hide_index=True)

    st.markdown("#### Destaques da semana")

    ranking_impacto = ranking_md_dimensao(
        base_semana,
        "Oficina",
        minimo_base_md=1,
    )
    if not ranking_impacto.empty:
        ranking_impacto = ranking_impacto.sort_values(
            ["Improdutivas", "MD (%)", "Base MD"],
            ascending=[False, False, False],
        ).reset_index(drop=True)

    ranking_positivo = ranking_md_dimensao(
        base_semana,
        "Oficina",
        minimo_base_md=5,
    )
    if ranking_positivo.empty:
        ranking_positivo = ranking_md_dimensao(
            base_semana,
            "Oficina",
            minimo_base_md=3,
        )

    if not ranking_positivo.empty:
        ranking_positivo = ranking_positivo.sort_values(
            ["MD (%)", "Base MD", "Improdutivas"],
            ascending=[True, False, True],
        ).reset_index(drop=True)

    d1, d2 = st.columns(2)

    with d1:
        st.markdown("**🔴 Maior impacto de improdutividade**")
        if ranking_impacto.empty:
            st.info("Sem dados suficientes.")
        else:
            pior = ranking_impacto.iloc[0]
            st.markdown(
                f"**{pior['Oficina']}**  \n"
                f"{int(pior['Improdutivas'])} improdutiva(s) · "
                f"MD {pior['MD (%)']:.1f}% · "
                f"Base MD {int(pior['Base MD'])}  \n"
                f"Principal motivo: {pior['Principal motivo']}"
            )

    with d2:
        st.markdown("**🟢 Melhor desempenho MD**")
        if ranking_positivo.empty:
            st.info("Sem dados suficientes.")
        else:
            melhor = ranking_positivo.iloc[0]
            st.markdown(
                f"**{melhor['Oficina']}**  \n"
                f"{int(melhor['Improdutivas'])} improdutiva(s) · "
                f"MD {melhor['MD (%)']:.1f}% · "
                f"Base MD {int(melhor['Base MD'])}  \n"
                f"Principal motivo: {melhor['Principal motivo']}"
            )

    if not ranking_positivo.empty:
        zeros = ranking_positivo[
            ranking_positivo["Improdutivas"] == 0
        ]
        if not zeros.empty:
            st.caption(
                f"🏆 {len(zeros)} oficina(s) com zero improdutividade "
                "e volume relevante na semana."
            )

    st.caption(
        "Rankings completos por motivo, região e oficina estão no "
        "Dashboard de Improdutividade."
    )

    st.caption(
        "Este bloco é diagnóstico. A definição de quando abrir ação/QPI "
        "e como registrar tratativas será validada com o time."
    )


def categorizar_motivo_ofs(valor: str) -> str:
    texto = normalizar_texto(valor)

    if not texto:
        return "Não informado"

    if any(
        termo in texto
        for termo in [
            "EQUIP",
            "FERRAMENT",
            "MATERIAL",
            "PECA",
            "INSUM",
            "CABO",
            "CHICOTE",
        ]
    ):
        return "Equipamento / material"

    if any(
        termo in texto
        for termo in [
            "VEICUL",
            "CARRO",
            "CAMINHAO",
            "FROTA",
            "INDISPONIVEL",
        ]
    ):
        return "Veículo indisponível"

    if any(
        termo in texto
        for termo in [
            "TECNIC",
            "EQUIPE",
            "CAPACIDADE",
            "MAO DE OBRA",
            "OFICINA",
        ]
    ):
        return "Capacidade / técnico"

    if any(
        termo in texto
        for termo in [
            "CLIENT",
            "SOLICITOU",
            "LIBERAC",
            "AUTORIZ",
        ]
    ):
        return "Cliente"

    if any(
        termo in texto
        for termo in [
            "DESLOC",
            "ACESS",
            "DISTANC",
            "ROTA",
        ]
    ):
        return "Deslocamento / acesso"

    if any(
        termo in texto
        for termo in [
            "DADO",
            "INFORMAC",
            "OS",
            "ORDEM",
            "CADASTR",
        ]
    ):
        return "Dados / OS"

    return "Outro"


def categorias_risco_follow(resposta: dict) -> set[str]:
    categorias = set()

    if resposta.get("equipamentos_ok") is False:
        categorias.add("Equipamento / material")

    veiculo = texto_limpo(
        resposta.get("veiculo_disponivel", "")
    )
    if veiculo == "Não":
        categorias.add("Veículo indisponível")

    if resposta.get("capacidade_ok") is False:
        categorias.add("Capacidade / técnico")

    motivos = resposta.get("motivos") or []

    for motivo in motivos:
        categoria = categorizar_motivo_ofs(
            str(motivo)
        )
        if categoria != "Não informado":
            categorias.add(categoria)

    return categorias


def status_resultado_follow(
    linhas_os: pd.DataFrame,
) -> tuple[str, str, str]:
    """
    Retorna:
    - status operacional resumido;
    - motivo OFS consolidado;
    - observação técnica consolidada.
    """
    if linhas_os.empty:
        return (
            "Sem desfecho localizado",
            "",
            "",
        )

    classes = set(
        linhas_os["Classificação"]
        .dropna()
        .astype(str)
        .tolist()
    )

    if any(
        classe in classes
        for classe in [
            "Improdutiva agendada",
            "Improdutiva extra",
        ]
    ):
        status = "Improdutiva"
    elif any(
        classe in classes
        for classe in [
            "Executada agendada",
            "Executada extra",
        ]
    ):
        status = "Executada"
    elif any(
        classe in classes
        for classe in [
            "Cancelada",
            "Cancelada extra",
            "Cancelada no agendamento",
        ]
    ):
        status = "Cancelada"
    elif "OS Perdida" in classes:
        status = "OS Perdida"
    elif "Possível substituição de OS" in classes:
        status = "Possível substituição de OS"
    else:
        status = "Outro"

    motivos = juntar_unicos(
        linhas_os.get(
            "Razao_improdutiva",
            pd.Series(dtype=str),
        )
    )
    observacoes = juntar_unicos(
        linhas_os.get(
            "Observacao_tecnico_improdutiva",
            pd.Series(dtype=str),
        )
    )

    return status, motivos, observacoes


def classificar_coerencia_follow(
    contato: dict,
    resposta: dict,
    status_resultado: str,
    motivo_ofs: str,
) -> tuple[str, str]:
    """
    Analisa a relação entre o que a oficina informou antes
    e o que efetivamente ocorreu depois no OFS.
    """
    respondeu = bool(contato.get("respondido_em"))

    if not respondeu:
        return (
            "Sem resposta prévia",
            "Não há resposta da oficina para comparar com o resultado.",
        )

    tem_impedimento = bool(
        contato.get("tem_impedimento")
    )

    categorias_previstas = categorias_risco_follow(
        resposta
    )
    categoria_resultado = categorizar_motivo_ofs(
        motivo_ofs
    )

    if status_resultado == "Executada":
        if tem_impedimento:
            return (
                "Risco tratado / execução realizada",
                (
                    "A oficina informou impedimento antes da execução, "
                    "mas a manutenção foi concluída."
                ),
            )

        return (
            "Confirmação coerente",
            (
                "A oficina informou que estava tudo OK e a manutenção "
                "foi executada."
            ),
        )

    if status_resultado == "Improdutiva":
        if not tem_impedimento:
            if categoria_resultado != "Não informado":
                return (
                    "Divergência do Follow",
                    (
                        "A oficina informou previamente que estava tudo OK, "
                        "mas a manutenção terminou improdutiva por motivo "
                        f"relacionado a {categoria_resultado}."
                    ),
                )

            return (
                "Improdutiva não antecipada",
                (
                    "A oficina informou que estava tudo OK, mas a manutenção "
                    "terminou improdutiva. O motivo OFS não permite identificar "
                    "uma categoria específica."
                ),
            )

        if (
            categoria_resultado in categorias_previstas
            and categoria_resultado != "Não informado"
        ):
            return (
                "Risco antecipado pelo Follow",
                (
                    "O impedimento informado antes da execução coincide "
                    "com a categoria do motivo de improdutividade no OFS."
                ),
            )

        return (
            "Risco informado, motivo diferente",
            (
                "A oficina informou impedimento antes da execução, porém "
                "o motivo final da improdutividade não coincide com o risco "
                "registrado no Follow."
            ),
        )

    if status_resultado == "OS Perdida":
        if tem_impedimento:
            return (
                "Risco informado / não executada",
                (
                    "A oficina informou impedimento e a manutenção não teve "
                    "desfecho de execução localizado."
                ),
            )
        return (
            "OS Perdida não antecipada",
            (
                "A oficina informou que estava tudo OK, mas a manutenção "
                "foi classificada como OS Perdida."
            ),
        )

    if status_resultado == "Cancelada":
        if tem_impedimento:
            return (
                "Risco informado / cancelada",
                (
                    "A oficina informou impedimento e a manutenção acabou "
                    "cancelada."
                ),
            )
        return (
            "Cancelamento não antecipado",
            (
                "A oficina informou que estava tudo OK, mas a manutenção "
                "acabou cancelada."
            ),
        )

    if status_resultado == "Possível substituição de OS":
        return (
            "Requer auditoria de OS",
            (
                "Há indício de substituição de OS; a coerência do Follow "
                "não deve ser concluída automaticamente."
            ),
        )

    return (
        "Sem conclusão",
        (
            "Não foi localizado desfecho suficiente para avaliar a "
            "coerência entre Follow e resultado."
        ),
    )


def montar_base_follow_resultado(
    contatos: pd.DataFrame,
    consolidado: pd.DataFrame,
) -> pd.DataFrame:
    linhas = []

    for _, contato in contatos.iterrows():
        follow_id = int(contato["id"])
        resposta = carregar_resposta_mais_recente_follow(
            follow_id
        )

        data_manutencao = texto_limpo(
            contato.get("data_manutencao")
        )
        os_follow = contato.get("os_agendadas") or []

        os_norm = {
            normalizar_texto(os_numero)
            for os_numero in os_follow
            if texto_limpo(os_numero)
        }

        base_data = consolidado[
            consolidado["Data Operacional"].astype(str)
            == str(data_manutencao)
        ].copy()

        if os_norm:
            mascara = pd.Series(
                False,
                index=base_data.index,
            )

            for coluna in [
                "OS_planejada",
                "OS_resultado",
            ]:
                if coluna in base_data.columns:
                    mascara = mascara | base_data[coluna].apply(
                        lambda valor: any(
                            normalizar_texto(parte)
                            in os_norm
                            for parte in str(valor).split("|")
                            if texto_limpo(parte)
                        )
                    )

            linhas_os = base_data[
                mascara
            ].copy()
        else:
            linhas_os = pd.DataFrame(
                columns=base_data.columns
            )

        status_resultado, motivo_ofs, obs_tecnico = (
            status_resultado_follow(
                linhas_os
            )
        )

        coerencia, explicacao = (
            classificar_coerencia_follow(
                contato.to_dict(),
                resposta,
                status_resultado,
                motivo_ofs,
            )
        )

        categorias_previstas = sorted(
            categorias_risco_follow(
                resposta
            )
        )

        linhas.append(
            {
                "Follow ID": follow_id,
                "Data": data_manutencao,
                "Consultor": texto_limpo(
                    contato.get("consultor")
                ),
                "Oficina": texto_limpo(
                    contato.get("oficina")
                ),
                "Qtd agendadas": int(
                    contato.get("qtd_agendadas") or 0
                ),
                "Respondido": bool(
                    contato.get("respondido_em")
                ),
                "Tem impedimento": bool(
                    contato.get("tem_impedimento")
                )
                if contato.get("respondido_em")
                else False,
                "Riscos informados": " | ".join(
                    categorias_previstas
                ),
                "Resultado OFS": status_resultado,
                "Razão da Improdutiva": motivo_ofs,
                "Categoria motivo OFS": categorizar_motivo_ofs(
                    motivo_ofs
                ),
                "Observação do Técnico": obs_tecnico,
                "Coerência Follow × Resultado": coerencia,
                "Leitura": explicacao,
                "Status ação": texto_limpo(
                    contato.get("status_acao")
                ) or "Não iniciado",
                "Responsável ação": texto_limpo(
                    contato.get("responsavel_acao")
                ),
                "Prazo ação": texto_limpo(
                    contato.get("prazo_acao")
                ),
            }
        )

    return pd.DataFrame(linhas)




# Se o link recebido no WhatsApp tiver um token, mostramos somente
# o formulário público e encerramos a execução do painel normal.
if exibir_formulario_publico_follow():
    st.stop()



# =========================================================
# PERFORMANCE E ESTADO DA INTERFACE
# =========================================================



def invalidar_cache_dados() -> None:
    st.cache_data.clear()


def selectbox_persistente(
    label: str,
    options,
    key: str,
    format_func=None,
):
    opcoes = list(options)

    if not opcoes:
        return None

    memoria_key = f"persist::{key}"
    salvo = st.session_state.get(memoria_key)

    if salvo not in opcoes:
        salvo = opcoes[0]

    # Evita conflito entre `index=` e um valor já existente no session_state
    # para a mesma key. O valor persistente é controlado apenas por memoria_key.
    widget_key = f"widget::{key}"

    if widget_key in st.session_state:
        atual = st.session_state.get(widget_key)
        if atual not in opcoes:
            st.session_state[widget_key] = salvo
    else:
        st.session_state[widget_key] = salvo

    kwargs = {
        "key": widget_key,
    }

    # O Streamlit espera uma função válida em format_func.
    # Quando não houver formatação personalizada, simplesmente
    # não enviamos esse argumento e deixamos o padrão do Streamlit.
    if format_func is not None:
        kwargs["format_func"] = format_func

    valor = st.selectbox(
        label,
        opcoes,
        **kwargs,
    )

    st.session_state[memoria_key] = valor
    return valor


def multiselect_persistente(
    label: str,
    options,
    key: str,
    default=None,
):
    opcoes = list(options)
    memoria_key = f"persist::{key}"
    salvo = st.session_state.get(memoria_key)

    if salvo is None:
        salvo = list(default or [])

    salvo = [
        valor
        for valor in salvo
        if valor in opcoes
    ]

    valor = st.multiselect(
        label,
        opcoes,
        default=salvo,
        key=key,
    )

    st.session_state[memoria_key] = valor
    return valor


def date_input_persistente(
    label: str,
    value,
    key: str,
    min_value=None,
    max_value=None,
):
    memoria_key = f"persist::{key}"
    salvo = st.session_state.get(
        memoria_key,
        value,
    )

    valor = st.date_input(
        label,
        value=salvo,
        min_value=min_value,
        max_value=max_value,
        key=key,
    )

    st.session_state[memoria_key] = valor
    return valor


# =========================================================
# CABEÇALHO E MENU
# =========================================================

st.title("🚛 Operações de Campo PS")
st.caption("Sistema de Gestão Operacional de Campo")

if SUPABASE is None:
    st.warning(ERRO_SUPABASE or "Supabase não conectado.")
else:
    st.success("🟢 Supabase conectado e persistência ativa.")

st.divider()

with st.sidebar:
    st.header("Navegação")

    area = st.radio(
        "Área",
        [
            "📊 Dashboard",
            "📞 Follow",
            "📥 Dados",
            "📦 Estoque",
            "⚙️ Configurações",
        ],
        key="nav_area_principal",
    )

    if area == "📊 Dashboard":
        subarea = st.radio(
            "Visão",
            [
                "Executivo",
                "Consultor",
                "Improdutividade",
                "Pré-agenda",
            ],
            key="nav_dashboard",
        )

        pagina = {
            "Executivo": "📊 Dashboard Executivo",
            "Consultor": "👤 Painel do Consultor",
            "Improdutividade": "📉 Dashboard de Improdutividade",
            "Pré-agenda": "🛰️ Controle Pré-Agenda",
        }[subarea]

    elif area == "📞 Follow":
        subarea = st.radio(
            "Etapa",
            [
                "Preventivo",
                "Ações",
                "Follow × OFS",
            ],
            key="nav_follow",
        )

        pagina = {
            "Preventivo": "📞 Follow",
            "Ações": "🧭 Painel de Ações do Follow",
            "Follow × OFS": "🔄 Follow × Resultado OFS",
        }[subarea]

    elif area == "📦 Estoque":
        pagina = "📦 Estoque & Equipamentos"

    elif area == "📥 Dados":
        subarea = st.radio(
            "Dados",
            [
                "Importações",
                "Bases salvas",
            ],
            key="nav_dados",
        )

        pagina = {
            "Importações": "📥 Importações",
            "Bases salvas": "🗂 Bases Salvas",
        }[subarea]

    else:
        pagina = "🏢 Cadastro de Oficinas"

    st.divider()
    st.caption(
        "Versão 2.7.1 — Sanitização + Estoque OFS + Curva ABC"
    )


# =========================================================
# IMPORTAÇÕES
# =========================================================

if pagina == "📥 Importações":
    exigir_supabase()

    st.subheader("Importações permanentes")
    st.info(
        "Escolha a data operacional. Se já existir uma base do mesmo "
        "tipo e data, ela será substituída."
    )
    st.caption(
        "🔐 Minimização ativa: CPF, RG, CNH, telefone, celular, WhatsApp e e-mail "
        "são descartados do conteúdo bruto persistido nas importações operacionais. "
        "Campos necessários às regras do painel, como Recurso, continuam preservados."
    )

    aba_oficinas, aba_planejado, aba_resultado, aba_pre_agenda, aba_estoque_ofs = st.tabs(
        [
            "🏢 Oficinas",
            "📅 Planejado",
            "📈 Resultado",
            "🛰️ Pré-agenda / Aceite",
            "📦 Equipamentos Trocados — OFS",
        ]
    )

    with aba_oficinas:
        arquivo = st.file_uploader(
            "Cadastro oficial das oficinas",
            type=["ods", "xlsx", "csv"],
            key="cadastro_upload",
        )

        if arquivo is not None:
            try:
                cadastro = preparar_cadastro(
                    ler_cadastro_oficinas(arquivo)
                )

                st.write(f"Oficinas identificadas: **{len(cadastro)}**")
                st.dataframe(
                    cadastro.head(20),
                    use_container_width=True,
                    hide_index=True,
                )

                if st.button(
                    "💾 Salvar cadastro no Supabase",
                    type="primary",
                ):
                    salvar_oficinas(cadastro)
                    st.success(
                        f"{len(cadastro)} oficinas salvas/atualizadas."
                    )

            except Exception as erro:
                st.error(f"Erro no cadastro: {erro}")

    with aba_planejado:
        st.markdown("### Janela móvel de agendamentos")
        st.caption(
            "Baixe no OFS a janela de 3 dias (ou outra janela desejada). "
            "O sistema separa automaticamente as manutenções pela coluna Data "
            "e preserva a primeira aparição de cada OS para cada data."
        )

        arquivo = st.file_uploader(
            "Arquivo CSV do agendamento",
            type=["csv"],
            key="planejado_upload",
        )

        if arquivo is not None:
            try:
                df = ler_csv_ofs(arquivo)
                grupos_preview = separar_planejamento_por_data(df)

                st.write(
                    f"Manutenções identificadas no arquivo: "
                    f"**{sum(len(g) for g in grupos_preview.values())}**"
                )

                resumo_preview = pd.DataFrame(
                    [
                        {
                            "Data": pd.to_datetime(data).strftime(
                                "%d/%m/%Y"
                            ),
                            "Manutenções": len(grupo),
                        }
                        for data, grupo in sorted(
                            grupos_preview.items()
                        )
                    ]
                )

                st.dataframe(
                    resumo_preview,
                    use_container_width=True,
                    hide_index=True,
                )

                if st.button(
                    "💾 Salvar janela de agendamentos no Supabase",
                    type="primary",
                ):
                    resumo_salvo = salvar_planejamento_janela(
                        arquivo.name,
                        df,
                    )

                    st.success(
                        "Janela salva. Primeira aparição das OS preservada."
                    )

                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Data": pd.to_datetime(data).strftime(
                                        "%d/%m/%Y"
                                    ),
                                    "Manutenções salvas": quantidade,
                                }
                                for data, quantidade in sorted(
                                    resumo_salvo.items()
                                )
                            ]
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

            except Exception as erro:
                st.error(f"Erro no agendamento: {erro}")

    with aba_resultado:
        data_resultado = st.date_input(
            "Data operacional do resultado",
            value=date.today(),
            key="data_resultado",
        )
        arquivo = st.file_uploader(
            "Arquivo CSV do resultado",
            type=["csv"],
            key="resultado_upload",
        )

        if arquivo is not None:
            try:
                df = ler_csv_ofs(arquivo)
                st.write(f"Registros lidos: **{len(df)}**")

                if st.button(
                    "💾 Salvar resultado no Supabase",
                    type="primary",
                ):
                    salvar_base(
                        "resultado",
                        data_resultado,
                        arquivo.name,
                        df,
                    )
                    st.success("Resultado salvo permanentemente.")

            except Exception as erro:
                st.error(f"Erro no resultado: {erro}")


    with aba_pre_agenda:
        st.markdown("### Controle Pré-Agenda — Aceite e Alocação")
        st.caption(
            "Importe o relatório do OFS usado para acompanhar aceite/alocação de todas as atividades. "
            "Esta base é independente do Planejado e não altera MCI, MD ou "
            "planejamento_base."
        )

        arquivo_pre = st.file_uploader(
            "CSV de pré-agenda / pendentes de aceite",
            type=["csv"],
            key="pre_agenda_upload",
        )

        if arquivo_pre is not None:
            try:
                df_pre = ler_csv_ofs(arquivo_pre)
                preview_pre = df_pre.copy()

                for coluna in [
                    "Motivos de Rejeição da OS",
                    "Flag OS Rejeitada",
                    "Aceite OS",
                ]:
                    if coluna not in preview_pre.columns:
                        preview_pre[coluna] = ""

                preview_pre["Situação"] = preview_pre.apply(
                    classificar_pre_agenda,
                    axis=1,
                )

                resumo_pre = (
                    preview_pre["Situação"]
                    .value_counts()
                    .rename_axis("Situação")
                    .reset_index(name="Quantidade")
                )

                st.write(
                    f"Atividades identificadas: **{len(preview_pre)}**"
                )
                st.dataframe(
                    resumo_pre,
                    use_container_width=True,
                    hide_index=True,
                )

                colunas_preview = [
                    coluna
                    for coluna in [
                        "Data",
                        "OS",
                        "Oficina",
                        "Recurso",
                        "Tipo de Atividade",
                        "Status da Atividade",
                        "Motivos de Rejeição da OS",
                        "Situação",
                    ]
                    if coluna in preview_pre.columns
                ]

                st.dataframe(
                    preview_pre[colunas_preview].head(100),
                    use_container_width=True,
                    hide_index=True,
                )

                if st.button(
                    "💾 Salvar fotografia de pré-agenda",
                    type="primary",
                    key="salvar_pre_agenda",
                ):
                    _, quantidade = salvar_pre_agenda(
                        arquivo_pre.name,
                        df_pre,
                    )
                    st.success(
                        f"Fotografia salva: {quantidade} atividade(s)."
                    )
                    st.rerun()

            except Exception as erro:
                st.error(f"Erro na pré-agenda: {erro}")


    with aba_estoque_ofs:
        st.markdown("### Equipamentos Trocados — OFS")
        st.caption(
            "Fonte de consumo realizado. O importador grava oficina, OS, item, quantidade, "
            "data e motivo. Nome de técnico, CPF e celular não são armazenados no módulo de estoque."
        )
        arquivo_estoque = st.file_uploader(
            "CSV de Equipamentos Trocados do OFS", type=["csv"], key="estoque_ofs_upload"
        )
        if arquivo_estoque is not None:
            try:
                df_est = ler_csv_equipamentos_ofs(arquivo_estoque)
                preview = preparar_consumos_ofs(df_est)
                qtd_total = sum(float(r["quantidade"]) for r in preview)
                st.write(f"Linhas lidas: **{len(df_est)}** · Quantidade total: **{qtd_total:,.0f}** · Itens: **{df_est['Cód. Equip.'].nunique()}**")
                st.dataframe(
                    df_est[["OS", "Execução", "Oficina", "Cód. Equip.", "Nome Equip.", "Qde", "Motivo Envio"]].head(100),
                    use_container_width=True, hide_index=True,
                )
                if st.button("💾 Importar consumo OFS no estoque", type="primary", key="salvar_estoque_ofs"):
                    resumo = salvar_consumos_ofs(arquivo_estoque, df_est)
                    if resumo["duplicado"]:
                        st.warning("Este arquivo já foi importado anteriormente. Nenhum dado foi duplicado.")
                    else:
                        st.success(
                            f"Importação concluída: {resumo['importados']} registro(s) novo(s), "
                            f"{resumo['ignorados']} ignorado(s) e {resumo['catalogo']} item(ns) no catálogo."
                        )
                    st.rerun()
            except Exception as erro:
                st.error(f"Erro no consumo OFS: {erro}")


# =========================================================
# BASES SALVAS
# =========================================================

elif pagina == "🗂 Bases Salvas":
    exigir_supabase()

    st.subheader("Bases armazenadas no Supabase")
    bases = listar_bases()

    if bases.empty:
        st.info("Ainda não há bases salvas.")
        st.stop()

    bases_exibir = bases.copy()
    bases_exibir["data_operacional"] = pd.to_datetime(
        bases_exibir["data_operacional"]
    ).dt.strftime("%d/%m/%Y")

    colunas = [
        "tipo",
        "data_operacional",
        "nome_arquivo",
        "quantidade_registros",
        "criado_em",
        "atualizado_em",
    ]
    colunas = [c for c in colunas if c in bases_exibir.columns]

    st.dataframe(
        bases_exibir[colunas],
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("### Excluir uma base")
    opcoes = [
        f"{linha['tipo']} | {linha['data_operacional']} | "
        f"{linha.get('nome_arquivo', '')}"
        for _, linha in bases.iterrows()
    ]

    selecionada = st.selectbox(
        "Base",
        opcoes,
    )
    indice = opcoes.index(selecionada)
    linha = bases.iloc[indice]

    confirmar = st.checkbox(
        "Confirmo que desejo excluir esta base."
    )

    if st.button(
        "🗑 Excluir base",
        disabled=not confirmar,
        type="primary",
    ):
        excluir_base(
            str(linha["tipo"]),
            str(linha["data_operacional"]),
        )
        st.success("Base excluída.")
        st.rerun()


# =========================================================
# PAINEL
# =========================================================

elif pagina == "📊 Dashboard Executivo":
    exigir_supabase()

    bases = listar_bases()

    if bases.empty:
        st.warning("Importe ao menos um planejado e um resultado.")
        st.stop()

    datas_completas = listar_datas_completas_reais()

    if not datas_completas:
        st.warning(
            "Não existe uma data com planejado e resultado "
            "salvos juntos."
        )
        st.stop()

    cadastro = carregar_oficinas()
    consolidado = carregar_consolidado(datas_completas)

    if consolidado.empty:
        st.warning("Não foi possível montar o consolidado geral.")
        st.stop()

    if not cadastro.empty:
        consolidado_enriquecido = enriquecer_com_cadastro(
            consolidado,
            cadastro,
        )
    else:
        consolidado_enriquecido = consolidado.copy()

    # =====================================================
    # CONSOLIDADO GERAL — SEMPRE FIXO
    # =====================================================

    st.subheader("Visão consolidada geral de manutenções")
    st.caption(
        f"Acumulado de {len(datas_completas)} data(s) operacional(is), "
        f"de {pd.to_datetime(min(datas_completas)).strftime('%d/%m/%Y')} "
        f"até {pd.to_datetime(max(datas_completas)).strftime('%d/%m/%Y')}."
    )

    indicadores_consolidados = calcular_indicadores(
        consolidado_enriquecido
    )
    exibir_cards_indicadores(
        indicadores_consolidados,
        prefixo="consolidado_geral",
    )
    exibir_detalhamento(
        consolidado_enriquecido,
        escopo="consolidado_geral",
        contexto="Consolidado geral de manutenções de todas as datas completas",
    )

    # =====================================================
    # VISÃO SEMANAL — MCI E MD
    # =====================================================

    st.divider()
    st.subheader("Visão semanal de MCI e MD")
    st.caption(
        "Semanas operacionais de segunda a domingo. "
        "A semana atual considera apenas as datas completas já importadas."
    )

    semanal_base = consolidado_enriquecido.copy()
    semanal_base["__Data_dt"] = pd.to_datetime(
        semanal_base["Data Operacional"],
        errors="coerce",
    )

    semanal_base = semanal_base[
        semanal_base["__Data_dt"].notna()
    ].copy()

    if not semanal_base.empty:
        semanal_base["__Inicio_semana"] = (
            semanal_base["__Data_dt"]
            - pd.to_timedelta(
                semanal_base["__Data_dt"].dt.weekday,
                unit="D",
            )
        ).dt.normalize()

        blocos_semanais = []

        for inicio_semana, grupo_semana in (
            semanal_base
            .groupby("__Inicio_semana")
        ):
            fim_semana = (
                inicio_semana
                + pd.Timedelta(days=6)
            )

            indicadores_semana = calcular_indicadores(
                grupo_semana
            )

            blocos_semanais.append(
                {
                    "Semana": (
                        f"{inicio_semana.strftime('%d/%m')} "
                        f"a {fim_semana.strftime('%d/%m')}"
                    ),
                    "Início": inicio_semana,
                    "Fim": fim_semana,
                    "Agendadas": indicadores_semana["Planejadas"],
                    "Executadas agendadas": (
                        indicadores_semana["Executadas planejadas"]
                    ),
                    "Executadas extras": (
                        indicadores_semana["Executadas extras"]
                    ),
                    "Improdutivas": indicadores_semana["Improdutivas"],
                    "OS Perdidas": indicadores_semana["OS Perdidas"],
                    "Canceladas": indicadores_semana["Canceladas"],
                    "MCI": indicadores_semana["MCI"],
                    "MD": indicadores_semana["MD"],
                    "Datas completas": int(
                        grupo_semana[
                            "Data Operacional"
                        ].astype(str).nunique()
                    ),
                }
            )

        resumo_semanal = pd.DataFrame(
            blocos_semanais
        ).sort_values(
            "Início",
            ascending=False,
        )

        resumo_exibicao = resumo_semanal[
            [
                "Semana",
                "Agendadas",
                "Executadas agendadas",
                "Executadas extras",
                "Improdutivas",
                "OS Perdidas",
                "Canceladas",
                "MCI",
                "MD",
                "Datas completas",
            ]
        ].copy()

        resumo_exibicao["MCI"] = (
            resumo_exibicao["MCI"]
            .apply(lambda valor: f"{valor:.1f}%")
        )
        resumo_exibicao["MD"] = (
            resumo_exibicao["MD"]
            .apply(lambda valor: f"{valor:.1f}%")
        )

        st.dataframe(
            resumo_exibicao,
            use_container_width=True,
            hide_index=True,
        )

        semanas_opcoes = resumo_semanal[
            "Semana"
        ].tolist()

        semana_selecionada = st.selectbox(
            "Detalhar semana",
            semanas_opcoes,
            key="dashboard_semana_mci_md",
        )

        linha_semana = resumo_semanal[
            resumo_semanal["Semana"]
            == semana_selecionada
        ].iloc[0]

        inicio_sel = linha_semana["Início"]
        fim_sel = linha_semana["Fim"]

        base_semana_sel = semanal_base[
            (
                semanal_base["__Data_dt"]
                >= inicio_sel
            )
            & (
                semanal_base["__Data_dt"]
                <= fim_sel
            )
        ].copy()

        indicadores_sel = calcular_indicadores(
            base_semana_sel
        )

        s1, s2, s3, s4 = st.columns(4)
        s1.metric(
            "MCI da semana",
            f"{indicadores_sel['MCI']:.1f}%",
        )
        s2.metric(
            "MD da semana",
            f"{indicadores_sel['MD']:.1f}%",
        )
        s3.metric(
            "Agendadas",
            indicadores_sel["Planejadas"],
        )
        s4.metric(
            "Executadas agendadas",
            indicadores_sel["Executadas planejadas"],
        )

        s5, s6, s7, s8 = st.columns(4)
        s5.metric(
            "Improdutivas",
            indicadores_sel["Improdutivas"],
        )
        s6.metric(
            "OS Perdidas",
            indicadores_sel["OS Perdidas"],
        )
        s7.metric(
            "Canceladas",
            indicadores_sel["Canceladas"],
        )
        s8.metric(
            "Execuções extras",
            indicadores_sel["Executadas extras"],
        )

        exibir_detalhamento(
            base_semana_sel,
            escopo=(
                "semana_"
                + inicio_sel.strftime("%Y_%m_%d")
            ),
            contexto=(
                "Semana "
                f"{inicio_sel.strftime('%d/%m/%Y')} "
                "a "
                f"{fim_sel.strftime('%d/%m/%Y')}"
            ),
        )

        st.divider()
        exibir_diagnostico_md_semanal(
            base_semana_sel,
            inicio_sel,
            fim_sel,
        )
    else:
        st.info(
            "Não há dados suficientes para montar a visão semanal."
        )

    # =====================================================
    # VISÃO DA DATA SELECIONADA
    # =====================================================

    st.divider()
    st.subheader("Visão por dia")

    data_selecionada = selectbox_persistente(
        "Data analisada",
        datas_completas,
        key="dashboard_executivo_data",
        format_func=lambda valor: pd.to_datetime(valor).strftime(
            "%d/%m/%Y"
        ),
    )

    conciliacao_dia = consolidado_enriquecido[
        consolidado_enriquecido["Data Operacional"].astype(str)
        == str(data_selecionada)
    ].copy()

    indicadores_dia = calcular_indicadores(conciliacao_dia)
    escopo_dia = f"dia_{data_selecionada}"

    exibir_cards_indicadores(
        indicadores_dia,
        prefixo=escopo_dia,
    )
    exibir_detalhamento(
        conciliacao_dia,
        escopo=escopo_dia,
        contexto=(
            "Data "
            f"{pd.to_datetime(data_selecionada).strftime('%d/%m/%Y')}"
        ),
    )

    st.divider()
    esquerda, direita = st.columns(2)

    with esquerda:
        st.subheader("Classificação da data selecionada")

        resumo = (
            conciliacao_dia["Classificação"]
            .value_counts()
            .reset_index()
        )
        resumo.columns = ["Classificação", "Quantidade"]

        grafico = px.bar(
            resumo,
            x="Quantidade",
            y="Classificação",
            orientation="h",
            text="Quantidade",
        )
        grafico.update_layout(
            showlegend=False,
            height=480,
        )
        st.plotly_chart(
            grafico,
            use_container_width=True,
        )

    with direita:
        st.subheader("Resumo da data selecionada")

        st.metric(
            "Atendimentos conciliados",
            len(conciliacao_dia),
        )
        st.metric(
            "Tickets",
            contar_unicos(conciliacao_dia, "Ticket"),
        )
        st.metric(
            "Oficinas",
            contar_unicos(conciliacao_dia, "Oficina"),
        )
        st.metric(
            "Consultores com atendimento",
            contar_unicos(conciliacao_dia, "Consultor"),
        )


# =========================================================
# PAINEL DO CONSULTOR
# =========================================================

elif pagina == "👤 Painel do Consultor":
    exigir_supabase()

    bases = listar_bases()

    if bases.empty:
        st.warning("Importe ao menos um planejado e um resultado.")
        st.stop()

    datas_completas = listar_datas_completas_reais()

    if not datas_completas:
        st.warning(
            "Não existe uma data com planejado e resultado "
            "salvos juntos."
        )
        st.stop()

    cadastro = carregar_oficinas()

    if cadastro.empty:
        st.warning(
            "Cadastre as oficinas para habilitar o painel do consultor."
        )
        st.stop()

    # Monta o consolidado de todas as datas completas.
    consolidado = carregar_consolidado(datas_completas)

    if consolidado.empty:
        st.warning(
            "Não foi possível montar o consolidado dos consultores."
        )
        st.stop()

    consolidado_enriquecido = enriquecer_com_cadastro(
        consolidado,
        cadastro,
    )

    consultores_disponiveis = sorted(
        {
            texto_limpo(valor)
            for valor in consolidado_enriquecido["Consultor"]
            if texto_limpo(valor)
        }
    )

    if not consultores_disponiveis:
        st.warning(
            "Nenhum consultor foi identificado nas oficinas "
            "relacionadas às manutenções."
        )
        st.stop()

    st.subheader("Painel do Consultor")

    consultor_selecionado = selectbox_persistente(
        "Consultor",
        consultores_disponiveis,
        key="consultor_painel_dedicado",
    )

    regiao = REGIOES_CONSULTORES.get(
        consultor_selecionado,
        "Não definida",
    )

    # =====================================================
    # CONSOLIDADO DO CONSULTOR — SEMPRE FIXO
    # =====================================================

    base_consolidada_consultor = consolidado_enriquecido[
        consolidado_enriquecido["Consultor"]
        == consultor_selecionado
    ].copy()

    st.info(
        f"Consultor: **{consultor_selecionado}** · "
        f"Região: **{regiao}**"
    )

    st.subheader("Consolidado do consultor")
    st.caption(
        f"Acumulado de {len(datas_completas)} data(s) operacional(is), "
        f"de {pd.to_datetime(min(datas_completas)).strftime('%d/%m/%Y')} "
        f"até {pd.to_datetime(max(datas_completas)).strftime('%d/%m/%Y')}."
    )

    indicadores_consolidados_consultor = calcular_indicadores(
        base_consolidada_consultor
    )

    escopo_consolidado_consultor = (
        "consolidado_consultor_"
        f"{normalizar_texto(consultor_selecionado)}"
    )

    exibir_cards_indicadores(
        indicadores_consolidados_consultor,
        prefixo=escopo_consolidado_consultor,
    )

    exibir_detalhamento(
        base_consolidada_consultor,
        escopo=escopo_consolidado_consultor,
        contexto=(
            f"Consolidado de {consultor_selecionado} — {regiao}"
        ),
    )

    # =====================================================
    # VISÃO SEMANAL DO CONSULTOR — MCI E MD
    # =====================================================

    st.divider()
    st.subheader("Visão semanal do consultor — MCI e MD")
    st.caption(
        "Semanas de segunda a domingo. "
        "A semana atual considera apenas as datas completas já importadas."
    )

    semanal_consultor = base_consolidada_consultor.copy()
    semanal_consultor["__Data_dt"] = pd.to_datetime(
        semanal_consultor["Data Operacional"],
        errors="coerce",
    )

    semanal_consultor = semanal_consultor[
        semanal_consultor["__Data_dt"].notna()
    ].copy()

    if not semanal_consultor.empty:
        semanal_consultor["__Inicio_semana"] = (
            semanal_consultor["__Data_dt"]
            - pd.to_timedelta(
                semanal_consultor["__Data_dt"].dt.weekday,
                unit="D",
            )
        ).dt.normalize()

        blocos_semanais_consultor = []

        for inicio_semana, grupo_semana in (
            semanal_consultor.groupby("__Inicio_semana")
        ):
            fim_semana = (
                inicio_semana
                + pd.Timedelta(days=6)
            )

            indicadores_semana = calcular_indicadores(
                grupo_semana
            )

            blocos_semanais_consultor.append(
                {
                    "Semana": (
                        f"{inicio_semana.strftime('%d/%m')} "
                        f"a {fim_semana.strftime('%d/%m')}"
                    ),
                    "Início": inicio_semana,
                    "Fim": fim_semana,
                    "Agendadas": indicadores_semana["Planejadas"],
                    "Executadas agendadas": (
                        indicadores_semana["Executadas planejadas"]
                    ),
                    "Executadas extras": (
                        indicadores_semana["Executadas extras"]
                    ),
                    "Improdutivas": indicadores_semana["Improdutivas"],
                    "OS Perdidas": indicadores_semana["OS Perdidas"],
                    "Canceladas": indicadores_semana["Canceladas"],
                    "MCI": indicadores_semana["MCI"],
                    "MD": indicadores_semana["MD"],
                    "Datas completas": int(
                        grupo_semana[
                            "Data Operacional"
                        ].astype(str).nunique()
                    ),
                }
            )

        resumo_semanal_consultor = pd.DataFrame(
            blocos_semanais_consultor
        ).sort_values(
            "Início",
            ascending=False,
        )

        resumo_exibicao_consultor = (
            resumo_semanal_consultor[
                [
                    "Semana",
                    "Agendadas",
                    "Executadas agendadas",
                    "Executadas extras",
                    "Improdutivas",
                    "OS Perdidas",
                    "Canceladas",
                    "MCI",
                    "MD",
                    "Datas completas",
                ]
            ].copy()
        )

        resumo_exibicao_consultor["MCI"] = (
            resumo_exibicao_consultor["MCI"]
            .apply(lambda valor: f"{valor:.1f}%")
        )
        resumo_exibicao_consultor["MD"] = (
            resumo_exibicao_consultor["MD"]
            .apply(lambda valor: f"{valor:.1f}%")
        )

        st.dataframe(
            resumo_exibicao_consultor,
            use_container_width=True,
            hide_index=True,
        )

        semanas_consultor = resumo_semanal_consultor[
            "Semana"
        ].tolist()

        semana_consultor_sel = st.selectbox(
            "Detalhar semana do consultor",
            semanas_consultor,
            key=(
                "semana_consultor_mci_md_"
                + normalizar_texto(
                    consultor_selecionado
                )
            ),
        )

        linha_semana_consultor = (
            resumo_semanal_consultor[
                resumo_semanal_consultor["Semana"]
                == semana_consultor_sel
            ].iloc[0]
        )

        inicio_consultor_sel = (
            linha_semana_consultor["Início"]
        )
        fim_consultor_sel = (
            linha_semana_consultor["Fim"]
        )

        base_semana_consultor = semanal_consultor[
            (
                semanal_consultor["__Data_dt"]
                >= inicio_consultor_sel
            )
            & (
                semanal_consultor["__Data_dt"]
                <= fim_consultor_sel
            )
        ].copy()

        indicadores_semana_consultor = (
            calcular_indicadores(
                base_semana_consultor
            )
        )

        sw1, sw2, sw3, sw4 = st.columns(4)
        sw1.metric(
            "MCI da semana",
            f"{indicadores_semana_consultor['MCI']:.1f}%",
        )
        sw2.metric(
            "MD da semana",
            f"{indicadores_semana_consultor['MD']:.1f}%",
        )
        sw3.metric(
            "Agendadas",
            indicadores_semana_consultor["Planejadas"],
        )
        sw4.metric(
            "Executadas agendadas",
            indicadores_semana_consultor[
                "Executadas planejadas"
            ],
        )

        sw5, sw6, sw7, sw8 = st.columns(4)
        sw5.metric(
            "Improdutivas",
            indicadores_semana_consultor["Improdutivas"],
        )
        sw6.metric(
            "OS Perdidas",
            indicadores_semana_consultor["OS Perdidas"],
        )
        sw7.metric(
            "Canceladas",
            indicadores_semana_consultor["Canceladas"],
        )
        sw8.metric(
            "Execuções extras",
            indicadores_semana_consultor["Executadas extras"],
        )

        exibir_detalhamento(
            base_semana_consultor,
            escopo=(
                "semana_consultor_"
                + normalizar_texto(
                    consultor_selecionado
                )
                + "_"
                + inicio_consultor_sel.strftime(
                    "%Y_%m_%d"
                )
            ),
            contexto=(
                f"{consultor_selecionado} — {regiao} — "
                "Semana "
                f"{inicio_consultor_sel.strftime('%d/%m/%Y')} "
                "a "
                f"{fim_consultor_sel.strftime('%d/%m/%Y')}"
            ),
        )
    else:
        st.info(
            "Não há dados suficientes para montar "
            "a visão semanal deste consultor."
        )

    # =====================================================
    # VISÃO DIÁRIA DO CONSULTOR
    # =====================================================

    st.divider()
    st.subheader("Visão diária do consultor")

    data_selecionada = selectbox_persistente(
        "Data analisada",
        datas_completas,
        key="data_painel_consultor",
        format_func=lambda valor: pd.to_datetime(valor).strftime(
            "%d/%m/%Y"
        ),
    )

    base_dia_consultor = base_consolidada_consultor[
        base_consolidada_consultor["Data Operacional"].astype(str)
        == str(data_selecionada)
    ].copy()

    st.caption(
        f"{consultor_selecionado} · {regiao} · "
        f"{pd.to_datetime(data_selecionada).strftime('%d/%m/%Y')} · "
        f"{len(base_dia_consultor)} atendimento(s)"
    )

    indicadores_dia_consultor = calcular_indicadores(
        base_dia_consultor
    )

    escopo_dia_consultor = (
        f"dia_consultor_{data_selecionada}_"
        f"{normalizar_texto(consultor_selecionado)}"
    )

    exibir_cards_indicadores(
        indicadores_dia_consultor,
        prefixo=escopo_dia_consultor,
    )

    exibir_detalhamento(
        base_dia_consultor,
        escopo=escopo_dia_consultor,
        contexto=(
            f"{consultor_selecionado} — {regiao} — "
            f"{pd.to_datetime(data_selecionada).strftime('%d/%m/%Y')}"
        ),
    )

    st.divider()
    esquerda, direita = st.columns(2)

    with esquerda:
        st.subheader("Classificação do dia")

        resumo = (
            base_dia_consultor["Classificação"]
            .value_counts()
            .reset_index()
        )
        resumo.columns = ["Classificação", "Quantidade"]

        grafico = px.bar(
            resumo,
            x="Quantidade",
            y="Classificação",
            orientation="h",
            text="Quantidade",
        )
        grafico.update_layout(
            showlegend=False,
            height=480,
        )
        st.plotly_chart(
            grafico,
            use_container_width=True,
        )

    with direita:
        st.subheader("Resumo operacional do dia")

        st.metric(
            "Tickets",
            contar_unicos(base_dia_consultor, "Ticket"),
        )
        st.metric(
            "Oficinas",
            contar_unicos(base_dia_consultor, "Oficina"),
        )
        st.metric(
            "Placas",
            contar_unicos(base_dia_consultor, "Placa"),
        )

    # =====================================================
    # RANKING CONSOLIDADO DAS OFICINAS DO CONSULTOR
    # =====================================================

    st.divider()
    st.subheader("Ranking consolidado das oficinas")

    linhas_ranking = []

    for oficina, grupo in base_consolidada_consultor.groupby(
        "Oficina",
        dropna=False,
    ):
        ind = calcular_indicadores(grupo)

        linhas_ranking.append(
            {
                "Oficina": texto_limpo(oficina) or "Não definida",
                "Planejadas": ind["Planejadas"],
                "Planejadas elegíveis MCI": ind["Planejadas elegíveis MCI"],
                "Executadas": ind["Executadas planejadas"],
                "Extras": ind["Executadas extras"],
                "Improdutivas": ind["Improdutivas"],
                "Improdutivas consideradas": ind["Improdutivas consideradas MD"],
                "Expurgadas": ind["Improdutivas expurgadas"],
                "OS_Perdidas": ind["OS Perdidas"],
                "Canceladas": ind["Canceladas"],
                "MCI (%)": ind["MCI"],
                "MD (%)": ind["MD"],
            }
        )

    ranking = pd.DataFrame(linhas_ranking)

    if not ranking.empty:
        ranking = ranking.sort_values(
            ["Planejadas", "Oficina"],
            ascending=[False, True],
        )

        ranking.insert(
            0,
            "Posição",
            range(1, len(ranking) + 1),
        )

    st.dataframe(
        ranking,
        use_container_width=True,
        hide_index=True,
    )


# =========================================================
# CADASTRO DE OFICINAS
# =========================================================

elif pagina == "🏢 Cadastro de Oficinas":
    exigir_supabase()

    cadastro = carregar_oficinas()

    if cadastro.empty:
        st.warning("Importe o cadastro das oficinas.")
        st.stop()

    st.subheader("Cadastro mestre de oficinas")

    pesquisa = st.text_input("Pesquisar oficina")
    cadastro_filtrado = cadastro.copy()

    if pesquisa:
        cadastro_filtrado = cadastro_filtrado[
            cadastro_filtrado["Oficina"].str.contains(
                pesquisa,
                case=False,
                na=False,
            )
        ]

    cadastro_editado = st.data_editor(
        cadastro_filtrado,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "Consultor": st.column_config.SelectboxColumn(
                "Consultor",
                options=CONSULTORES,
            ),
            "Prioridade": st.column_config.SelectboxColumn(
                "Prioridade",
                options=["Alta", "Normal", "Baixa"],
            ),
            "Ativa": st.column_config.SelectboxColumn(
                "Ativa",
                options=["Sim", "Não"],
            ),
        },
    )

    if st.button(
        "💾 Salvar alterações no Supabase",
        type="primary",
    ):
        cadastro_editado = cadastro_editado.copy()
        cadastro_editado["Chave Oficina"] = cadastro_editado[
            "Oficina"
        ].apply(normalizar_texto)
        salvar_oficinas(cadastro_editado)
        st.success("Cadastro atualizado permanentemente.")


# =========================================================
# RANKING
# =========================================================

elif pagina == "🏆 Ranking por Consultor":
    exigir_supabase()

    bases = listar_bases()
    datas = sorted(
        bases.loc[
            bases["tipo"] == "planejado",
            "data_operacional",
        ].astype(str).unique(),
        reverse=True,
    )

    if not datas:
        st.warning("Não existe planejado salvo.")
        st.stop()

    data_selecionada = st.selectbox(
        "Data do planejado",
        datas,
        format_func=lambda valor: pd.to_datetime(valor).strftime(
            "%d/%m/%Y"
        ),
    )

    cadastro = carregar_oficinas()
    planejado = filtrar_somente_manutencoes(
        carregar_base("planejado", data_selecionada)
    )

    if cadastro.empty:
        st.warning("Cadastre as oficinas.")
        st.stop()

    base = enriquecer_com_cadastro(planejado, cadastro)
    consultores = sorted(base["Consultor"].dropna().unique().tolist())

    consultor = st.selectbox("Consultor", consultores)
    base_consultor = base[base["Consultor"] == consultor]

    ranking = (
        base_consultor
        .groupby("Oficina")
        .agg(
            Planejadas=("Oficina", "size"),
            Tickets=("Ticket Jira", "nunique"),
            Placas=("Placa", "nunique"),
        )
        .reset_index()
        .sort_values(
            ["Planejadas", "Oficina"],
            ascending=[False, True],
        )
    )

    ranking.insert(0, "Posição", range(1, len(ranking) + 1))

    st.dataframe(
        ranking,
        use_container_width=True,
        hide_index=True,
    )


# =========================================================
# FOLLOW
# =========================================================


elif pagina == "🛰️ Controle Pré-Agenda":
    exigir_supabase()

    st.subheader("🛰️ Controle Pré-Agenda")
    st.caption(
        "Radar preventivo para evitar que OS cheguem ao dia da manutenção "
        "sem técnico atribuído. Prioridade operacional: D+1 e D+2."
    )

    fotografia, fotografia_anterior = carregar_duas_fotografias_pre_agenda()

    if fotografia.empty:
        st.info(
            "Ainda não existe fotografia de pré-agenda. "
            "Importe o CSV em Dados → Importações → Pré-agenda / Aceite."
        )
        st.stop()

    cadastro = carregar_oficinas()
    fotografia = enriquecer_pre_agenda_com_cadastro(
        fotografia,
        cadastro,
    )

    if not fotografia_anterior.empty:
        fotografia_anterior = enriquecer_pre_agenda_com_cadastro(
            fotografia_anterior,
            cadastro,
        )

    fotografia["Data_dt"] = pd.to_datetime(
        fotografia["Data operacional"],
        errors="coerce",
    )

    prioridades = fotografia["Data operacional"].apply(
        prioridade_pre_agenda
    )
    fotografia["Prioridade"] = prioridades.apply(
        lambda item: item[0]
    )
    fotografia["Dias para agenda"] = prioridades.apply(
        lambda item: item[1]
    )

    arquivo_atual = texto_limpo(
        fotografia.iloc[0].get("Arquivo", "")
    )
    importado_em = pd.to_datetime(
        fotografia.iloc[0].get("Importado em", ""),
        errors="coerce",
    )

    if pd.notna(importado_em):
        importado_txt = importado_em.strftime(
            "%d/%m/%Y %H:%M"
        )
    else:
        importado_txt = "não identificado"

    st.info(
        f"Fotografia atual: **{arquivo_atual}** · "
        f"importada em **{importado_txt}**"
    )

    # -----------------------------
    # Cards
    # -----------------------------
    total = len(fotografia)
    atribuida = int(
        (fotografia["Situação"] == "Atribuída ao técnico").sum()
    )
    rejeitada = int(
        (fotografia["Situação"] == "Rejeitada pela oficina").sum()
    )
    sem_tecnico = int(
        (
            fotografia["Situação"]
            == "Sem técnico / requer alocação"
        ).sum()
    )
    validar = int(
        (fotografia["Situação"] == "A validar").sum()
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Atividades na fotografia", total)
    c2.metric("🟢 Atribuídas", atribuida)
    c3.metric("🔴 Rejeitadas", rejeitada)
    c4.metric("🟠 Sem técnico", sem_tecnico)
    c5.metric("⚪ A validar", validar)

    # -----------------------------
    # Evolução entre fotografias
    # -----------------------------
    comparacao = comparar_fotografias_pre_agenda(
        fotografia,
        fotografia_anterior,
    )

    st.markdown("### 🔄 Evolução desde a fotografia anterior")

    if fotografia_anterior.empty:
        st.info(
            "Ainda não existe uma fotografia anterior comparável. "
            "A evolução ficará disponível a partir da próxima importação."
        )
    else:
        alocadas_novas = comparacao["alocadas_desde_anterior"]
        persistentes = comparacao["persistem_sem_tecnico"]
        novas_sem = comparacao["novas_sem_tecnico"]
        rejeitadas_novas = comparacao["rejeitadas_novas"]

        e1, e2, e3, e4 = st.columns(4)
        e1.metric(
            "🟢 Alocadas desde a foto anterior",
            len(alocadas_novas),
            help=(
                "Atividades que estavam sem técnico na fotografia anterior "
                "e agora estão atribuídas a um recurso diferente da oficina."
            ),
        )
        e2.metric(
            "🔴 Persistem sem técnico",
            len(persistentes),
            help=(
                "Atividades que já estavam sem técnico na fotografia anterior "
                "e continuam sem alocação. São as de maior atenção operacional."
            ),
        )
        e3.metric(
            "🟠 Novas sem técnico",
            len(novas_sem),
            help=(
                "Atividades sem técnico que não estavam presentes na fotografia anterior."
            ),
        )
        e4.metric(
            "🔴 Novas rejeitadas",
            len(rejeitadas_novas),
            help=(
                "Atividades que passaram a apresentar evidência de rejeição "
                "desde a fotografia anterior."
            ),
        )

        if not persistentes.empty:
            st.markdown("#### ⚠️ Persistem sem técnico desde a foto anterior")
            cols_persist = [
                coluna
                for coluna in [
                    "Data operacional",
                    "OS",
                    "Tipo de Atividade",
                    "Oficina",
                    "Consultor",
                    "Região",
                    "Recurso",
                    "Situação",
                    "Situação anterior",
                ]
                if coluna in persistentes.columns
            ]
            st.dataframe(
                persistentes[cols_persist].sort_values(
                    ["Data operacional", "Oficina", "OS"]
                ),
                use_container_width=True,
                hide_index=True,
            )

    # -----------------------------
    # Filtros
    # -----------------------------
    st.markdown("### Filtros")
    f1, f2, f3 = st.columns(3)

    consultores = sorted(
        {
            texto_limpo(v)
            for v in fotografia["Consultor"]
            if texto_limpo(v)
        }
    )
    oficinas = sorted(
        {
            texto_limpo(v)
            for v in fotografia["Oficina"]
            if texto_limpo(v)
        }
    )
    situacoes = sorted(
        {
            texto_limpo(v)
            for v in fotografia["Situação"]
            if texto_limpo(v)
        }
    )

    with f1:
        consultor_sel = multiselect_persistente(
            "Consultor",
            consultores,
            key="pre_agenda_consultor",
        )

    with f2:
        oficina_sel = multiselect_persistente(
            "Oficina",
            oficinas,
            key="pre_agenda_oficina",
        )

    with f3:
        situacao_sel = multiselect_persistente(
            "Situação",
            situacoes,
            key="pre_agenda_situacao",
        )

    filtrada = fotografia.copy()

    if consultor_sel:
        filtrada = filtrada[
            filtrada["Consultor"].isin(consultor_sel)
        ].copy()

    if oficina_sel:
        filtrada = filtrada[
            filtrada["Oficina"].isin(oficina_sel)
        ].copy()

    if situacao_sel:
        filtrada = filtrada[
            filtrada["Situação"].isin(situacao_sel)
        ].copy()

    # -----------------------------
    # Radar preventivo
    # -----------------------------
    st.divider()
    st.markdown("### 🚨 Radar preventivo")

    d1 = filtrada[
        (filtrada["Dias para agenda"] == 1)
        & (
            filtrada["Situação"].isin(
                [
                    "Sem técnico / requer alocação",
                    "Rejeitada pela oficina",
                    "A validar",
                ]
            )
        )
    ].copy()

    d2 = filtrada[
        (filtrada["Dias para agenda"] == 2)
        & (
            filtrada["Situação"].isin(
                [
                    "Sem técnico / requer alocação",
                    "Rejeitada pela oficina",
                    "A validar",
                ]
            )
        )
    ].copy()

    d0 = filtrada[
        (filtrada["Dias para agenda"] <= 0)
        & (
            filtrada["Situação"].isin(
                [
                    "Sem técnico / requer alocação",
                    "Rejeitada pela oficina",
                    "A validar",
                ]
            )
        )
    ].copy()

    r1, r2, r3 = st.columns(3)
    r1.metric(
        "🔴 D+1 — tratar hoje",
        len(d1),
        help=(
            "OS de amanhã que ainda estão rejeitadas, sem técnico "
            "ou precisam de validação."
        ),
    )
    r2.metric(
        "🟠 D+2 — atenção",
        len(d2),
        help=(
            "OS de depois de amanhã que ainda precisam de ação preventiva."
        ),
    )
    r3.metric(
        "⚫ D0/vencidas",
        len(d0),
        help=(
            "Casos que chegaram ao dia da agenda ou passaram dele "
            "sem resolução preventiva."
        ),
    )

    colunas_fila = [
        coluna
        for coluna in [
            "Prioridade",
            "Data operacional",
            "OS",
            "Tipo de Atividade",
            "Oficina",
            "Consultor",
            "Região",
            "Recurso",
            "Situação",
            "Motivo rejeição",
            "Status",
        ]
        if coluna in filtrada.columns
    ]

    st.markdown("#### 🔴 Fila D+1 — ação hoje")
    if d1.empty:
        st.success("Nenhuma pendência crítica para amanhã.")
    else:
        st.dataframe(
            d1[colunas_fila].sort_values(
                ["Oficina", "OS"]
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("#### 🟠 Fila D+2 — antecipar tratativa")
    if d2.empty:
        st.success("Nenhuma pendência para D+2.")
    else:
        st.dataframe(
            d2[colunas_fila].sort_values(
                ["Oficina", "OS"]
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.markdown("### Visão completa da fotografia")

    abas = st.tabs(
        [
            "🟠 Sem técnico",
            "🔴 Rejeitadas",
            "🟢 Atribuídas",
            "⚪ A validar",
            "📋 Todas",
        ]
    )

    grupos_situacao = [
        "Sem técnico / requer alocação",
        "Rejeitada pela oficina",
        "Atribuída ao técnico",
        "A validar",
        None,
    ]

    for aba, situacao in zip(
        abas,
        grupos_situacao,
    ):
        with aba:
            if situacao is None:
                grupo = filtrada.copy()
            else:
                grupo = filtrada[
                    filtrada["Situação"] == situacao
                ].copy()

            if grupo.empty:
                st.info("Nenhuma OS nesta categoria.")
            else:
                st.dataframe(
                    grupo[colunas_fila].sort_values(
                        ["Data operacional", "Oficina", "OS"]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )

elif pagina == "📉 Dashboard de Improdutividade":
    exigir_supabase()

    st.subheader("📉 Dashboard de Improdutividade")
    st.caption(
        "Análise histórica das manutenções não concluídas, separando "
        "improdutivas agendadas e extras com os motivos registrados no OFS."
    )

    bases = listar_bases()

    if bases.empty:
        st.warning("Não existem bases salvas para análise.")
        st.stop()

    datas_completas = sorted(listar_datas_completas_reais())

    if not datas_completas:
        st.warning(
            "Não existe período com planejado e resultado salvos juntos."
        )
        st.stop()

    cadastro = carregar_oficinas()
    consolidado = carregar_consolidado(datas_completas)

    if consolidado.empty:
        st.warning("Não foi possível montar o histórico.")
        st.stop()

    if not cadastro.empty:
        base = enriquecer_com_cadastro(
            consolidado,
            cadastro,
        )
    else:
        base = consolidado.copy()

    improdutivas = base[
        base["Classificação"].isin(
            [
                "Improdutiva agendada",
                "Improdutiva extra",
            ]
        )
    ].copy()

    if improdutivas.empty:
        st.info("Não existem improdutivas no período armazenado.")
        st.stop()

    # -----------------------------
    # FILTROS
    # -----------------------------
    st.markdown("### Filtros")

    f1, f2 = st.columns(2)

    data_min = pd.to_datetime(
        min(datas_completas)
    ).date()
    data_max = pd.to_datetime(
        max(datas_completas)
    ).date()

    with f1:
        periodo = date_input_persistente(
            "Período",
            value=(data_min, data_max),
            min_value=data_min,
            max_value=data_max,
            key="imp_periodo",
        )

    with f2:
        tipo = multiselect_persistente(
            "Tipo de improdutiva",
            [
                "Improdutiva agendada",
                "Improdutiva extra",
            ],
            default=[
                "Improdutiva agendada",
                "Improdutiva extra",
            ],
            key="imp_tipo",
        )

    if isinstance(periodo, (list, tuple)) and len(periodo) == 2:
        inicio, fim = periodo
    else:
        inicio = fim = (
            periodo[0]
            if isinstance(periodo, (list, tuple))
            else periodo
        )

    datas_base = pd.to_datetime(
        improdutivas["Data Operacional"],
        errors="coerce",
    ).dt.date

    filtrada = improdutivas[
        (datas_base >= inicio)
        & (datas_base <= fim)
    ].copy()

    if tipo:
        filtrada = filtrada[
            filtrada["Classificação"].isin(tipo)
        ].copy()

    consultores = sorted(
        {
            texto_limpo(v)
            for v in filtrada.get(
                "Consultor",
                pd.Series(dtype=str),
            )
            if texto_limpo(v)
        }
    )

    oficinas = sorted(
        {
            texto_limpo(v)
            for v in filtrada.get(
                "Oficina",
                pd.Series(dtype=str),
            )
            if texto_limpo(v)
        }
    )

    tecnicos = sorted(
        {
            texto_limpo(v)
            for v in filtrada.get(
                "Tecnico_recurso",
                pd.Series(dtype=str),
            )
            if texto_limpo(v)
        }
    )

    f3, f4, f5 = st.columns(3)

    with f3:
        consultor_filtro = multiselect_persistente(
            "Consultor",
            consultores,
            key="imp_consultor",
        )

    with f4:
        oficina_filtro = multiselect_persistente(
            "Oficina",
            oficinas,
            key="imp_oficina",
        )

    with f5:
        tecnico_filtro = multiselect_persistente(
            "Técnico / recurso",
            tecnicos,
            key="imp_tecnico",
        )

    if consultor_filtro:
        filtrada = filtrada[
            filtrada["Consultor"].isin(
                consultor_filtro
            )
        ].copy()

    if oficina_filtro:
        filtrada = filtrada[
            filtrada["Oficina"].isin(
                oficina_filtro
            )
        ].copy()

    if tecnico_filtro:
        filtrada = filtrada[
            filtrada["Tecnico_recurso"].isin(
                tecnico_filtro
            )
        ].copy()

    if filtrada.empty:
        st.info("Nenhuma improdutiva encontrada com esses filtros.")
        st.stop()

    # =====================================================
    # RANKINGS E DIAGNÓSTICO MD
    # =====================================================
    st.markdown("### 🧭 Rankings e diagnóstico MD")
    st.caption(
        "Detalhamento do período filtrado. Canceladas e OS Perdidas não entram "
        "na MD. O ranking crítico e o reconhecimento das oficinas com 0% "
        "ficam separados para facilitar a leitura."
    )

    datas_base_completa = pd.to_datetime(
        base["Data Operacional"],
        errors="coerce",
    ).dt.date
    base_md_filtrada = base[
        (datas_base_completa >= inicio)
        & (datas_base_completa <= fim)
    ].copy()

    if consultor_filtro:
        base_md_filtrada = base_md_filtrada[
            base_md_filtrada["Consultor"].isin(consultor_filtro)
        ].copy()

    if oficina_filtro:
        base_md_filtrada = base_md_filtrada[
            base_md_filtrada["Oficina"].isin(oficina_filtro)
        ].copy()

    if tecnico_filtro:
        base_md_filtrada = base_md_filtrada[
            base_md_filtrada["Tecnico_recurso"].isin(tecnico_filtro)
        ].copy()

    if "Consultor" not in base_md_filtrada.columns:
        base_md_filtrada["Consultor"] = "Não definido"

    base_md_filtrada["Região"] = base_md_filtrada["Consultor"].map(
        REGIOES_CONSULTORES
    ).fillna("Não definida")

    motivos_rank = resumo_motivos_md(
        preparar_diagnostico_md(filtrada)
    )
    regioes_rank = ranking_md_dimensao(
        base_md_filtrada, "Região", minimo_base_md=1
    )
    oficinas_rank = ranking_md_dimensao(
        base_md_filtrada, "Oficina", minimo_base_md=1
    )

    rr1, rr2 = st.columns(2)

    with rr1:
        st.markdown("#### Principais motivos")
        if motivos_rank.empty:
            st.info("Sem improdutivas para os filtros selecionados.")
        else:
            tabela = motivos_rank.copy()
            tabela["% das improdutivas"] = tabela[
                "% das improdutivas"
            ].map(lambda v: f"{v:.1f}%")
            st.dataframe(tabela, use_container_width=True, hide_index=True)

    with rr2:
        st.markdown("#### Ranking de regiões por MD")
        if regioes_rank.empty:
            st.info("Sem dados regionais suficientes.")
        else:
            tabela = regioes_rank.copy()
            tabela["MD (%)"] = tabela["MD (%)"].map(lambda v: f"{v:.1f}%")
            tabela["% motivo"] = tabela["% motivo"].map(lambda v: f"{v:.1f}%")
            st.dataframe(
                tabela[[
                    "Região", "Executadas", "Improdutivas", "Base MD",
                    "MD (%)", "Principal motivo", "% motivo",
                ]],
                use_container_width=True,
                hide_index=True,
            )

    st.markdown("#### 🔴 Ranking de oficinas com improdutividade — MD (%)")
    criticas = oficinas_rank[
        oficinas_rank["Improdutivas"] > 0
    ].copy()

    if criticas.empty:
        st.info("Nenhuma oficina com improdutividade no período filtrado.")
    else:
        criticas["MD (%)"] = criticas["MD (%)"].map(lambda v: f"{v:.1f}%")
        criticas["% motivo"] = criticas["% motivo"].map(lambda v: f"{v:.1f}%")
        st.dataframe(
            criticas[[
                "Oficina", "Consultor", "Região", "Executadas",
                "Improdutivas", "Base MD", "MD (%)",
                "Principal motivo", "% motivo",
            ]],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("#### 🏆 Oficinas com 0% de MD")
    positivas = oficinas_rank[
        (oficinas_rank["Improdutivas"] == 0)
        & (oficinas_rank["Base MD"] >= 3)
    ].copy()

    if positivas.empty:
        st.info(
            "Nenhuma oficina com 0% de MD e Base MD mínima de 3 "
            "no período filtrado."
        )
    else:
        positivas = positivas.sort_values(
            ["Base MD", "Executadas"],
            ascending=[False, False],
        ).reset_index(drop=True)
        st.caption(
            f"{len(positivas)} oficina(s) com zero improdutividade. "
            "Ordenadas pela maior Base MD."
        )
        st.dataframe(
            positivas[[
                "Oficina", "Consultor", "Região",
                "Executadas", "Base MD",
            ]],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # -----------------------------
    # INDICADORES
    # -----------------------------
    total = len(filtrada)
    agendadas = int(
        (
            filtrada["Classificação"]
            == "Improdutiva agendada"
        ).sum()
    )
    extras = int(
        (
            filtrada["Classificação"]
            == "Improdutiva extra"
        ).sum()
    )

    motivos_validos = filtrada[
        "Razao_improdutiva"
    ].apply(texto_limpo)

    com_motivo = int(
        (motivos_validos != "").sum()
    )

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Improdutivas totais", total)
    k2.metric("Agendadas", agendadas)
    k3.metric("Extras", extras)
    k4.metric(
        "% Agendadas",
        f"{(agendadas / total * 100):.1f}%",
    )
    k5.metric(
        "% com motivo OFS",
        f"{(com_motivo / total * 100):.1f}%",
    )

    st.divider()

    # -----------------------------
    # PRINCIPAIS MOTIVOS + AUDITORIA
    # -----------------------------
    analise = enriquecer_auditoria_improdutivas(
        filtrada
    )

    st.markdown("### Qualidade e conformidade dos apontamentos")

    qualidade_contagem = (
        analise["Qualidade apontamento"]
        .value_counts()
    )
    temporal_contagem = (
        analise["Conformidade temporal"]
        .value_counts()
    )

    q_coerentes = int(
        qualidade_contagem.get(
            "🟢 Coerente",
            0,
        )
    )
    q_revisar = int(
        qualidade_contagem.get(
            "🟡 Revisar",
            0,
        )
    )
    q_divergentes = int(
        qualidade_contagem.get(
            "🔴 Divergente",
            0,
        )
    )
    q_insuf = int(
        qualidade_contagem.get(
            "🟠 Justificativa insuficiente",
            0,
        )
    )
    q_sem_info = int(
        qualidade_contagem.get(
            "⚪ Sem informação",
            0,
        )
    )

    t_conformes = int(
        temporal_contagem.get(
            "🟢 Conforme",
            0,
        )
    )
    t_revisar = int(
        temporal_contagem.get(
            "🟡 Revisar",
            0,
        )
    )
    t_risco = int(
        temporal_contagem.get(
            "🔴 Risco de faturamento",
            0,
        )
    )

    qc1, qc2, qc3, qc4, qc5 = st.columns(5)
    qc1.metric("🟢 Coerentes", q_coerentes)
    qc2.metric("🟡 Revisar texto", q_revisar)
    qc3.metric("🔴 Divergentes", q_divergentes)
    qc4.metric("🟠 Justificativa insuficiente", q_insuf)
    qc5.metric("⚪ Sem observação", q_sem_info)

    tc1, tc2, tc3 = st.columns(3)
    tc1.metric("🟢 Temporal conforme", t_conformes)
    tc2.metric("🟡 Temporal revisar", t_revisar)
    tc3.metric("🔴 Risco de faturamento", t_risco)

    st.caption(
        "Regra temporal piloto: início da improdutiva deve estar dentro "
        "do campo 'Intervalo de Tempo' e a duração registrada deve ser "
        "de pelo menos 30 minutos. O painel sinaliza risco; não decide "
        "automaticamente faturamento."
    )

    fqual1, fqual2 = st.columns(2)

    with fqual1:
        filtro_qualidade = st.multiselect(
            "Filtrar qualidade do apontamento",
            sorted(
                analise[
                    "Qualidade apontamento"
                ].dropna().unique()
            ),
            key="imp_qualidade_apontamento",
        )

    with fqual2:
        filtro_temporal = st.multiselect(
            "Filtrar conformidade temporal",
            sorted(
                analise[
                    "Conformidade temporal"
                ].dropna().unique()
            ),
            key="imp_conformidade_temporal",
        )

    if filtro_qualidade:
        analise = analise[
            analise[
                "Qualidade apontamento"
            ].isin(
                filtro_qualidade
            )
        ].copy()

    if filtro_temporal:
        analise = analise[
            analise[
                "Conformidade temporal"
            ].isin(
                filtro_temporal
            )
        ].copy()

    if analise.empty:
        st.info(
            "Nenhuma improdutiva encontrada após aplicar "
            "os filtros de qualidade/conformidade."
        )
        st.stop()

    st.divider()
    st.markdown("### Principais motivos de improdutividade")

    analise["Motivo OFS"] = analise[
        "Razao_improdutiva"
    ].apply(
        lambda v: texto_limpo(v) or "Motivo não informado"
    )

    ranking = (
        analise
        .groupby(
            ["Motivo OFS", "Classificação"],
            dropna=False,
        )
        .size()
        .reset_index(name="Quantidade")
    )

    tabela_motivos = (
        ranking
        .pivot_table(
            index="Motivo OFS",
            columns="Classificação",
            values="Quantidade",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
    )

    for coluna in [
        "Improdutiva agendada",
        "Improdutiva extra",
    ]:
        if coluna not in tabela_motivos.columns:
            tabela_motivos[coluna] = 0

    tabela_motivos["Total"] = (
        tabela_motivos["Improdutiva agendada"]
        + tabela_motivos["Improdutiva extra"]
    )
    tabela_motivos["% do total"] = (
        tabela_motivos["Total"]
        / total
        * 100
    ).round(1)

    tabela_motivos = tabela_motivos.sort_values(
        "Total",
        ascending=False,
    )

    esquerda, direita = st.columns([1.25, 1])

    with esquerda:
        grafico_motivos = px.bar(
            ranking,
            x="Quantidade",
            y="Motivo OFS",
            color="Classificação",
            orientation="h",
            barmode="stack",
            text="Quantidade",
            title="Agendadas x Extras por motivo",
        )
        grafico_motivos.update_layout(
            height=max(
                430,
                55 * ranking["Motivo OFS"].nunique(),
            ),
            yaxis={
                "categoryorder": "total ascending"
            },
        )
        st.plotly_chart(
            grafico_motivos,
            use_container_width=True,
        )

    with direita:
        st.dataframe(
            tabela_motivos.rename(
                columns={
                    "Improdutiva agendada": "Agendadas",
                    "Improdutiva extra": "Extras",
                }
            ),
            use_container_width=True,
            hide_index=True,
            height=430,
        )

    # -----------------------------
    # EVOLUÇÃO POR DATA
    # -----------------------------
    st.divider()
    st.markdown("### Evolução das improdutivas")

    evolucao = (
        analise
        .groupby(
            ["Data Operacional", "Classificação"],
            dropna=False,
        )
        .size()
        .reset_index(name="Quantidade")
    )

    evolucao["Data Operacional"] = pd.to_datetime(
        evolucao["Data Operacional"],
        errors="coerce",
    )

    grafico_evolucao = px.bar(
        evolucao,
        x="Data Operacional",
        y="Quantidade",
        color="Classificação",
        barmode="group",
        text="Quantidade",
    )
    grafico_evolucao.update_layout(
        height=420,
    )
    st.plotly_chart(
        grafico_evolucao,
        use_container_width=True,
    )

    # -----------------------------
    # OFICINAS
    # -----------------------------
    st.divider()
    st.markdown("### Oficinas com mais improdutivas")

    ranking_oficinas = (
        analise
        .groupby(
            ["Oficina", "Classificação"],
            dropna=False,
        )
        .size()
        .reset_index(name="Quantidade")
    )

    grafico_oficinas = px.bar(
        ranking_oficinas,
        x="Quantidade",
        y="Oficina",
        color="Classificação",
        orientation="h",
        barmode="stack",
        text="Quantidade",
    )
    grafico_oficinas.update_layout(
        height=max(
            430,
            min(
                900,
                45 * ranking_oficinas["Oficina"].nunique(),
            ),
        ),
        yaxis={
            "categoryorder": "total ascending"
        },
    )
    st.plotly_chart(
        grafico_oficinas,
        use_container_width=True,
    )

    # =====================================================
    # TRATATIVA GERENCIAL EXCLUSIVA DA MD
    # =====================================================
    st.divider()
    st.markdown("### ✍️ Tratativa gerencial da MD")
    st.caption(
        "A fila abaixo é uma fila de trabalho: depois de salvar a tratativa, "
        "a OS sai da lista pendente e permanece registrada no banco/histórico. "
        "O apontamento original do OFS, os arquivos operacionais, a MCI e as "
        "contagens históricas continuam intactos."
    )

    analise_revisao = analise.copy()

    mascara_revisada = analise_revisao.get(
        "Revisao_MD",
        pd.Series(False, index=analise_revisao.index),
    ).fillna(False).astype(bool)

    total_carteira = len(analise_revisao)
    revisadas = int(mascara_revisada.sum())
    pendentes = max(total_carteira - revisadas, 0)

    no_show_tecnico = int(
        (
            analise_revisao.get(
                "Classificacao_gerencial_MD",
                pd.Series("", index=analise_revisao.index),
            ) == "No-show técnico"
        ).sum()
    )

    # Estado da aba/fila
    if "md_fila_status" not in st.session_state:
        st.session_state["md_fila_status"] = "Pendentes"

    b1, b2, b3, b4 = st.columns(4)

    with b1:
        if st.button(
            f"🟠 Pendentes ({pendentes})",
            use_container_width=True,
            type=(
                "primary"
                if st.session_state["md_fila_status"] == "Pendentes"
                else "secondary"
            ),
            key="btn_md_pendentes",
        ):
            st.session_state["md_fila_status"] = "Pendentes"
            st.rerun()

    with b2:
        if st.button(
            f"✅ Revisadas ({revisadas})",
            use_container_width=True,
            type=(
                "primary"
                if st.session_state["md_fila_status"] == "Revisadas"
                else "secondary"
            ),
            key="btn_md_revisadas",
        ):
            st.session_state["md_fila_status"] = "Revisadas"
            st.rerun()

    with b3:
        if st.button(
            f"📋 Todas ({total_carteira})",
            use_container_width=True,
            type=(
                "primary"
                if st.session_state["md_fila_status"] == "Todas"
                else "secondary"
            ),
            key="btn_md_todas",
        ):
            st.session_state["md_fila_status"] = "Todas"
            st.rerun()

    with b4:
        st.metric("No-show técnico", no_show_tecnico)

    st.caption(
        "Os filtros de Consultor, Oficina e Técnico/Recurso acima também "
        "controlam esta fila. Assim cada consultor trata apenas a sua carteira."
    )

    fila_status = st.session_state["md_fila_status"]

    # Regra principal:
    # - Pendentes = fila de trabalho.
    # - Revisadas = histórico das tratativas.
    # - Todas = visão administrativa, não fila operacional.
    if fila_status == "Pendentes":
        analise_revisao = analise_revisao[
            ~mascara_revisada
        ].copy()
    elif fila_status == "Revisadas":
        analise_revisao = analise_revisao[
            mascara_revisada
        ].copy()

    analise_revisao["OS_exibir"] = analise_revisao[
        "OS_resultado"
    ].apply(lambda v: texto_limpo(v) or "Sem OS")

    analise_revisao["Rotulo_revisao"] = analise_revisao.apply(
        lambda linha: (
            f"{texto_limpo(linha.get('Data Operacional', ''))} | "
            f"OS {texto_limpo(linha.get('OS_exibir', ''))} | "
            f"{texto_limpo(linha.get('Tecnico_recurso', ''))} | "
            f"{texto_limpo(linha.get('Oficina', ''))}"
        ),
        axis=1,
    )

    opcoes = analise_revisao["Rotulo_revisao"].tolist()

    if not opcoes:
        if fila_status == "Pendentes":
            st.success("Nenhuma OS pendente de revisão neste filtro.")
        elif fila_status == "Revisadas":
            st.info("Nenhuma OS revisada neste filtro.")
        else:
            st.info("Nenhuma OS encontrada neste filtro.")
    else:
        selecionada = st.selectbox(
            (
                "Selecione a OS para tratar"
                if fila_status == "Pendentes"
                else "Selecione a OS para consultar"
            ),
            opcoes,
            key=f"md_revisao_os_{fila_status}",
        )

        linha = analise_revisao[
            analise_revisao["Rotulo_revisao"] == selecionada
        ].iloc[0]

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                "**Motivo OFS:**  \n"
                + (
                    texto_limpo(linha.get("Razao_improdutiva", ""))
                    or "Não informado"
                )
            )
            st.markdown(
                "**Observação do técnico:**  \n"
                + (
                    texto_limpo(
                        linha.get("Observacao_tecnico_improdutiva", "")
                    )
                    or "Sem observação"
                )
            )
            st.markdown(
                "**Técnico / recurso:**  \n"
                + (
                    texto_limpo(linha.get("Tecnico_recurso", ""))
                    or "Não informado"
                )
            )

        with c2:
            st.markdown(
                "**Consultor:**  \n"
                + (
                    texto_limpo(linha.get("Consultor", ""))
                    or "Não definido"
                )
            )
            st.markdown(
                "**Oficina:**  \n"
                + (
                    texto_limpo(linha.get("Oficina", ""))
                    or "Não definida"
                )
            )
            st.markdown(
                "**Classificação MD atual:**  \n"
                + (
                    texto_limpo(
                        linha.get("Classificacao_gerencial_MD", "")
                    )
                    or "Improdutiva"
                )
            )

        classificacoes = [
            "Improdutiva",
            "No-show técnico",
            "No-show cliente",
            "Cancelada",
        ]
        atual = (
            texto_limpo(
                linha.get("Classificacao_gerencial_MD", "")
            )
            or "Improdutiva"
        )
        idx = (
            classificacoes.index(atual)
            if atual in classificacoes
            else 0
        )

        classificacao_md = st.selectbox(
            "Classificação gerencial para a MD",
            classificacoes,
            index=idx,
            disabled=(fila_status == "Revisadas"),
            help=(
                "No-show técnico, No-show cliente e Cancelada retiram a OS "
                "do universo da MD, mas não alteram o registro original."
            ),
            key=f"md_revisao_classificacao_{fila_status}",
        )

        motivo_original = texto_limpo(
            linha.get("Razao_improdutiva", "")
        )
        motivo_md = (
            texto_limpo(
                linha.get("Razao_improdutiva_md", "")
            )
            or motivo_original
        )

        motivos = [
            "Problemas técnicos com sistemas",
            "Problemas técnicos com veículos",
            "Veículo indisponível",
            "Equipamento / material",
            "Portaria / acesso",
            "Cliente / agendamento",
            "Técnico / capacidade",
            "OS / cadastro / direcionamento",
            "Sinistro",
        ]

        for valor in base.get(
            "Razao_improdutiva",
            pd.Series(dtype=str),
        ):
            valor = texto_limpo(valor)
            if valor and valor not in motivos:
                motivos.append(valor)

        if motivo_md and motivo_md not in motivos:
            motivos.insert(0, motivo_md)

        novo_motivo_md = motivo_md

        if classificacao_md == "Improdutiva":
            idx_motivo = (
                motivos.index(motivo_md)
                if motivo_md in motivos
                else 0
            )
            novo_motivo_md = st.selectbox(
                "Motivo validado para a MD",
                motivos,
                index=idx_motivo,
                disabled=(fila_status == "Revisadas"),
                key=f"md_revisao_motivo_{fila_status}",
            )
        else:
            st.info(
                f"Para a MD, esta OS está sendo tratada como "
                f"**{classificacao_md}** e não compõe a improdutividade "
                "do indicador."
            )

        justificativa = st.text_area(
            "Justificativa da tratativa",
            value=texto_limpo(
                linha.get("Justificativa_revisao_MD", "")
            ),
            disabled=(fila_status == "Revisadas"),
            key=f"md_revisao_justificativa_{fila_status}",
        )

        revisor = st.text_input(
            "Responsável pela revisão",
            value=texto_limpo(
                linha.get("Revisor_MD", "")
            ),
            disabled=(fila_status == "Revisadas"),
            key=f"md_revisao_responsavel_{fila_status}",
        )

        if fila_status != "Revisadas":
            s1, s2 = st.columns(2)

            with s1:
                if st.button(
                    "💾 Salvar e concluir revisão",
                    type="primary",
                    use_container_width=True,
                    key=f"salvar_revisao_md_{fila_status}",
                ):
                    if not texto_limpo(justificativa):
                        st.error(
                            "Informe a justificativa da tratativa."
                        )
                    else:
                        salvar_revisao_md(
                            texto_limpo(
                                linha.get("Data Operacional", "")
                            ),
                            texto_limpo(
                                linha.get("Chave Atendimento", "")
                            ),
                            motivo_original,
                            classificacao_md,
                            novo_motivo_md,
                            justificativa,
                            revisor,
                        )

                        # Após concluir, volta para a fila de pendentes.
                        # A OS recém-revisada desaparece imediatamente dessa fila.
                        st.session_state["md_fila_status"] = "Pendentes"
                        st.success(
                            "Revisão concluída. A OS saiu da fila pendente "
                            "e ficou registrada no histórico."
                        )
                        st.rerun()

            with s2:
                if bool(linha.get("Revisao_MD", False)):
                    if st.button(
                        "↩️ Remover tratativa e reabrir OS",
                        use_container_width=True,
                        key=f"remover_revisao_md_{fila_status}",
                    ):
                        excluir_revisao_md(
                            texto_limpo(
                                linha.get("Data Operacional", "")
                            ),
                            texto_limpo(
                                linha.get("Chave Atendimento", "")
                            ),
                        )
                        st.session_state["md_fila_status"] = "Pendentes"
                        st.success(
                            "Tratativa removida. A OS voltou para a fila pendente."
                        )
                        st.rerun()
        else:
            if st.button(
                "↩️ Reabrir esta OS para nova revisão",
                use_container_width=True,
                key="reabrir_revisao_md",
            ):
                excluir_revisao_md(
                    texto_limpo(
                        linha.get("Data Operacional", "")
                    ),
                    texto_limpo(
                        linha.get("Chave Atendimento", "")
                    ),
                )
                st.session_state["md_fila_status"] = "Pendentes"
                st.success(
                    "OS reaberta e devolvida à fila pendente."
                )
                st.rerun()

    revisoes_ativas = analise[
        analise.get(
            "Revisao_MD",
            pd.Series(False, index=analise.index),
        ).fillna(False).astype(bool)
    ].copy()

    if not revisoes_ativas.empty:
        st.markdown("#### Histórico de tratativas no filtro atual")
        cols = [
            c
            for c in [
                "Data Operacional",
                "OS_resultado",
                "Consultor",
                "Oficina",
                "Tecnico_recurso",
                "Razao_improdutiva",
                "Classificacao_gerencial_MD",
                "Razao_improdutiva_md",
                "Justificativa_revisao_MD",
                "Revisor_MD",
            ]
            if c in revisoes_ativas.columns
        ]
        st.dataframe(
            revisoes_ativas[cols].rename(
                columns={
                    "OS_resultado": "OS",
                    "Tecnico_recurso": "Técnico / recurso",
                    "Razao_improdutiva": "Motivo OFS",
                    "Classificacao_gerencial_MD": "Classificação MD",
                    "Razao_improdutiva_md": "Motivo validado MD",
                    "Justificativa_revisao_MD": "Justificativa",
                    "Revisor_MD": "Revisor",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

    # -----------------------------
    # DETALHAMENTO
    # -----------------------------
    st.divider()
    st.markdown("### Detalhamento das OS improdutivas")
    st.caption(
        "O motivo e a observação originais do OFS são preservados. "
        "As colunas de qualidade e conformidade são uma camada analítica "
        "do painel para priorizar revisões."
    )

    colunas_detalhe = [
        "Data Operacional",
        "Classificação",
        "OS_resultado",
        "Ticket",
        "Placa",
        "Oficina",
        "Consultor",
        "Tecnico_recurso",
        "Cliente_resultado",
        "Cidade_resultado",
        "Razao_improdutiva",
        "Razao_improdutiva_md",
        "Classificacao_gerencial_MD",
        "Revisao_MD",
        "Justificativa_revisao_MD",
        "Revisor_MD",
        "Observacao_tecnico_improdutiva",
        "Qualidade apontamento",
        "Motivo sugerido",
        "Leitura qualidade",
        "Intervalo_tempo",
        "Janela_servico_detalhada",
        "Inicio_real",
        "Fim_real",
        "Duracao_real",
        "Conformidade temporal",
        "Elegibilidade",
        "Leitura temporal",
    ]

    colunas_detalhe = [
        c
        for c in colunas_detalhe
        if c in analise.columns
    ]

    detalhe_imp = analise[
        colunas_detalhe
    ].copy()

    detalhe_imp = detalhe_imp.rename(
        columns={
            "OS_resultado": "OS",
            "Razao_improdutiva": "Razão da Improdutiva",
            "Razao_improdutiva_md": "Motivo validado para MD",
            "Classificacao_gerencial_MD": "Classificação gerencial MD",
            "Revisao_MD": "Revisão MD ativa",
            "Justificativa_revisao_MD": "Justificativa revisão MD",
            "Revisor_MD": "Revisor MD",
            "Observacao_tecnico_improdutiva": (
                "Observação do Técnico"
            ),
            "Tecnico_recurso": "Técnico / Recurso",
            "Cliente_resultado": "Cliente",
            "Cidade_resultado": "Cidade",
            "Intervalo_tempo": "Janela contratada",
            "Janela_servico_detalhada": "Janela OFS detalhada",
            "Inicio_real": "Início",
            "Fim_real": "Fim",
            "Duracao_real": "Duração",
        }
    )

    st.dataframe(
        detalhe_imp,
        use_container_width=True,
        hide_index=True,
        height=520,
    )

    st.download_button(
        "⬇️ Baixar análise de improdutivas em Excel",
        data=dataframe_para_excel(
            detalhe_imp,
            "Improdutivas",
        ),
        file_name="analise_improdutivas.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


# =========================================================
# FOLLOW
# =========================================================

elif pagina == "🔄 Follow × Resultado OFS":
    exigir_supabase()

    st.subheader("🔄 Follow × Resultado OFS")
    st.caption(
        "Compara o que a oficina informou antes da execução com o "
        "resultado real registrado posteriormente no OFS."
    )

    contatos = pd.DataFrame(
        buscar_todos(
            "follow_contatos",
            ordem="data_manutencao",
            desc=True,
        )
    )

    if contatos.empty:
        st.info("Ainda não existem registros de Follow.")
        st.stop()

    bases = listar_bases()

    if bases.empty:
        st.warning("Não existem bases de resultado para cruzamento.")
        st.stop()

    datas_completas = sorted(listar_datas_completas_reais())

    if not datas_completas:
        st.warning(
            "Não existe período com planejado e resultado salvos juntos."
        )
        st.stop()

    cadastro = carregar_oficinas()
    consolidado = carregar_consolidado(
        datas_completas
    )

    if consolidado.empty:
        st.warning(
            "Não foi possível montar a base consolidada para o cruzamento."
        )
        st.stop()

    if not cadastro.empty:
        consolidado = enriquecer_com_cadastro(
            consolidado,
            cadastro,
        )

    cruzamento = montar_base_follow_resultado(
        contatos,
        consolidado,
    )

    if cruzamento.empty:
        st.info("Não há dados suficientes para análise.")
        st.stop()

    st.markdown("### Filtros")

    datas_validas = pd.to_datetime(
        cruzamento["Data"],
        errors="coerce",
    ).dropna()

    min_data = datas_validas.min().date()
    max_data = datas_validas.max().date()

    f1, f2, f3 = st.columns(3)

    with f1:
        periodo = date_input_persistente(
            "Período",
            value=(min_data, max_data),
            min_value=min_data,
            max_value=max_data,
            key="cruz_follow_periodo",
        )

    with f2:
        consultores = sorted(
            {
                texto_limpo(v)
                for v in cruzamento["Consultor"]
                if texto_limpo(v)
            }
        )
        consultor_filtro = multiselect_persistente(
            "Consultor",
            consultores,
            key="cruz_follow_consultor",
        )

    with f3:
        tipos_coerencia = sorted(
            cruzamento[
                "Coerência Follow × Resultado"
            ].dropna().unique().tolist()
        )
        coerencia_filtro = multiselect_persistente(
            "Situação",
            tipos_coerencia,
            key="cruz_follow_coerencia",
        )

    if isinstance(periodo, (list, tuple)) and len(periodo) == 2:
        inicio, fim = periodo
    else:
        inicio = fim = (
            periodo[0]
            if isinstance(periodo, (list, tuple))
            else periodo
        )

    data_series = pd.to_datetime(
        cruzamento["Data"],
        errors="coerce",
    ).dt.date

    filtrado = cruzamento[
        (data_series >= inicio)
        & (data_series <= fim)
    ].copy()

    if consultor_filtro:
        filtrado = filtrado[
            filtrado["Consultor"].isin(
                consultor_filtro
            )
        ]

    if coerencia_filtro:
        filtrado = filtrado[
            filtrado[
                "Coerência Follow × Resultado"
            ].isin(coerencia_filtro)
        ]

    if filtrado.empty:
        st.info("Nenhum registro encontrado com esses filtros.")
        st.stop()

    total = len(filtrado)
    divergencias = int(
        (
            filtrado[
                "Coerência Follow × Resultado"
            ] == "Divergência do Follow"
        ).sum()
    )
    riscos_antecipados = int(
        (
            filtrado[
                "Coerência Follow × Resultado"
            ] == "Risco antecipado pelo Follow"
        ).sum()
    )
    riscos_tratados = int(
        (
            filtrado[
                "Coerência Follow × Resultado"
            ] == "Risco tratado / execução realizada"
        ).sum()
    )
    coerentes = int(
        (
            filtrado[
                "Coerência Follow × Resultado"
            ] == "Confirmação coerente"
        ).sum()
    )
    sem_resposta = int(
        (
            filtrado[
                "Coerência Follow × Resultado"
            ] == "Sem resposta prévia"
        ).sum()
    )

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Follows analisados", total)
    c2.metric("Confirmações coerentes", coerentes)
    c3.metric("Riscos antecipados", riscos_antecipados)
    c4.metric("Riscos tratados", riscos_tratados)
    c5.metric("Divergências", divergencias)
    c6.metric("Sem resposta", sem_resposta)

    st.divider()
    st.markdown("### Placar de coerência")

    resumo = (
        filtrado[
            "Coerência Follow × Resultado"
        ]
        .value_counts()
        .rename_axis("Situação")
        .reset_index(name="Quantidade")
    )

    grafico = px.bar(
        resumo,
        x="Quantidade",
        y="Situação",
        orientation="h",
        text="Quantidade",
    )
    grafico.update_layout(
        height=max(
            420,
            48 * len(resumo),
        ),
        yaxis={
            "categoryorder": "total ascending"
        },
    )
    st.plotly_chart(
        grafico,
        use_container_width=True,
    )

    st.divider()
    st.markdown("### Casos que exigem atenção")

    situacoes_criticas = [
        "Divergência do Follow",
        "Improdutiva não antecipada",
        "Risco informado, motivo diferente",
        "OS Perdida não antecipada",
        "Cancelamento não antecipado",
        "Requer auditoria de OS",
    ]

    criticos = filtrado[
        filtrado[
            "Coerência Follow × Resultado"
        ].isin(situacoes_criticas)
    ].copy()

    if criticos.empty:
        st.success(
            "Nenhuma divergência crítica encontrada no período selecionado."
        )
    else:
        st.dataframe(
            criticos[
                [
                    "Data",
                    "Consultor",
                    "Oficina",
                    "Qtd agendadas",
                    "Riscos informados",
                    "Resultado OFS",
                    "Razão da Improdutiva",
                    "Categoria motivo OFS",
                    "Coerência Follow × Resultado",
                    "Leitura",
                    "Status ação",
                    "Responsável ação",
                    "Prazo ação",
                ]
            ],
            use_container_width=True,
            hide_index=True,
            height=520,
        )

    st.divider()
    st.markdown("### Base completa da análise")

    st.dataframe(
        filtrado,
        use_container_width=True,
        hide_index=True,
        height=520,
    )

    st.download_button(
        "⬇️ Baixar cruzamento Follow × OFS em Excel",
        data=dataframe_para_excel(
            filtrado,
            "Follow_x_OFS",
        ),
        file_name="follow_x_resultado_ofs.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


elif pagina == "🧭 Painel de Ações do Follow":
    exigir_supabase()

    st.subheader("🧭 Painel de Ações do Follow")
    st.caption(
        "Transforma as respostas das oficinas em um placar operacional "
        "com prioridade, impedimentos, responsáveis, prazos e ações."
    )

    contatos = pd.DataFrame(
        buscar_todos(
            "follow_contatos",
            ordem="data_manutencao",
            desc=True,
        )
    )

    if contatos.empty:
        st.info("Ainda não existem registros de Follow.")
        st.stop()

    linhas = []

    for _, contato in contatos.iterrows():
        follow_id = int(contato["id"])
        resposta = carregar_resposta_mais_recente_follow(
            follow_id
        )

        motivos = resposta.get("motivos") or []
        os_afetadas = resposta.get("os_afetadas") or []

        risco = classificar_risco_follow(
            contato.to_dict(),
            resposta,
        )

        linhas.append(
            {
                "Follow ID": follow_id,
                "Data manutenção": texto_limpo(
                    contato.get("data_manutencao")
                ),
                "Consultor": texto_limpo(
                    contato.get("consultor")
                ),
                "Oficina": texto_limpo(
                    contato.get("oficina")
                ),
                "Qtd agendadas": int(
                    contato.get("qtd_agendadas") or 0
                ),
                "Status Follow": texto_limpo(
                    contato.get("status")
                ) or "Preparado",
                "Respondido em": texto_limpo(
                    contato.get("respondido_em")
                ),
                "Tem impedimento": bool(
                    contato.get("tem_impedimento")
                )
                if contato.get("respondido_em")
                else False,
                "Risco": risco,
                "Motivos": " | ".join(
                    str(v) for v in motivos
                ),
                "OS afetadas": " | ".join(
                    str(v) for v in os_afetadas
                ),
                "Observação oficina": texto_limpo(
                    resposta.get("observacao")
                ),
                "Previsão oficina": texto_limpo(
                    resposta.get("previsao_solucao")
                ),
                "Status ação": texto_limpo(
                    contato.get("status_acao")
                ) or "Não iniciado",
                "Responsável ação": texto_limpo(
                    contato.get("responsavel_acao")
                ),
                "Ação tomada": texto_limpo(
                    contato.get("acao_tomada")
                ),
                "Prazo ação": texto_limpo(
                    contato.get("prazo_acao")
                ),
                "Observação ação": texto_limpo(
                    contato.get("observacao_acao")
                ),
            }
        )

    painel = pd.DataFrame(linhas)

    # -----------------------------
    # FILTROS
    # -----------------------------
    st.markdown("### Filtros")

    datas_validas = pd.to_datetime(
        painel["Data manutenção"],
        errors="coerce",
    ).dropna()

    min_data = datas_validas.min().date()
    max_data = datas_validas.max().date()

    f1, f2, f3, f4 = st.columns(4)

    with f1:
        periodo = date_input_persistente(
            "Período",
            value=(min_data, max_data),
            min_value=min_data,
            max_value=max_data,
            key="acao_follow_periodo",
        )

    with f2:
        consultores = sorted(
            {
                v for v in painel["Consultor"]
                if texto_limpo(v)
            }
        )
        filtro_consultor = multiselect_persistente(
            "Consultor",
            consultores,
            key="acao_follow_consultor",
        )

    with f3:
        filtro_risco = multiselect_persistente(
            "Risco",
            ["Alto", "Médio", "Baixo", "Sem resposta"],
            key="acao_follow_risco",
        )

    with f4:
        filtro_status_acao = multiselect_persistente(
            "Status da ação",
            [
                "Não iniciado",
                "Em andamento",
                "Aguardando oficina",
                "Aguardando cliente",
                "Concluído",
            ],
            key="acao_follow_status",
        )

    if isinstance(periodo, (list, tuple)) and len(periodo) == 2:
        inicio, fim = periodo
    else:
        inicio = fim = (
            periodo[0]
            if isinstance(periodo, (list, tuple))
            else periodo
        )

    data_series = pd.to_datetime(
        painel["Data manutenção"],
        errors="coerce",
    ).dt.date

    filtrado = painel[
        (data_series >= inicio)
        & (data_series <= fim)
    ].copy()

    if filtro_consultor:
        filtrado = filtrado[
            filtrado["Consultor"].isin(
                filtro_consultor
            )
        ]

    if filtro_risco:
        filtrado = filtrado[
            filtrado["Risco"].isin(
                filtro_risco
            )
        ]

    if filtro_status_acao:
        filtrado = filtrado[
            filtrado["Status ação"].isin(
                filtro_status_acao
            )
        ]

    if filtrado.empty:
        st.info("Nenhum registro encontrado com esses filtros.")
        st.stop()

    exibir_cards_follow_acao(filtrado)

    st.divider()

    # -----------------------------
    # PLACAR DE RISCO
    # -----------------------------
    st.markdown("### Placar de risco")

    risco_resumo = (
        filtrado["Risco"]
        .value_counts()
        .rename_axis("Risco")
        .reset_index(name="Quantidade")
    )

    grafico_risco = px.bar(
        risco_resumo,
        x="Risco",
        y="Quantidade",
        text="Quantidade",
    )
    grafico_risco.update_layout(
        showlegend=False,
        height=360,
    )
    st.plotly_chart(
        grafico_risco,
        use_container_width=True,
    )

    st.divider()

    # -----------------------------
    # IMPEDIMENTOS
    # -----------------------------
    st.markdown("### Principais impedimentos informados")

    impedimentos = filtrado[
        filtrado["Motivos"].apply(
            lambda x: bool(texto_limpo(x))
        )
    ].copy()

    if impedimentos.empty:
        st.info(
            "Nenhum impedimento detalhado no período selecionado."
        )
    else:
        motivos_explodidos = []

        for _, linha in impedimentos.iterrows():
            for motivo in str(linha["Motivos"]).split(" | "):
                motivo = texto_limpo(motivo)
                if motivo:
                    motivos_explodidos.append(
                        {
                            "Motivo": motivo,
                            "Consultor": linha["Consultor"],
                            "Oficina": linha["Oficina"],
                        }
                    )

        df_motivos = pd.DataFrame(
            motivos_explodidos
        )

        ranking_motivos = (
            df_motivos["Motivo"]
            .value_counts()
            .rename_axis("Motivo")
            .reset_index(name="Quantidade")
        )

        grafico_motivos = px.bar(
            ranking_motivos,
            x="Quantidade",
            y="Motivo",
            orientation="h",
            text="Quantidade",
        )
        grafico_motivos.update_layout(
            height=max(
                380,
                48 * len(ranking_motivos),
            ),
            yaxis={
                "categoryorder": "total ascending"
            },
        )
        st.plotly_chart(
            grafico_motivos,
            use_container_width=True,
        )

    st.divider()

    # -----------------------------
    # FILA DE AÇÃO
    # -----------------------------
    st.markdown("### Fila de ação")
    st.caption(
        "Priorize os registros de risco alto, impedimentos e ausência de resposta."
    )

    prioridade_ordem = {
        "Alto": 1,
        "Sem resposta": 2,
        "Médio": 3,
        "Baixo": 4,
    }

    fila = filtrado.copy()
    fila["__prioridade"] = fila["Risco"].map(
        prioridade_ordem
    ).fillna(99)

    fila = fila.sort_values(
        ["__prioridade", "Data manutenção", "Oficina"]
    )

    tabela_fila = fila[
        [
            "Follow ID",
            "Data manutenção",
            "Consultor",
            "Oficina",
            "Qtd agendadas",
            "Risco",
            "Motivos",
            "OS afetadas",
            "Status ação",
            "Responsável ação",
            "Prazo ação",
        ]
    ].copy()

    st.dataframe(
        tabela_fila,
        use_container_width=True,
        hide_index=True,
        height=430,
    )

    st.divider()

    # -----------------------------
    # AÇÃO SOBRE UM FOLLOW
    # -----------------------------
    st.markdown("### Registrar / atualizar ação")

    opcoes = [
        (
            int(row["Follow ID"]),
            (
                f"{row['Data manutenção']} · "
                f"{row['Oficina']} · "
                f"{row['Risco']}"
            ),
        )
        for _, row in fila.iterrows()
    ]

    mapa_opcoes = {
        label: follow_id
        for follow_id, label in opcoes
    }

    selecionado_label = st.selectbox(
        "Selecione um registro",
        list(mapa_opcoes.keys()),
        key="acao_follow_registro",
    )

    follow_id_sel = mapa_opcoes[
        selecionado_label
    ]

    atual = fila[
        fila["Follow ID"] == follow_id_sel
    ].iloc[0]

    with st.container(border=True):
        st.markdown(
            f"#### {atual['Oficina']} — {atual['Risco']}"
        )
        st.write(
            f"**Data:** {atual['Data manutenção']}  \n"
            f"**Consultor:** {atual['Consultor']}  \n"
            f"**Motivos:** {atual['Motivos'] or 'Não informado'}  \n"
            f"**OS afetadas:** {atual['OS afetadas'] or 'Não informado'}  \n"
            f"**Observação da oficina:** "
            f"{atual['Observação oficina'] or 'Não informada'}"
        )

        a1, a2 = st.columns(2)

        with a1:
            status_acao = st.selectbox(
                "Status da ação",
                [
                    "Não iniciado",
                    "Em andamento",
                    "Aguardando oficina",
                    "Aguardando cliente",
                    "Concluído",
                ],
                index=[
                    "Não iniciado",
                    "Em andamento",
                    "Aguardando oficina",
                    "Aguardando cliente",
                    "Concluído",
                ].index(
                    atual["Status ação"]
                    if atual["Status ação"]
                    in [
                        "Não iniciado",
                        "Em andamento",
                        "Aguardando oficina",
                        "Aguardando cliente",
                        "Concluído",
                    ]
                    else "Não iniciado"
                ),
                key="acao_follow_status_edicao",
            )

            responsavel = st.text_input(
                "Responsável pela ação",
                value=atual["Responsável ação"],
                key="acao_follow_responsavel",
            )

        with a2:
            prazo = st.date_input(
                "Prazo da ação",
                value=(
                    pd.to_datetime(
                        atual["Prazo ação"],
                        errors="coerce",
                    ).date()
                    if texto_limpo(
                        atual["Prazo ação"]
                    )
                    else pd.to_datetime(
                        atual["Data manutenção"]
                    ).date()
                ),
                key="acao_follow_prazo",
            )

            acao_tomada = st.text_area(
                "Ação / encaminhamento",
                value=atual["Ação tomada"],
                key="acao_follow_acao",
            )

        observacao_acao = st.text_area(
            "Observação interna",
            value=atual["Observação ação"],
            key="acao_follow_obs",
        )

        if st.button(
            "💾 Salvar ação",
            type="primary",
            use_container_width=True,
        ):
            atualizar_acao_follow(
                follow_id=follow_id_sel,
                status_acao=status_acao,
                responsavel_acao=responsavel,
                acao_tomada=acao_tomada,
                prazo_acao=prazo.isoformat(),
                observacao_acao=observacao_acao,
            )
            st.success("Ação atualizada.")
            st.rerun()

    st.divider()

    # -----------------------------
    # EXPORTAÇÃO
    # -----------------------------
    st.download_button(
        "⬇️ Baixar placar do Follow em Excel",
        data=dataframe_para_excel(
            filtrado.drop(
                columns=["__prioridade"],
                errors="ignore",
            ),
            "Follow_Acoes",
        ),
        file_name="follow_acoes.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


elif pagina == "📞 Follow":
    exigir_supabase()

    bases = listar_bases()
    datas = sorted(
        bases.loc[
            bases["tipo"] == "planejado",
            "data_operacional",
        ].astype(str).unique(),
        reverse=True,
    )

    if not datas:
        st.warning("Não existe agendamento salvo.")
        st.stop()

    st.subheader("Follow preventivo das oficinas")
    st.caption(
        "Confirme antecipadamente as manutenções agendadas, "
        "registre os contatos e acompanhe impedimentos."
    )

    data_selecionada = selectbox_persistente(
        "Data das manutenções",
        datas,
        key="follow_preventivo_data",
        format_func=lambda valor: pd.to_datetime(valor).strftime(
            "%d/%m/%Y"
        ),
    )

    cadastro = carregar_oficinas()
    planejado = filtrar_somente_manutencoes(
        carregar_base("planejado", data_selecionada)
    )

    if cadastro.empty:
        st.warning("Cadastre as oficinas.")
        st.stop()

    # Mantemos somente a fotografia vigente do planejamento.
    if "__Ativa no Planejamento" in planejado.columns:
        planejado = planejado[
            planejado["__Ativa no Planejamento"] == True
        ].copy()

    if planejado.empty:
        st.info("Não há manutenções vigentes para esta data.")
        st.stop()

    # Classifica canceladas para excluir do follow acionável,
    # sem apagar do histórico.
    planejado["__Cancelada"] = planejado[
        "Status da Atividade"
    ].apply(status_cancelado)

    base_todas = enriquecer_com_cadastro(
        planejado,
        cadastro,
    )

    consultores = sorted(
        {
            texto_limpo(valor)
            for valor in base_todas["Consultor"]
            if texto_limpo(valor)
        }
    )

    if not consultores:
        st.warning(
            "Nenhum consultor foi relacionado às oficinas desta data."
        )
        st.stop()

    consultor = selectbox_persistente(
        "Consultor",
        consultores,
        key="follow_preventivo_consultor",
    )

    base_consultor_todas = base_todas[
        base_todas["Consultor"] == consultor
    ].copy()

    if base_consultor_todas.empty:
        st.info(
            "Não há manutenções para este consultor."
        )
        st.stop()

    # Follow acionável: somente manutenção vigente e não cancelada.
    base_consultor = base_consultor_todas[
        base_consultor_todas["__Cancelada"] == False
    ].copy()

    # Resumo por oficina considerando total encontrado, canceladas e acionáveis.
    resumo_oficinas = (
        base_consultor_todas
        .groupby(
            [
                "Chave Oficina",
                "Oficina",
                "WhatsApp",
                "Prioridade",
            ],
            dropna=False,
        )
        .agg(
            Encontradas=("Oficina", "size"),
            Canceladas=("__Cancelada", "sum"),
            OS_Todas=(
                "OS",
                lambda serie: sorted(
                    {
                        texto_limpo(valor)
                        for valor in serie
                        if texto_limpo(valor)
                    }
                ),
            ),
        )
        .reset_index()
    )

    acionaveis = (
        base_consultor
        .groupby(
            [
                "Chave Oficina",
                "Oficina",
                "WhatsApp",
                "Prioridade",
            ],
            dropna=False,
        )
        .agg(
            Para_Follow=("Oficina", "size"),
            OS_Follow=(
                "OS",
                lambda serie: sorted(
                    {
                        texto_limpo(valor)
                        for valor in serie
                        if texto_limpo(valor)
                    }
                ),
            ),
        )
        .reset_index()
    )

    agrupado = resumo_oficinas.merge(
        acionaveis,
        on=[
            "Chave Oficina",
            "Oficina",
            "WhatsApp",
            "Prioridade",
        ],
        how="left",
    )

    agrupado["Para_Follow"] = (
        agrupado["Para_Follow"]
        .fillna(0)
        .astype(int)
    )
    agrupado["Canceladas"] = (
        agrupado["Canceladas"]
        .fillna(0)
        .astype(int)
    )
    agrupado["OS_Follow"] = agrupado["OS_Follow"].apply(
        lambda valor: valor if isinstance(valor, list) else []
    )

    # Só exibimos oficinas que ainda têm pelo menos uma manutenção acionável.
    agrupado = agrupado[
        agrupado["Para_Follow"] > 0
    ].copy()

    agrupado = agrupado.sort_values(
        ["Para_Follow", "Oficina"],
        ascending=[False, True],
    )

    if agrupado.empty:
        st.info(
            "Não há manutenções acionáveis para Follow "
            "neste consultor/data. As encontradas podem estar canceladas."
        )
        st.stop()

    exibir_metricas_follow(
        consultor,
        data_selecionada,
        len(agrupado),
    )

    st.divider()
    st.markdown("### Oficinas para contato")
    st.caption(
        "O Follow considera somente manutenções vigentes e não canceladas. "
        "As canceladas continuam armazenadas para histórico e auditoria."
    )

    quantidade_maxima = len(agrupado)

    if quantidade_maxima == 1:
        quantidade = 1
        st.caption(
            "1 oficina disponível para o consultor selecionado."
        )
    else:
        quantidade = st.slider(
            "Quantidade de oficinas exibidas",
            min_value=1,
            max_value=quantidade_maxima,
            value=min(3, quantidade_maxima),
        )

    for _, linha in agrupado.head(
        quantidade
    ).iterrows():
        chave_oficina = texto_limpo(
            linha["Chave Oficina"]
        )
        oficina = texto_limpo(
            linha["Oficina"]
        )
        telefone = limpar_telefone(
            linha["WhatsApp"]
        )
        prioridade = (
            texto_limpo(linha["Prioridade"])
            or "Normal"
        )

        qtd_encontradas = int(
            linha["Encontradas"]
        )
        qtd_canceladas = int(
            linha["Canceladas"]
        )
        qtd_agendadas = int(
            linha["Para_Follow"]
        )
        os_agendadas = list(
            linha["OS_Follow"]
            if isinstance(linha["OS_Follow"], list)
            else []
        )

        follow = obter_ou_criar_follow(
            data_manutencao=data_selecionada,
            chave_oficina=chave_oficina,
            oficina=oficina,
            consultor=consultor,
            telefone=telefone,
            qtd_agendadas=qtd_agendadas,
            os_agendadas=os_agendadas,
        )

        with st.container(border=True):
            topo1, topo2, topo3, topo4 = st.columns(
                [4, 1.5, 1.5, 2]
            )

            topo1.subheader(oficina)
            topo1.caption(
                f"Prioridade: {prioridade}"
            )
            topo2.metric(
                "Encontradas",
                qtd_encontradas,
            )
            topo3.metric(
                "Para Follow",
                qtd_agendadas,
            )
            topo4.metric(
                "Canceladas",
                qtd_canceladas,
            )

            status = texto_limpo(
                follow.get("status", "Preparado")
            )
            resposta = texto_limpo(
                follow.get("status_resposta", "")
            )

            status_exibir = (
                f"{status} · {resposta}"
                if resposta
                else status
            )

            st.caption(
                f"Status do Follow: **{status_exibir}**"
            )

            if os_agendadas:
                st.caption(
                    "OS para Follow: "
                    + ", ".join(
                        os_agendadas[:12]
                    )
                    + (
                        "..."
                        if len(os_agendadas) > 12
                        else ""
                    )
                )

            formulario = montar_url_formulario_follow(
                texto_limpo(follow["token"])
            )

            st.text_area(
                "Mensagem preparada",
                texto_limpo(follow["mensagem"]),
                height=120,
                key=f"msg_{follow['id']}",
            )

            col_whats, col_envio, col_form = st.columns(
                [2, 2, 2]
            )

            with col_whats:
                botao_whatsapp_web(
                    telefone,
                    texto_limpo(follow["mensagem"]),
                    str(follow["id"]),
                )

            with col_envio:
                if st.button(
                    "✅ Registrar envio",
                    key=f"envio_{follow['id']}",
                    use_container_width=True,
                    help=(
                        "Use depois de enviar a mensagem para "
                        "registrar o contato no histórico."
                    ),
                ):
                    registrar_envio_follow(
                        int(follow["id"])
                    )
                    st.success(
                        "Envio registrado."
                    )
                    st.rerun()

            with col_form:
                if formulario:
                    st.markdown(
                        f"[🔗 Abrir formulário de teste]"
                        f"({formulario})"
                    )
                else:
                    st.warning(
                        "Não foi possível gerar a URL pública."
                    )

            if follow.get("respondido_em"):
                if bool(
                    follow.get("tem_impedimento")
                ):
                    st.warning(
                        "⚠️ A oficina informou impedimento."
                    )
                else:
                    st.success(
                        "✅ Oficina confirmou sem impedimento."
                    )

    st.divider()
    exibir_respostas_follow(
        consultor,
        data_selecionada,
    )

