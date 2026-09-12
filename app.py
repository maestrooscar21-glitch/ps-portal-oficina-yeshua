
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, datetime, timedelta
from supabase import create_client

st.set_page_config(
    page_title="Portal da Oficina | YESHUA RASTREAMENTO",
    page_icon="🏢",
    layout="wide",
)

OFICINA_PORTAL = "YESHUA RASTREAMENTO"
CACHE_TTL_SEGUNDOS = 60
FUSO_BRASIL = ZoneInfo("America/Recife")

def obter_cliente_supabase():
    try:
        url = st.secrets["SUPABASE_URL"]

        # Usa o mesmo padrão do painel principal.
        if "SUPABASE_SERVICE_KEY" in st.secrets:
            key = st.secrets["SUPABASE_SERVICE_KEY"]
        elif "SUPABASE_KEY" in st.secrets:
            key = st.secrets["SUPABASE_KEY"]
        else:
            raise KeyError("Chave do Supabase não encontrada.")

    except Exception:
        st.error(
            "O portal ainda não está conectado ao Supabase. "
            "Configure SUPABASE_URL e SUPABASE_SERVICE_KEY "
            "(ou SUPABASE_KEY) nos Secrets do Streamlit."
        )
        st.stop()

    return create_client(url, key)

cliente = obter_cliente_supabase()

# Compatibilidade com o motor reaproveitado do painel principal.
# Algumas funções internas usam os nomes SUPABASE / ERRO_SUPABASE.
SUPABASE = cliente
ERRO_SUPABASE = None

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

    # Compatibilidade com a restrição histórica da tabela, criada quando
    # a fila aceitava somente revisões de improdutividade. As novas decisões
    # de OS Perdida são codificadas no campo de motivo e recuperadas pelo app.
    prefixo_auditoria_perdida = "AUDITORIA_OS_PERDIDA::"
    classificacoes_perdida = {
        "OS Perdida",
        "Executada",
        "Possível substituição de OS",
    }
    classificacao_banco = classificacao_md
    motivo_banco = motivo_md_revisado

    if classificacao_md in classificacoes_perdida:
        classificacao_banco = "Improdutiva"
        motivo_banco = prefixo_auditoria_perdida + classificacao_md

    registro = {
        "data_operacional": data_operacional,
        "chave_atendimento": chave_atendimento,
        "motivo_original": motivo_original,
        "classificacao_md": classificacao_banco,
        "motivo_md_revisado": motivo_banco,
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

    resultado["Classificacao_gerencial_MD"] = resultado.get(
        "Classificação",
        pd.Series("Improdutiva", index=resultado.index, dtype=str),
    ).apply(
        lambda classificacao: (
            "OS Perdida"
            if texto_limpo(classificacao) == "OS Perdida"
            else "Improdutiva"
        )
    )
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

        classificacao_padrao = (
            "OS Perdida"
            if texto_limpo(linha.get("Classificação", "")) == "OS Perdida"
            else "Improdutiva"
        )
        motivo = texto_limpo(
            revisao.get("motivo_md_revisado", "")
        )
        prefixo_auditoria_perdida = "AUDITORIA_OS_PERDIDA::"
        classificacao_codificada = (
            motivo[len(prefixo_auditoria_perdida):]
            if motivo.startswith(prefixo_auditoria_perdida)
            else ""
        )
        classificacao = (
            classificacao_codificada
            or texto_limpo(revisao.get("classificacao_md", ""))
            or classificacao_padrao
        )

        if classificacao_codificada:
            motivo = ""

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
        # Aviso antecipado do cliente: não houve visita/deslocamento efetivo.
        # A expressão curta cobre pequenas variações do texto padronizado do OFS,
        # sem expurgar motivos genéricos como "cliente solicitou".
        "CLIENTE INFORMOU COM ANTECEDENCIA",
    ]

    return any(
        motivo in texto
        for motivo in motivos_expurgados
    )

def motivo_problema_tecnico_veiculo(valor) -> bool:
    """Identifica somente o expurgo ligado a problema técnico no veículo."""
    texto = normalizar_texto(valor)
    if not texto:
        return False

    return any(
        motivo in texto
        for motivo in [
            "PROBLEMAS TECNICOS COM VEICULOS",
            "PROBLEMA TECNICO COM VEICULO",
            "PROBLEMAS TECNICOS COM O VEICULO",
            "PROBLEMAS TECNICOS EM VEICULOS",
            "PROBLEMA TECNICO EM VEICULO",
        ]
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



import html

import io

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

TABELA_POR_TIPO = {
    "planejado": "atividades_planejadas",
    "resultado": "atividades_resultado",
}

DATA_CORTE_NOVA_REGRA = "2026-08-08"

CACHE_TTL_SEGUNDOS = 60

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


def carregar_consolidado_portal_todos_servicos(
    datas: list[str],
) -> pd.DataFrame:
    """Concilia todas as atividades da oficina, não apenas manutenções."""
    partes = []

    for data_operacional in datas:
        planejado = carregar_base(
            "planejado",
            data_operacional,
        )
        resultado = carregar_base(
            "resultado",
            data_operacional,
        )

        if planejado.empty and resultado.empty:
            continue

        conciliacao_data = (
            conciliar_bases_portal_todos_servicos(
                planejado,
                resultado,
            )
        )

        conciliacao_data.insert(
            0,
            "Data Operacional",
            data_operacional,
        )

        partes.append(
            conciliacao_data
        )

    if not partes:
        return pd.DataFrame()

    return pd.concat(
        partes,
        ignore_index=True,
        sort=False,
    )



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
            "Prioridade": [r.get("prioridade", "Normal") for r in registros],
            "Ativa": ["Sim" if r.get("ativa", True) else "Não" for r in registros],
            "Observações": [r.get("observacoes", "") for r in registros],
            "Chave Oficina": [r.get("chave_oficina", "") for r in registros],
        }
    )

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
    resultado["Prioridade"] = resultado["Prioridade"].fillna("Normal")

    return resultado

def exigir_supabase() -> Client:
    if SUPABASE is None:
        st.error(ERRO_SUPABASE or "Supabase não conectado.")
        st.stop()

    return SUPABASE

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

def juntar_unicos(valores) -> str:
    itens = sorted(
        {
            texto_limpo(valor)
            for valor in valores
            if texto_limpo(valor)
        }
    )
    return " | ".join(itens)

def listar_bases() -> pd.DataFrame:
    registros = buscar_todos(
        "bases_importadas",
        ordem="data_operacional",
        desc=True,
    )
    return pd.DataFrame(registros)

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

def status_cancelado(valor) -> bool:
    return "CANCEL" in status_normalizado(valor)

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

def status_normalizado(valor) -> str:
    return normalizar_texto(valor)

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


def conciliar_bases_portal_todos_servicos(
    planejado: pd.DataFrame,
    resultado: pd.DataFrame,
) -> pd.DataFrame:
    """
    Regra híbrida:
    - datas anteriores a DATA_CORTE_NOVA_REGRA usam a lógica histórica;
    - a partir da data de corte, usa primeira aparição da OS para
      classificar Agendada x Extra/Encaixe.
    """
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

    if "__Ativa no Planejamento" not in planejado.columns:
        planejado["__Ativa no Planejamento"] = True

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
            Tipo_resultado=("Tipo de Atividade", "first"),
            Status_resultado=("Status da Atividade", juntar_unicos),
            Razao_improdutiva=(
                "Razão da Improdutiva",
                juntar_unicos,
            ),
            Observacao_tecnico_improdutiva=(
                "Observação do Técnico (Improdutiva)",
                juntar_unicos,
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
            Tipo_planejado=("Tipo de Atividade", "first"),
            Status_planejado=("Status da Atividade", juntar_unicos),
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

    # Auditoria de substituição de OS:
    # se a OS planejada não aparece pelo mesmo atendimento, procuramos
    # outra OS no resultado com o MESMO Ticket Jira + MESMA placa.
    # Nessa situação, não declaramos OS Perdida automaticamente.
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

        return data_operacional >= DATA_CORTE_NOVA_REGRA

    def eh_agendada_nova(linha) -> bool:
        if linha.get("_merge") == "right_only":
            return False

        if not bool(linha.get("Ativa_planejamento", False)):
            return False

        primeira = converter_data_operacional(
            linha.get("Primeira_aparicao_data", "")
        )
        data_operacional = data_referencia_linha(linha)

        if not primeira or not data_operacional:
            return False

        return primeira < data_operacional

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
        if origem_merge == "left_only" and not ativa:
            return "Retirada do agendamento"

        if (
            origem_merge in {"left_only", "both"}
            and ativa
            and status_cancelado(status_planejado)
        ):
            return "Cancelada no agendamento"

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
                f"Manutenção já estava agendada antes de {data_operacional} "
                f"(primeira aparição: {primeira}) e foi executada."
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
                "Jira + mesma placa. O caso foi retirado do OS Perdida para "
                "auditoria de possível troca/substituição de OS."
            )
        if classificacao == "OS Perdida":
            return (
                "Manutenção estava agendada antes do dia, a própria OS não "
                "apareceu no resultado e nenhuma outra OS com o mesmo Ticket "
                "Jira + mesma placa foi localizada. Classificada como "
                "OS Perdida provável."
            )
        if classificacao == "Encaixe não realizado":
            return (
                "A manutenção surgiu no próprio dia como extra/encaixe "
                "e não apareceu no resultado; não conta como OS Perdida."
            )
        if classificacao == "Retirada do agendamento":
            return (
                "A OS apareceu em fotografia anterior, mas não está mais "
                "no agendamento vigente dessa data."
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

    conciliacao["Tipo de Serviço"] = conciliacao[
        "Tipo_planejado"
    ].fillna(conciliacao["Tipo_resultado"])

    return conciliacao

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
    resultado["Prioridade"] = resultado["Prioridade"].fillna("Normal")

    return resultado

def exigir_supabase() -> Client:
    if SUPABASE is None:
        st.error(ERRO_SUPABASE or "Supabase não conectado.")
        st.stop()

    return SUPABASE

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

def juntar_unicos(valores) -> str:
    itens = sorted(
        {
            texto_limpo(valor)
            for valor in valores
            if texto_limpo(valor)
        }
    )
    return " | ".join(itens)

def listar_bases() -> pd.DataFrame:
    registros = buscar_todos(
        "bases_importadas",
        ordem="data_operacional",
        desc=True,
    )
    return pd.DataFrame(registros)

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

def status_cancelado(valor) -> bool:
    return "CANCEL" in status_normalizado(valor)

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

def status_normalizado(valor) -> str:
    return normalizar_texto(valor)

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




def calcular_indicadores(conciliacao: pd.DataFrame) -> dict:
    # Preserva a classificação original. Somente OS Perdidas com revisão
    # concluída podem assumir outra classificação nos indicadores.
    classificacao_calculo = conciliacao["Classificação"].copy()
    revisao_ativa = conciliacao.get(
        "Revisao_MD",
        pd.Series(False, index=conciliacao.index),
    ).fillna(False).astype(bool)
    classificacao_gerencial_calculo = conciliacao.get(
        "Classificacao_gerencial_MD",
        pd.Series("", index=conciliacao.index, dtype=str),
    ).fillna("")
    mascara_perdida_revisada = (
        (classificacao_calculo == "OS Perdida")
        & revisao_ativa
    )
    mapa_correcao_perdida = {
        "Executada": "Executada agendada",
        "Executada agendada": "Executada agendada",
        "Cancelada": "Cancelada",
        "Possível substituição de OS": "Possível substituição de OS",
        "OS Perdida": "OS Perdida",
    }
    classificacao_calculo.loc[mascara_perdida_revisada] = (
        classificacao_gerencial_calculo.loc[mascara_perdida_revisada]
        .map(mapa_correcao_perdida)
        .fillna("OS Perdida")
    )

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
            classificacao_calculo.isin(
                agendadas_validas
            ).sum()
        )

    agendadas_executadas = int(
        (
            classificacao_calculo
            == "Executada agendada"
        ).sum()
    )

    executadas_extras = int(
        (
            classificacao_calculo
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
            ["OS Perdida técnico", "OS Perdida cliente", "Cancelada"]
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
            classificacao_calculo
            == "Cancelada"
        ).sum()
    )

    no_show = int(
        (
            classificacao_calculo
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

    # MD: as causas expurgadas não entram nem no numerador
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
                classificacao_calculo
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




def limpar_telefone(valor) -> str:
    numeros = re.sub(r"\D", "", texto_limpo(valor))

    if numeros.startswith("0") and len(numeros) > 10:
        numeros = numeros[1:]

    if numeros and not numeros.startswith("55"):
        numeros = f"55{numeros}"

    return numeros

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


# =========================================================
# FOLLOW INTEGRADO AO PORTAL DA OFICINA
# =========================================================

MOTIVOS_IMPEDIMENTO_PORTAL = [
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


def buscar_follow_pendentes_portal() -> pd.DataFrame:
    registros = buscar_todos(
        "follow_contatos",
        ordem="data_manutencao",
        desc=False,
    )

    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros)

    if df.empty or "oficina" not in df.columns:
        return pd.DataFrame()

    df["oficina_norm"] = (
        df["oficina"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )

    return df[
        df["oficina_norm"] == OFICINA_PORTAL.upper()
    ].copy()


def carregar_resposta_follow_portal(follow_id: int) -> dict:
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


def enviar_resposta_follow_portal(
    follow: dict,
    nome_respondente: str,
    equipamentos: str,
    veiculo: str,
    capacidade: str,
    tem_impedimento: bool,
    motivos: list[str],
    os_afetadas: list[str],
    observacao: str,
    previsao: str,
) -> None:
    agora = datetime.now().astimezone().isoformat()

    cliente.table("follow_respostas").insert(
        {
            "follow_id": int(follow["id"]),
            "token": str(follow["token"]),
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

    cliente.table("follow_contatos").update(
        {
            "status": "Respondido",
            "respondido_em": agora,
            "tem_impedimento": tem_impedimento,
            "status_resposta": (
                "Com impedimento"
                if tem_impedimento
                else "Sem impedimento"
            ),
            "ultima_atualizacao": agora,
        }
    ).eq(
        "id",
        int(follow["id"]),
    ).execute()



def gerar_follows_automaticos_portal() -> dict:
    """
    Gera o Follow diretamente da tabela atividades_planejadas.

    Esta versão não depende de bases_importadas para descobrir as datas.
    Ela lê o planejamento vigente salvo no Supabase, filtra:
    - somente manutenções;
    - somente a oficina do portal;
    - somente registros ativos;
    - exclui canceladas;
    - somente hoje e datas futuras.
    """
    hoje = date.today()

    registros = buscar_todos(
        "atividades_planejadas",
        ordem="data_operacional",
        desc=False,
    )

    if not registros:
        return {
            "criados": 0,
            "atualizados": 0,
            "pendentes": 0,
            "datas_detectadas": [],
            "os_detectadas": 0,
        }

    linhas = []

    for registro in registros:
        dados = dict(registro.get("dados") or {})

        linha = {
            "data_operacional": str(
                registro.get("data_operacional") or ""
            ),
            "chave_atendimento": texto_limpo(
                registro.get("chave_atendimento", "")
            ),
            "os": texto_limpo(
                registro.get("os", "")
            ),
            "oficina": texto_limpo(
                registro.get("oficina", "")
            ),
            "tipo_atividade": texto_limpo(
                registro.get("tipo_atividade", "")
            ),
            "status_atividade": texto_limpo(
                registro.get("status_atividade", "")
            ),
            "ativa_no_planejamento": bool(
                registro.get(
                    "ativa_no_planejamento",
                    True,
                )
            ),
        }

        # Fallback para dados históricos armazenados no JSON.
        if not linha["oficina"]:
            linha["oficina"] = texto_limpo(
                dados.get("Oficina", "")
            )

        if not linha["tipo_atividade"]:
            linha["tipo_atividade"] = texto_limpo(
                dados.get("Tipo de Atividade", "")
            )

        if not linha["status_atividade"]:
            linha["status_atividade"] = texto_limpo(
                dados.get("Status da Atividade", "")
            )

        if not linha["os"]:
            linha["os"] = texto_limpo(
                dados.get("OS", "")
            )

        linhas.append(linha)

    df = pd.DataFrame(linhas)

    if df.empty:
        return {
            "criados": 0,
            "atualizados": 0,
            "pendentes": 0,
            "datas_detectadas": [],
            "os_detectadas": 0,
        }

    df["data_dt"] = pd.to_datetime(
        df["data_operacional"],
        errors="coerce",
    ).dt.date

    # Somente datas atuais/futuras.
    df = df[
        df["data_dt"].notna()
        & (df["data_dt"] >= hoje)
    ].copy()

    # Somente registros ainda vigentes no planejamento.
    df = df[
        df["ativa_no_planejamento"] == True
    ].copy()

    # Somente manutenções.
    df = df[
        df["tipo_atividade"].apply(
            lambda valor: (
                "MANUTEN" in normalizar_texto(valor)
            )
        )
    ].copy()

    # Exclui canceladas.
    df = df[
        ~df["status_atividade"].apply(
            status_cancelado
        )
    ].copy()

    # Somente a oficina deste portal.
    df = df[
        df["oficina"].apply(
            normalizar_texto
        ) == normalizar_texto(
            OFICINA_PORTAL
        )
    ].copy()

    if df.empty:
        return {
            "criados": 0,
            "atualizados": 0,
            "pendentes": 0,
            "datas_detectadas": [],
            "os_detectadas": 0,
        }

    # Evita duplicidade da mesma atividade.
    df["chave_unica"] = df.apply(
        lambda linha: (
            texto_limpo(
                linha["chave_atendimento"]
            )
            or (
                f"{linha['data_operacional']}|"
                f"{texto_limpo(linha['os'])}"
            )
        ),
        axis=1,
    )

    df = df.drop_duplicates(
        subset=["data_operacional", "chave_unica"],
        keep="last",
    )

    cadastro = carregar_oficinas()

    consultor = "Não definido"
    telefone = ""
    chave_oficina = normalizar_texto(
        OFICINA_PORTAL
    )

    if not cadastro.empty:
        cadastro_oficina = cadastro[
            cadastro["Oficina"].apply(
                normalizar_texto
            ) == normalizar_texto(
                OFICINA_PORTAL
            )
        ]

        if not cadastro_oficina.empty:
            cad = cadastro_oficina.iloc[0]

            consultor = (
                texto_limpo(
                    cad.get("Consultor", "")
                )
                or "Não definido"
            )
            telefone = texto_limpo(
                cad.get("WhatsApp", "")
            )
            chave_oficina = (
                texto_limpo(
                    cad.get("Chave Oficina", "")
                )
                or chave_oficina
            )

    existentes_raw = buscar_todos(
        "follow_contatos",
        ordem="data_manutencao",
        desc=False,
    )
    existentes = pd.DataFrame(
        existentes_raw
    )

    criados = 0
    atualizados = 0

    for data_operacional, grupo in df.groupby(
        "data_operacional"
    ):
        os_lista = sorted(
            {
                texto_limpo(v)
                for v in grupo["os"]
                if texto_limpo(v)
            }
        )

        quantidade = len(grupo)

        existente = None

        if not existentes.empty:
            mascara = (
                existentes["data_manutencao"]
                .astype(str)
                .eq(str(data_operacional))
            )

            if "oficina" in existentes.columns:
                mascara = mascara & (
                    existentes["oficina"]
                    .fillna("")
                    .astype(str)
                    .apply(normalizar_texto)
                    .eq(
                        normalizar_texto(
                            OFICINA_PORTAL
                        )
                    )
                )

            encontrados = existentes[
                mascara
            ]

            if not encontrados.empty:
                existente = (
                    encontrados
                    .sort_values("id")
                    .iloc[-1]
                    .to_dict()
                )

        agora = (
            datetime.now()
            .astimezone()
            .isoformat()
        )

        mensagem = (
            f"Olá! A {OFICINA_PORTAL} possui "
            f"{quantidade} manutenção(ões) "
            f"planejada(s) para "
            f"{pd.to_datetime(data_operacional).strftime('%d/%m/%Y')}. "
            "Acesse o Portal da Oficina e confirme o Follow."
        )

        if existente:
            payload = {
                "qtd_agendadas": quantidade,
                "os_agendadas": os_lista,
                "telefone": telefone,
                "mensagem": mensagem,
                "ultima_atualizacao": agora,
            }

            # Só reabre como pendente se ainda não houve resposta.
            if not existente.get(
                "respondido_em"
            ):
                payload["status"] = "Pendente"

            cliente.table(
                "follow_contatos"
            ).update(
                payload
            ).eq(
                "id",
                int(existente["id"]),
            ).execute()

            atualizados += 1

        else:
            cliente.table(
                "follow_contatos"
            ).insert(
                {
                    "token": str(uuid.uuid4()),
                    "data_follow": hoje.isoformat(),
                    "data_manutencao": (
                        data_operacional
                    ),
                    "chave_oficina": chave_oficina,
                    "oficina": OFICINA_PORTAL,
                    "consultor": consultor,
                    "telefone": telefone,
                    "qtd_agendadas": quantidade,
                    "os_agendadas": os_lista,
                    "mensagem": mensagem,
                    "status": "Pendente",
                    "preparado_em": agora,
                    "ultima_atualizacao": agora,
                }
            ).execute()

            criados += 1

    follows = buscar_follow_pendentes_portal()

    pendentes = 0

    if not follows.empty:
        follows["data_dt"] = pd.to_datetime(
            follows["data_manutencao"],
            errors="coerce",
        ).dt.date

        pendentes = int(
            (
                follows["data_dt"].notna()
                & (follows["data_dt"] >= hoje)
                & (follows["respondido_em"].isna() | follows["respondido_em"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "nat"]))
            ).sum()
        )

    return {
        "criados": criados,
        "atualizados": atualizados,
        "pendentes": pendentes,
        "datas_detectadas": sorted(
            df["data_operacional"]
            .astype(str)
            .unique()
            .tolist()
        ),
        "os_detectadas": len(df),
    }


def resumo_alerta_follow_portal() -> tuple[int, int]:
    """
    Retorna:
    (quantidade de datas com Follow pendente,
     quantidade total de manutenções aguardando confirmação)
    """
    follows = buscar_follow_pendentes_portal()

    if follows.empty:
        return 0, 0

    hoje = date.today()

    follows["data_dt"] = pd.to_datetime(
        follows["data_manutencao"],
        errors="coerce",
    ).dt.date

    pendentes = follows[
        follows["data_dt"].notna()
        & (follows["data_dt"] >= hoje)
        & (follows["respondido_em"].isna() | follows["respondido_em"].astype(str).str.strip().str.lower().isin(["", "nan", "none", "nat"]))
    ].copy()

    if pendentes.empty:
        return 0, 0

    return (
        len(pendentes),
        int(
            pendentes["qtd_agendadas"]
            .fillna(0)
            .astype(int)
            .sum()
        ),
    )



def carregar_respostas_os_follow(
    follow_id: int,
) -> pd.DataFrame:
    resposta = (
        cliente.table("follow_respostas_os")
        .select("*")
        .eq("follow_id", follow_id)
        .order("os_numero")
        .execute()
    )

    return pd.DataFrame(
        resposta.data or []
    )


def salvar_resposta_os_follow(
    follow: dict,
    os_numero: str,
    nome_respondente: str,
    equipamentos: str,
    veiculo: str,
    capacidade: str,
    tem_impedimento: bool,
    motivos: list[str],
    observacao: str,
    previsao: str,
) -> None:
    agora = datetime.now().astimezone().isoformat()

    payload = {
        "follow_id": int(follow["id"]),
        "os_numero": texto_limpo(os_numero),
        "nome_respondente": nome_respondente,
        "equipamentos_ok": equipamentos == "Sim",
        "veiculo_disponivel": veiculo,
        "capacidade_ok": capacidade == "Sim",
        "tem_impedimento": tem_impedimento,
        "motivos": motivos,
        "observacao": observacao,
        "previsao_solucao": previsao,
        "respondido_em": agora,
        "atualizado_em": agora,
    }

    (
        cliente.table("follow_respostas_os")
        .upsert(
            payload,
            on_conflict="follow_id,os_numero",
        )
        .execute()
    )

    atualizar_resumo_follow_por_os(
        follow
    )


def confirmar_todas_os_ok(
    follow: dict,
    os_lista: list[str],
    nome_respondente: str,
) -> None:
    agora = datetime.now().astimezone().isoformat()

    registros = []

    for os_numero in os_lista:
        registros.append(
            {
                "follow_id": int(follow["id"]),
                "os_numero": texto_limpo(os_numero),
                "nome_respondente": nome_respondente,
                "equipamentos_ok": True,
                "veiculo_disponivel": "Sim",
                "capacidade_ok": True,
                "tem_impedimento": False,
                "motivos": [],
                "observacao": "",
                "previsao_solucao": "",
                "respondido_em": agora,
                "atualizado_em": agora,
            }
        )

    if registros:
        (
            cliente.table("follow_respostas_os")
            .upsert(
                registros,
                on_conflict="follow_id,os_numero",
            )
            .execute()
        )

    atualizar_resumo_follow_por_os(
        follow
    )


def atualizar_resumo_follow_por_os(
    follow: dict,
) -> None:
    respostas = carregar_respostas_os_follow(
        int(follow["id"])
    )

    os_lista = [
        texto_limpo(v)
        for v in (follow.get("os_agendadas") or [])
        if texto_limpo(v)
    ]

    total = len(os_lista)

    if respostas.empty:
        respondidas = 0
        com_impedimento = 0
        sem_impedimento = 0
    else:
        respostas = respostas[
            respostas["os_numero"]
            .astype(str)
            .isin(os_lista)
        ].copy()

        respondidas = int(
            respostas["os_numero"]
            .astype(str)
            .nunique()
        )

        com_impedimento = int(
            respostas[
                respostas["tem_impedimento"] == True
            ]["os_numero"]
            .astype(str)
            .nunique()
        )

        sem_impedimento = max(
            respondidas - com_impedimento,
            0,
        )

    faltantes = max(
        total - respondidas,
        0,
    )

    agora = datetime.now().astimezone().isoformat()

    if total and faltantes == 0:
        status = "Respondido"

        if com_impedimento:
            status_resposta = (
                f"{com_impedimento} com impedimento · "
                f"{sem_impedimento} sem impedimento"
            )
            tem_impedimento = True
        else:
            status_resposta = "Sem impedimento"
            tem_impedimento = False

        respondido_em = agora

    elif respondidas > 0:
        status = "Parcial"
        status_resposta = (
            f"{respondidas}/{total} OS respondidas"
        )
        tem_impedimento = (
            com_impedimento > 0
        )
        respondido_em = None

    else:
        status = "Pendente"
        status_resposta = "Pendente"
        tem_impedimento = False
        respondido_em = None

    cliente.table("follow_contatos").update(
        {
            "status": status,
            "status_resposta": status_resposta,
            "tem_impedimento": tem_impedimento,
            "respondido_em": respondido_em,
            "ultima_atualizacao": agora,
        }
    ).eq(
        "id",
        int(follow["id"]),
    ).execute()

    # Compatibilidade com o painel de gestão atual:
    # sempre que houver respostas individuais, gravamos também um resumo
    # na tabela legada follow_respostas.
    if respondidas > 0:
        motivos = []
        os_afetadas = []
        observacoes = []

        if not respostas.empty:
            for _, item in respostas.iterrows():
                if bool(
                    item.get(
                        "tem_impedimento",
                        False,
                    )
                ):
                    os_afetadas.append(
                        texto_limpo(
                            item.get(
                                "os_numero",
                                "",
                            )
                        )
                    )

                motivos_item = (
                    item.get("motivos")
                    or []
                )
                motivos.extend(
                    [
                        texto_limpo(v)
                        for v in motivos_item
                        if texto_limpo(v)
                    ]
                )

                obs = texto_limpo(
                    item.get(
                        "observacao",
                        "",
                    )
                )
                if obs:
                    observacoes.append(
                        f"{texto_limpo(item.get('os_numero',''))}: {obs}"
                    )

        cliente.table(
            "follow_respostas"
        ).insert(
            {
                "follow_id": int(follow["id"]),
                "token": str(follow["token"]),
                "nome_respondente": "Resposta individual por OS",
                "equipamentos_ok": bool(
                    respostas["equipamentos_ok"]
                    .fillna(False)
                    .all()
                )
                if not respostas.empty
                else False,
                "veiculo_disponivel": (
                    "Sim"
                    if (
                        not respostas.empty
                        and (
                            respostas["veiculo_disponivel"]
                            .fillna("")
                            == "Sim"
                        ).all()
                    )
                    else "Parcial"
                ),
                "capacidade_ok": bool(
                    respostas["capacidade_ok"]
                    .fillna(False)
                    .all()
                )
                if not respostas.empty
                else False,
                "tem_impedimento": (
                    com_impedimento > 0
                ),
                "motivos": sorted(
                    set(motivos)
                ),
                "os_afetadas": sorted(
                    set(
                        [
                            v
                            for v in os_afetadas
                            if v
                        ]
                    )
                ),
                "observacao": " | ".join(
                    observacoes
                ),
                "previsao_solucao": "",
                "respondido_em": agora,
            }
        ).execute()



def exibir_follow_portal() -> None:
    st.markdown("### 📞 Follow de Manutenções")
    st.caption(
        "Cada OS deve ser confirmada individualmente. "
        "Se todas estiverem OK, use o botão de confirmação em lote."
    )

    follows = buscar_follow_pendentes_portal()

    if follows.empty:
        st.success(
            "Você não possui Follow disponível no momento."
        )
        return

    hoje = date.today()

    follows["data_dt"] = pd.to_datetime(
        follows["data_manutencao"],
        errors="coerce",
    ).dt.date

    pendentes = follows[
        follows["data_dt"].notna()
        & (follows["data_dt"] >= hoje)
    ].copy()

    if pendentes.empty:
        st.info(
            "Não há Follow para datas atuais ou futuras."
        )
        return

    st.markdown("#### Pendências para confirmação")

    planejamento = carregar_planejamento_futuro_portal()

    for _, row in pendentes.sort_values(
        "data_dt"
    ).iterrows():
        follow = row.to_dict()

        data_txt = pd.to_datetime(
            follow["data_manutencao"]
        ).strftime("%d/%m/%Y")

        os_lista = [
            texto_limpo(x)
            for x in (
                follow.get("os_agendadas")
                or []
            )
            if texto_limpo(x)
        ]

        respostas = carregar_respostas_os_follow(
            int(follow["id"])
        )

        respondidas = set()

        if not respostas.empty:
            respondidas = set(
                respostas["os_numero"]
                .fillna("")
                .astype(str)
            )

        total = len(os_lista)
        total_respondidas = len(
            [
                os_numero
                for os_numero in os_lista
                if os_numero in respondidas
            ]
        )

        total_impedimentos = 0
        if not respostas.empty:
            total_impedimentos = int(
                respostas[
                    (
                        respostas["os_numero"]
                        .astype(str)
                        .isin(os_lista)
                    )
                    & (
                        respostas[
                            "tem_impedimento"
                        ] == True
                    )
                ]["os_numero"]
                .nunique()
            )

        sem_resposta = max(
            total - total_respondidas,
            0,
        )

        with st.container(border=True):
            st.markdown(
                f"### Follow • {data_txt}"
            )

            r1, r2, r3, r4 = st.columns(
                4
            )
            r1.metric(
                "OS previstas",
                total,
            )
            r2.metric(
                "Confirmadas",
                total_respondidas,
            )
            r3.metric(
                "Com impedimento",
                total_impedimentos,
            )
            r4.metric(
                "Sem resposta",
                sem_resposta,
            )

            if sem_resposta == 0 and total > 0:
                if total_impedimentos:
                    st.warning(
                        "⚠️ Follow concluído com impedimento(s)."
                    )
                else:
                    st.success(
                        "✅ Follow concluído. Todas as OS estão confirmadas."
                    )
            else:
                st.warning(
                    f"⚠️ Ainda existem {sem_resposta} OS "
                    "aguardando confirmação."
                )

            st.markdown(
                "#### Confirmação rápida"
            )

            nome_lote = st.text_input(
                "Seu nome para confirmação em lote",
                key=f"nome_lote_{follow['id']}",
            )

            if st.button(
                "✅ Confirmar todas as OS como OK",
                key=f"confirmar_todas_{follow['id']}",
                use_container_width=True,
            ):
                if not nome_lote.strip():
                    st.error(
                        "Informe seu nome antes de confirmar."
                    )
                elif not os_lista:
                    st.error(
                        "Nenhuma OS disponível para confirmação."
                    )
                else:
                    confirmar_todas_os_ok(
                        follow=follow,
                        os_lista=os_lista,
                        nome_respondente=nome_lote,
                    )
                    st.success(
                        "Todas as OS foram confirmadas como OK."
                    )
                    st.rerun()

            st.divider()
            st.markdown(
                "#### Resposta individual por OS"
            )

            for os_numero in os_lista:
                detalhe_os = pd.DataFrame()

                if not planejamento.empty:
                    detalhe_os = planejamento[
                        planejamento["OS"]
                        .astype(str)
                        == str(os_numero)
                    ].copy()

                cliente_nome = ""
                local = ""
                tipo = "Manutenção"
                status_os = "Pendente"

                if not detalhe_os.empty:
                    item = detalhe_os.iloc[0]
                    cliente_nome = texto_limpo(
                        item.get("Cliente", "")
                    )
                    local = texto_limpo(
                        item.get("Local", "")
                    )
                    tipo = (
                        texto_limpo(
                            item.get(
                                "Tipo de Serviço",
                                "",
                            )
                        )
                        or tipo
                    )
                    status_os = (
                        texto_limpo(
                            item.get(
                                "Status",
                                "",
                            )
                        )
                        or status_os
                    )

                resposta_os = {}

                if not respostas.empty:
                    achado = respostas[
                        respostas["os_numero"]
                        .astype(str)
                        == str(os_numero)
                    ]

                    if not achado.empty:
                        resposta_os = (
                            achado.iloc[-1]
                            .to_dict()
                        )

                respondida = bool(
                    resposta_os
                )

                titulo_status = (
                    "⚠️ Com impedimento"
                    if (
                        respondida
                        and bool(
                            resposta_os.get(
                                "tem_impedimento",
                                False,
                            )
                        )
                    )
                    else (
                        "✅ Tudo OK"
                        if respondida
                        else "🔴 Aguardando resposta"
                    )
                )

                with st.expander(
                    f"{os_numero} • {titulo_status}",
                    expanded=not respondida,
                ):
                    d1, d2 = st.columns(2)

                    with d1:
                        st.write(
                            f"**Cliente:** "
                            f"{cliente_nome or 'Não informado'}"
                        )
                        st.write(
                            f"**Tipo:** {tipo}"
                        )

                    with d2:
                        st.write(
                            f"**Local:** "
                            f"{local or 'Não informado'}"
                        )
                        st.write(
                            f"**Status OFS:** {status_os}"
                        )

                    if respondida:
                        if bool(
                            resposta_os.get(
                                "tem_impedimento",
                                False,
                            )
                        ):
                            st.warning(
                                "Esta OS foi confirmada com impedimento."
                            )
                        else:
                            st.success(
                                "Esta OS foi confirmada sem impedimento."
                            )

                        respondente = texto_limpo(
                            resposta_os.get(
                                "nome_respondente",
                                "",
                            )
                        )
                        if respondente:
                            st.caption(
                                f"Respondido por {respondente}"
                            )

                        motivos_resp = (
                            resposta_os.get("motivos")
                            or []
                        )
                        if motivos_resp:
                            st.write(
                                "**Motivo(s):** "
                                + " | ".join(
                                    map(
                                        str,
                                        motivos_resp,
                                    )
                                )
                            )

                        obs_resp = texto_limpo(
                            resposta_os.get(
                                "observacao",
                                "",
                            )
                        )
                        if obs_resp:
                            st.write(
                                f"**Observação:** "
                                f"{obs_resp}"
                            )

                        if st.button(
                            "✏️ Alterar resposta desta OS",
                            key=(
                                f"editar_os_"
                                f"{follow['id']}_"
                                f"{os_numero}"
                            ),
                        ):
                            st.session_state[
                                f"editar_follow_os_"
                                f"{follow['id']}_"
                                f"{os_numero}"
                            ] = True
                            st.rerun()

                    editar = (
                        not respondida
                        or st.session_state.get(
                            f"editar_follow_os_"
                            f"{follow['id']}_"
                            f"{os_numero}",
                            False,
                        )
                    )

                    if editar:
                        with st.form(
                            f"form_os_"
                            f"{follow['id']}_"
                            f"{os_numero}"
                        ):
                            nome = st.text_input(
                                "Seu nome",
                                value=texto_limpo(
                                    resposta_os.get(
                                        "nome_respondente",
                                        "",
                                    )
                                )
                                if respondida
                                else "",
                            )

                            situacao = st.radio(
                                "Situação desta OS",
                                [
                                    "✅ Tudo OK",
                                    "⚠️ Tenho impedimento",
                                ],
                                index=(
                                    1
                                    if (
                                        respondida
                                        and bool(
                                            resposta_os.get(
                                                "tem_impedimento",
                                                False,
                                            )
                                        )
                                    )
                                    else 0
                                ),
                                horizontal=True,
                            )

                            tem_impedimento = (
                                situacao
                                == "⚠️ Tenho impedimento"
                            )

                            equipamentos = st.radio(
                                "Equipamentos/ferramentas disponíveis?",
                                ["Sim", "Não"],
                                horizontal=True,
                                index=(
                                    0
                                    if (
                                        not respondida
                                        or bool(
                                            resposta_os.get(
                                                "equipamentos_ok",
                                                True,
                                            )
                                        )
                                    )
                                    else 1
                                ),
                            )

                            veiculo_opcoes = [
                                "Sim",
                                "Não",
                                "Não sei",
                            ]
                            veiculo_atual = texto_limpo(
                                resposta_os.get(
                                    "veiculo_disponivel",
                                    "Sim",
                                )
                            )
                            if (
                                veiculo_atual
                                not in veiculo_opcoes
                            ):
                                veiculo_atual = "Sim"

                            veiculo = st.radio(
                                "Veículo disponível?",
                                veiculo_opcoes,
                                horizontal=True,
                                index=veiculo_opcoes.index(
                                    veiculo_atual
                                ),
                            )

                            capacidade = st.radio(
                                "Capacidade/técnico disponível?",
                                ["Sim", "Não"],
                                horizontal=True,
                                index=(
                                    0
                                    if (
                                        not respondida
                                        or bool(
                                            resposta_os.get(
                                                "capacidade_ok",
                                                True,
                                            )
                                        )
                                    )
                                    else 1
                                ),
                            )

                            motivos = []
                            observacao = ""
                            previsao = ""

                            if tem_impedimento:
                                motivos = st.multiselect(
                                    "Qual(is) o(s) impedimento(s)?",
                                    MOTIVOS_IMPEDIMENTO_PORTAL,
                                    default=(
                                        resposta_os.get(
                                            "motivos"
                                        )
                                        or []
                                    )
                                    if respondida
                                    else [],
                                )

                                observacao = st.text_area(
                                    "Explique o impedimento",
                                    value=texto_limpo(
                                        resposta_os.get(
                                            "observacao",
                                            "",
                                        )
                                    )
                                    if respondida
                                    else "",
                                )

                                previsao = st.text_input(
                                    "Previsão de solução",
                                    value=texto_limpo(
                                        resposta_os.get(
                                            "previsao_solucao",
                                            "",
                                        )
                                    )
                                    if respondida
                                    else "",
                                )
                            else:
                                observacao = st.text_area(
                                    "Observação (opcional)",
                                    value=texto_limpo(
                                        resposta_os.get(
                                            "observacao",
                                            "",
                                        )
                                    )
                                    if respondida
                                    else "",
                                )

                            salvar = (
                                st.form_submit_button(
                                    "💾 Salvar resposta desta OS",
                                    type="primary",
                                    use_container_width=True,
                                )
                            )

                        if salvar:
                            if not nome.strip():
                                st.error(
                                    "Informe seu nome."
                                )
                            elif (
                                tem_impedimento
                                and not motivos
                            ):
                                st.error(
                                    "Selecione ao menos um motivo."
                                )
                            else:
                                salvar_resposta_os_follow(
                                    follow=follow,
                                    os_numero=os_numero,
                                    nome_respondente=nome,
                                    equipamentos=equipamentos,
                                    veiculo=veiculo,
                                    capacidade=capacidade,
                                    tem_impedimento=tem_impedimento,
                                    motivos=motivos,
                                    observacao=observacao,
                                    previsao=previsao,
                                )

                                st.session_state.pop(
                                    f"editar_follow_os_"
                                    f"{follow['id']}_"
                                    f"{os_numero}",
                                    None,
                                )

                                st.success(
                                    f"Resposta da OS {os_numero} salva."
                                )
                                st.rerun()

    st.divider()
    st.markdown(
        "#### Histórico recente de Follow"
    )

    historico = follows[
        follows["status"]
        .fillna("")
        .astype(str)
        .isin(
            [
                "Parcial",
                "Respondido",
            ]
        )
    ].copy()

    if historico.empty:
        st.caption(
            "Nenhuma resposta registrada ainda."
        )
    else:
        cols = [
            c
            for c in [
                "data_manutencao",
                "qtd_agendadas",
                "status",
                "status_resposta",
                "ultima_atualizacao",
            ]
            if c in historico.columns
        ]

        st.dataframe(
            historico[cols]
            .sort_values(
                "data_manutencao",
                ascending=False,
            ),
            use_container_width=True,
            hide_index=True,
        )

def calcular_indicadores_portal_todos_servicos(
    conciliacao: pd.DataFrame,
) -> dict:
    classes = conciliacao[
        "Classificação"
    ].fillna("").astype(str)

    planejadas_validas = {
        "Executada agendada",
        "Improdutiva agendada",
        "Cancelada",
        "OS Perdida",
        "Status intermediário agendado",
    }

    planejadas = int(
        classes.isin(
            planejadas_validas
        ).sum()
    )

    executadas_agendadas = int(
        (
            classes
            == "Executada agendada"
        ).sum()
    )

    executadas_extras = int(
        (
            classes
            == "Executada extra"
        ).sum()
    )

    improdutivas_agendadas = int(
        (
            classes
            == "Improdutiva agendada"
        ).sum()
    )

    improdutivas_extras = int(
        (
            classes
            == "Improdutiva extra"
        ).sum()
    )

    no_show = int(
        (
            classes
            == "OS Perdida"
        ).sum()
    )

    canceladas = int(
        (
            classes
            == "Cancelada"
        ).sum()
    )

    base_executavel = max(
        planejadas - canceladas,
        0,
    )

    indice_execucao = (
        executadas_agendadas
        / base_executavel
        * 100
        if base_executavel
        else 0.0
    )

    perdas_agendadas = (
        improdutivas_agendadas
        + no_show
    )

    indice_perda = (
        perdas_agendadas
        / base_executavel
        * 100
        if base_executavel
        else 0.0
    )

    if (
        indice_execucao >= 90
        and indice_perda <= 10
    ):
        nivel = "Excelente"
        simbolo = "🟢"
        mensagem = (
            "Ótimo aproveitamento do planejamento. "
            "Mantenha o ritmo e continue prevenindo perdas."
        )
    elif (
        indice_execucao >= 75
        and indice_perda <= 20
    ):
        nivel = "Atenção"
        simbolo = "🟡"
        mensagem = (
            "O desempenho está razoável, mas existem perdas "
            "que podem ser reduzidas."
        )
    else:
        nivel = "Crítico"
        simbolo = "🔴"
        mensagem = (
            "Há perda relevante do planejamento. "
            "Priorize OS Perdida, improdutivas e pendências futuras."
        )

    return {
        "Planejadas": planejadas,
        "Executadas agendadas": executadas_agendadas,
        "Executadas extras": executadas_extras,
        "Executadas totais": (
            executadas_agendadas
            + executadas_extras
        ),
        "Improdutivas": (
            improdutivas_agendadas
            + improdutivas_extras
        ),
        "Improdutivas agendadas": improdutivas_agendadas,
        "Improdutivas extras": improdutivas_extras,
        "OS Perdida": no_show,
        "Canceladas": canceladas,
        "Índice de execução": indice_execucao,
        "Índice de perda": indice_perda,
        "Nível": nivel,
        "Símbolo": simbolo,
        "Mensagem": mensagem,
    }


def carregar_planejamento_futuro_portal() -> pd.DataFrame:
    """
    Planejamento futuro de TODOS os tipos de serviço da oficina.
    Follow continua sendo exclusivo para manutenções.
    """
    registros = buscar_todos(
        "atividades_planejadas",
        ordem="data_operacional",
        desc=False,
    )

    if not registros:
        return pd.DataFrame()

    linhas = []

    for registro in registros:
        dados_json = dict(
            registro.get("dados")
            or {}
        )

        cliente_nome = (
            texto_limpo(
                registro.get("cliente", "")
            )
            or texto_limpo(
                dados_json.get("Cliente", "")
            )
        )

        local_servico = (
            texto_limpo(
                registro.get("cidade", "")
            )
            or texto_limpo(
                dados_json.get("Cidade", "")
            )
            or texto_limpo(
                dados_json.get("Local", "")
            )
            or texto_limpo(
                dados_json.get("Localidade", "")
            )
        )

        linha = {
            "Data": str(
                registro.get(
                    "data_operacional"
                )
                or ""
            ),
            "OS": texto_limpo(
                registro.get("os", "")
            )
            or texto_limpo(
                dados_json.get("OS", "")
            ),
            "Cliente": cliente_nome,
            "Local": local_servico,
            "Oficina": texto_limpo(
                registro.get("oficina", "")
            )
            or texto_limpo(
                dados_json.get("Oficina", "")
            ),
            "Tipo de Serviço": texto_limpo(
                registro.get(
                    "tipo_atividade",
                    "",
                )
            )
            or texto_limpo(
                dados_json.get(
                    "Tipo de Atividade",
                    "",
                )
            ),
            "Status": texto_limpo(
                registro.get(
                    "status_atividade",
                    "",
                )
            )
            or texto_limpo(
                dados_json.get(
                    "Status da Atividade",
                    "",
                )
            ),
            "Ativa": bool(
                registro.get(
                    "ativa_no_planejamento",
                    True,
                )
            ),
        }

        linhas.append(linha)

    df = pd.DataFrame(
        linhas
    )

    if df.empty:
        return df

    df["Data_dt"] = pd.to_datetime(
        df["Data"],
        errors="coerce",
    ).dt.date

    hoje = date.today()

    df = df[
        df["Data_dt"].notna()
        & (df["Data_dt"] >= hoje)
        & (df["Ativa"] == True)
    ].copy()

    df = df[
        df["Oficina"].apply(
            normalizar_texto
        )
        == normalizar_texto(
            OFICINA_PORTAL
        )
    ].copy()

    df = df[
        ~df["Status"].apply(
            status_cancelado
        )
    ].copy()

    return df





# =========================================================
# DETALHAMENTO CLICÁVEL DOS INDICADORES DO PORTAL
# =========================================================

def definir_detalhe_portal(filtro: str) -> None:
    st.session_state["portal_detalhe_ativo"] = filtro


def limpar_detalhe_portal() -> None:
    st.session_state["portal_detalhe_ativo"] = None


def filtrar_detalhe_portal(base: pd.DataFrame, filtro: str) -> pd.DataFrame:
    classes = base["Classificação"].fillna("").astype(str)

    mapa = {
        "Planejados": [
            "Executada agendada",
            "Improdutiva agendada",
            "Cancelada",
            "OS Perdida",
            "Status intermediário agendado",
        ],
        "Executados": [
            "Executada agendada",
            "Executada extra",
        ],
        "Não concluídos": [
            "Improdutiva agendada",
            "Improdutiva extra",
        ],
        "OS Perdida": ["OS Perdida"],
        "Cancelados": ["Cancelada"],
        "Execuções extras": ["Executada extra"],
    }

    classes_filtro = mapa.get(filtro, [])
    return base[classes.isin(classes_filtro)].copy()


def exibir_card_portal(coluna, titulo: str, valor, filtro: str | None = None) -> None:
    coluna.metric(titulo, valor)

    if filtro:
        coluna.button(
            "🔎 Ver OS",
            key="portal_ver_" + normalizar_texto(filtro).lower().replace(" ", "_"),
            on_click=definir_detalhe_portal,
            args=(filtro,),
            use_container_width=True,
        )


def exibir_detalhe_portal(base: pd.DataFrame) -> None:
    filtro = st.session_state.get("portal_detalhe_ativo")
    if not filtro:
        return

    detalhe = filtrar_detalhe_portal(base, filtro)

    st.markdown("---")
    st.subheader(f"🔎 Conferência das OS — {filtro}")
    st.caption(
        f"Foram encontrados {len(detalhe)} atendimento(s) "
        "da oficina no período selecionado."
    )

    colunas = [
        "Data Operacional",
        "Tipo de Serviço",
        "Classificação",
        "Ticket",
        "Placa",
        "OS_planejada",
        "OS_resultado",
        "Status_planejado",
        "Status_resultado",
        "Razao_improdutiva",
        "Observacao_tecnico_improdutiva",
        "Motivo da Classificação",
    ]
    colunas = [c for c in colunas if c in detalhe.columns]

    exibicao = detalhe[colunas].copy().rename(
        columns={
            "OS_planejada": "OS planejada",
            "OS_resultado": "OS resultado",
            "Status_planejado": "Status planejado",
            "Status_resultado": "Status resultado",
            "Razao_improdutiva": "Razão da Improdutiva",
            "Observacao_tecnico_improdutiva": "Observação do Técnico",
        }
    )

    st.dataframe(
        exibicao,
        use_container_width=True,
        hide_index=True,
        height=500,
    )

    if st.button(
        "✖ Fechar",
        key="portal_fechar_detalhe",
    ):
        limpar_detalhe_portal()
        st.rerun()


# =========================================================
# PORTAL YESHUA — MVP 1.0 (SOMENTE LEITURA)
# =========================================================

st.title("🏢 YESHUA RASTREAMENTO")
st.caption("Portal Operacional • Planejamento, execução e Follow")

st.info(
    "Portal piloto com Follow automático. As manutenções planejadas importadas "
    "pela gestão aparecem automaticamente para confirmação da oficina."
)

try:
    bases = listar_bases()
except Exception as exc:
    st.error(f"Não foi possível consultar as bases operacionais: {exc}")
    st.stop()

if bases is None or bases.empty:
    st.warning("Ainda não existem bases operacionais disponíveis.")
    st.stop()


# Gera/atualiza automaticamente as pendências de Follow a partir
# das manutenções planejadas da própria oficina.
resultado_follow_automatico = {
    "criados": 0,
    "atualizados": 0,
    "pendentes": 0,
    "datas_detectadas": [],
    "os_detectadas": 0,
}

try:
    resultado_follow_automatico = (
        gerar_follows_automaticos_portal()
    )
except Exception as erro_follow:
    st.warning(
        "Os indicadores estão disponíveis, mas não foi possível "
        f"atualizar o Follow automático neste momento: {erro_follow}"
    )

datas_planejado = set(
    bases.loc[bases["tipo"] == "planejado", "data_operacional"].astype(str)
)
datas_resultado = set(
    bases.loc[bases["tipo"] == "resultado", "data_operacional"].astype(str)
)
datas_completas = sorted(datas_planejado & datas_resultado)

if not datas_completas:
    st.warning("Ainda não há datas com Planejado + Resultado disponíveis.")
    st.stop()

datas_dt = pd.to_datetime(pd.Series(datas_completas), errors="coerce").dropna()
data_max = datas_dt.max().date()
data_min = datas_dt.min().date()

inicio_padrao = max(data_min, data_max.replace(day=1))

with st.sidebar:
    st.header("Meu desempenho")
    periodo = st.date_input(
        "Período",
        value=(inicio_padrao, data_max),
        min_value=data_min,
        max_value=data_max,
        key="portal_periodo",
    )
    st.caption("Oficina vinculada")
    st.success(OFICINA_PORTAL)
    st.divider()
    st.caption("Portal da Oficina • v1.5.0 — alinhado ao Gestão 2.9.4")

if isinstance(periodo, (tuple, list)) and len(periodo) == 2:
    inicio, fim = periodo
else:
    inicio = fim = periodo[0] if isinstance(periodo, (tuple, list)) else periodo

datas_periodo = [
    d for d in datas_completas
    if inicio <= pd.to_datetime(d).date() <= fim
]

if not datas_periodo:
    st.info("Não existem bases completas no período selecionado.")
    st.stop()

with st.spinner("Atualizando indicadores da oficina..."):
    consolidado = carregar_consolidado_portal_todos_servicos(datas_periodo)
    cadastro = carregar_oficinas()
    if not cadastro.empty:
        consolidado = enriquecer_com_cadastro(consolidado, cadastro)

if consolidado.empty:
    st.info("Não há atendimentos consolidados nesse período.")
    st.stop()

col_oficina = "Oficina"
if col_oficina not in consolidado.columns:
    st.error("A base consolidada não contém a coluna de Oficina.")
    st.stop()

dados = consolidado[
    consolidado[col_oficina].fillna("").astype(str).str.strip().str.upper()
    == OFICINA_PORTAL.upper()
].copy()

# Série de classificações usada nas análises e abas do portal.
classes = dados["Classificação"].fillna("").astype(str)

if dados.empty:
    st.warning(
        "Nenhum atendimento da YESHUA RASTREAMENTO foi localizado no período. "
        "Confira se o nome da oficina no cadastro/base está exatamente vinculado."
    )
    st.stop()

# Indicadores do Portal: todos os tipos de serviço.
indicadores = calcular_indicadores_portal_todos_servicos(
    dados
)

planejadas = indicadores["Planejadas"]
executadas_ag = indicadores["Executadas agendadas"]
executadas_extra = indicadores["Executadas extras"]
executadas = indicadores["Executadas totais"]
improdutivas = indicadores["Improdutivas"]
improd_ag = indicadores["Improdutivas agendadas"]
improd_extra = indicadores["Improdutivas extras"]
no_show = indicadores["OS Perdida"]
canceladas = indicadores["Canceladas"]
indice_execucao = indicadores["Índice de execução"]
indice_perda = indicadores["Índice de perda"]

st.subheader(
    f"Seu desempenho • "
    f"{inicio.strftime('%d/%m/%Y')} a "
    f"{fim.strftime('%d/%m/%Y')}"
)

st.markdown(
    f"### {indicadores['Símbolo']} "
    f"Termômetro operacional: **{indicadores['Nível']}**"
)
st.caption(
    indicadores["Mensagem"]
)

c1, c2, c3, c4 = st.columns(4)

exibir_card_portal(c1, "Serviços planejados", planejadas, "Planejados")
exibir_card_portal(c2, "Executados", executadas, "Executados")
exibir_card_portal(c3, "Não concluídos", improdutivas, "Não concluídos")
exibir_card_portal(c4, "OS Perdida", no_show, "OS Perdida")

c5, c6, c7, c8 = st.columns(4)

exibir_card_portal(c5, "Cancelados", canceladas, "Cancelados")
exibir_card_portal(c6, "Execuções extras", executadas_extra, "Execuções extras")
exibir_card_portal(c7, "Índice de execução", f"{indice_execucao:.1f}%")
exibir_card_portal(c8, "Índice de perda", f"{indice_perda:.1f}%")

st.caption(
    "Índice de execução = executados do planejamento ÷ "
    "(planejados − cancelados). "
    "Índice de perda = improdutivas agendadas + OS Perdida ÷ "
    "(planejados − cancelados)."
)

# Indicadores oficiais de manutenção — mesma regra do Painel de Gestão v2.9.4.
# A visão operacional acima continua incluindo todos os tipos de serviço.
try:
    consolidado_manutencao = carregar_consolidado(datas_periodo)
    if not consolidado_manutencao.empty:
        dados_manutencao = consolidado_manutencao[
            consolidado_manutencao["Oficina"].fillna("").astype(str).str.strip().str.upper()
            == OFICINA_PORTAL.upper()
        ].copy()
    else:
        dados_manutencao = pd.DataFrame()
except Exception as exc:
    dados_manutencao = pd.DataFrame()
    st.warning(f"Não foi possível calcular MCI/MD neste momento: {exc}")

if not dados_manutencao.empty:
    ind_gestao = calcular_indicadores(dados_manutencao)
    st.markdown("### 🎯 Indicadores de manutenção — regra Gestão 2.9.4")
    g1, g2, g3, g4, g5 = st.columns(5)
    g1.metric("Planejadas", ind_gestao["Planejadas"])
    g2.metric("Executadas", ind_gestao["Executadas planejadas"] + ind_gestao["Executadas extras"])
    g3.metric("OS Perdidas", ind_gestao["OS Perdidas"])
    g4.metric("MCI — Execução", f'{ind_gestao["MCI"]:.1f}%')
    g5.metric("MD — Improdutividade", f'{ind_gestao["MD"]:.1f}%')

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Improdutivas totais", ind_gestao["Improdutivas"])
    a2.metric("Consideradas na MD", ind_gestao["Improdutivas consideradas MD"])
    a3.metric("Expurgadas da MD", ind_gestao["Improdutivas expurgadas MD"])
    a4.metric("Reclassificadas fora da MD", ind_gestao["Improdutivas reclassificadas fora da MD"])

    st.caption(
        "MCI = (executadas agendadas + executadas extras) ÷ planejadas elegíveis. "
        "MD = improdutivas consideradas ÷ (executadas totais + improdutivas consideradas). "
        "Os expurgos e as revisões gerenciais seguem a mesma regra do Painel de Gestão 2.9.4."
    )



exibir_detalhe_portal(dados)

# Planejamento futuro de todos os tipos de atividade.
planejamento_futuro = (
    carregar_planejamento_futuro_portal()
)

if not planejamento_futuro.empty:
    hoje = date.today()
    fim_semana = hoje + timedelta(
        days=6
    )

    proximos_7 = planejamento_futuro[
        planejamento_futuro["Data_dt"]
        <= fim_semana
    ].copy()

    if not proximos_7.empty:
        st.info(
            f"📅 Você possui **{len(proximos_7)} serviço(s)** "
            "planejado(s) para os próximos 7 dias. "
            "O objetivo é converter o máximo possível desse planejamento."
        )

        with st.expander(
            "Ver planejamento dos próximos 7 dias"
        ):
            st.caption(
                "Cada OS aparece separadamente para facilitar "
                "a análise de cada atendimento."
            )

            colunas_planejamento = [
                "Data",
                "OS",
                "Cliente",
                "Local",
                "Tipo de Serviço",
                "Status",
            ]

            colunas_planejamento = [
                coluna
                for coluna in colunas_planejamento
                if coluna in proximos_7.columns
            ]

            st.dataframe(
                proximos_7[
                    colunas_planejamento
                ].sort_values(
                    ["Data", "OS"]
                ),
                use_container_width=True,
                hide_index=True,
                height=420,
            )

st.divider()

qtd_follows_pendentes, qtd_os_follow_pendentes = (
    resumo_alerta_follow_portal()
)

rotulo_follow = (
    f"🔴 Follow ({qtd_follows_pendentes})"
    if qtd_follows_pendentes
    else "✅ Follow"
)

aba_resumo, aba_improd, aba_noshow, aba_os, aba_follow = st.tabs([
    "📊 Resumo",
    "🔴 Não concluídos",
    "🚫 OS Perdida",
    "🔎 Serviços",
    rotulo_follow,
])


if qtd_follows_pendentes:
    st.warning(
        f"⚠️ Você possui {qtd_os_follow_pendentes} manutenção(ões) "
        f"em {qtd_follows_pendentes} data(s) aguardando confirmação "
        "de Follow. Acesse a aba Follow e responda."
    )

datas_planejamento_detectadas = (
    resultado_follow_automatico.get(
        "datas_detectadas",
        [],
    )
)

if datas_planejamento_detectadas:
    datas_formatadas = ", ".join(
        pd.to_datetime(data).strftime("%d/%m/%Y")
        for data in datas_planejamento_detectadas
    )

    st.caption(
        "Planejamento futuro detectado para a oficina: "
        f"**{datas_formatadas}** · "
        f"{resultado_follow_automatico.get('os_detectadas', 0)} "
        "manutenção(ões) vigente(s)."
    )

with aba_resumo:
    st.markdown("### Serviços por tipo")

    if "Tipo de Serviço" in dados.columns:
        tipos_resumo = (
            dados[
                "Tipo de Serviço"
            ]
            .fillna("Não informado")
            .astype(str)
            .value_counts()
            .rename_axis(
                "Tipo de Serviço"
            )
            .reset_index(
                name="Quantidade"
            )
        )

        st.dataframe(
            tipos_resumo,
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("### Evolução diária")
    diario = (
        dados.groupby(["Data Operacional", "Classificação"])
        .size()
        .reset_index(name="Quantidade")
    )
    fig = px.bar(
        diario,
        x="Data Operacional",
        y="Quantidade",
        color="Classificação",
        barmode="stack",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Distribuição dos resultados")
    resumo = (
        classes.value_counts()
        .rename_axis("Classificação")
        .reset_index(name="Quantidade")
    )
    st.dataframe(resumo, use_container_width=True, hide_index=True)

with aba_improd:
    imp = dados[
        classes.isin(["Improdutiva agendada", "Improdutiva extra"])
    ].copy()

    st.markdown(
        f"### {len(imp)} serviço(s) não concluído(s) no período "
        f"• {improd_ag} agendada(s) • {improd_extra} extra(s)"
    )

    if imp.empty:
        st.success("Nenhuma improdutiva encontrada no período.")
    else:
        motivo_col = "Razao_improdutiva"
        obs_col = "Observacao_tecnico_improdutiva"

        if motivo_col in imp.columns:
            motivos = (
                imp[motivo_col]
                .fillna("Não informado")
                .astype(str)
                .value_counts()
                .rename_axis("Motivo")
                .reset_index(name="Quantidade")
            )
            fig_m = px.bar(
                motivos.head(10),
                x="Quantidade",
                y="Motivo",
                orientation="h",
                text="Quantidade",
            )
            st.plotly_chart(fig_m, use_container_width=True)

        cols = [
            c for c in [
                "Data Operacional", "Tipo de Serviço", "Classificação",
                "Ticket", "Placa", "OS_planejada", "OS_resultado",
                motivo_col, obs_col
            ] if c in imp.columns
        ]
        st.dataframe(imp[cols], use_container_width=True, hide_index=True)

with aba_noshow:
    ns = dados[classes == "OS Perdida"].copy()
    st.markdown(f"### {len(ns)} OS Perdida(s) no período")

    if ns.empty:
        st.success("Nenhum OS Perdida encontrado no período.")
    else:
        cols = [
            c for c in [
                "Data Operacional", "Tipo de Serviço", "Ticket", "Placa",
                "OS_planejada", "OS_resultado", "Consultor", "UF-base"
            ] if c in ns.columns
        ]
        st.dataframe(ns[cols], use_container_width=True, hide_index=True)

with aba_os:
    st.markdown("### Detalhamento operacional")
    tipos = sorted(classes.dropna().unique().tolist())
    filtro_tipo = st.multiselect(
        "Classificação",
        tipos,
        key="portal_filtro_classificacao",
    )

    detalhe = dados.copy()
    if filtro_tipo:
        detalhe = detalhe[detalhe["Classificação"].isin(filtro_tipo)]

    cols = [
        c for c in [
            "Data Operacional", "Tipo de Serviço", "Classificação",
            "Ticket", "Placa", "OS_planejada", "OS_resultado", "Troca de OS",
            "Razao_improdutiva", "Observacao_tecnico_improdutiva"
        ] if c in detalhe.columns
    ]
    st.dataframe(
        detalhe[cols].sort_values("Data Operacional", ascending=False),
        use_container_width=True,
        hide_index=True,
        height=560,
    )

with aba_follow:
    exibir_follow_portal()

st.divider()
st.caption(
    "Piloto YESHUA RASTREAMENTO • Dados provenientes da base operacional PS. "
    "Portal com Follow automático por planejamento."
)
