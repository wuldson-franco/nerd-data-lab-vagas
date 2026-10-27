"""
Nerd Data Lab - Compilado de vagas de dados (Júnior / Pleno / Sênior)

Fontes: Gupy (Brasil) e Remotive (remoto internacional).
Gera um .txt em vagas/ pronto para copiar e colar no WhatsApp,
evita repetir vagas já enviadas (vistas.json) e apaga .txt com mais de 7 dias.
"""
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------- CONFIGURAÇÃO -----------------------------
TZ = ZoneInfo("America/Sao_Paulo")
RAIZ = Path(__file__).parent
PASTA_VAGAS = RAIZ / "vagas"
ARQ_VISTAS = RAIZ / "vistas.json"

DIAS_MANTER_TXT = 7        # apaga .txt mais antigos que isso
DIAS_MANTER_VISTAS = 45    # esquece links vistos há mais tempo que isso
DIAS_VAGA_RECENTE = 10     # ignora vagas publicadas há mais de X dias
SOMENTE_REMOTO = False     # True = só vagas 100% remotas

BUSCAS_GUPY = [
    "analista de dados",
    "engenheiro de dados",
    "cientista de dados",
    "analista de BI",
    "analytics engineer",
    "data analyst",
    "machine learning",
]

LOCAIS_REMOTIVE_OK = ("brazil", "brasil", "latam", "latin america", "south america",
                      "americas", "worldwide", "anywhere")

HEADERS = {"User-Agent": "NerdDataLab-JobBot/1.0 (projeto pessoal)"}

# ----------------------------- UTILITÁRIOS ------------------------------
def norm(txt: str) -> str:
    """minúsculo e sem acento, para facilitar regex."""
    txt = unicodedata.normalize("NFKD", txt or "")
    return "".join(c for c in txt if not unicodedata.combining(c)).lower()


RE_DADOS = re.compile(
    r"\b(dados|data|bi|analytics|machine learning|business intelligence|"
    r"inteligencia de negocio|etl|dbt|power bi)\b"
)
RE_EXCLUIR = re.compile(
    r"data entry|digitador|estagi|intern\b|internship|trainee|data center|"
    r"jovem aprendiz|aprendiz"
)


def e_vaga_de_dados(titulo: str) -> bool:
    n = norm(titulo)
    return bool(RE_DADOS.search(n)) and not RE_EXCLUIR.search(n)


def classificar_nivel(titulo: str) -> str:
    n = norm(titulo)
    if re.search(r"\b(junior|jr)\b", n):
        return "Júnior"
    if re.search(r"\b(pleno|pl)\b", n):
        return "Pleno"
    if re.search(r"\b(senior|sr)\b", n):
        return "Sênior"
    return "Nível não informado"


def parse_data(txt):
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return None


def recente(txt_data, agora) -> bool:
    d = parse_data(txt_data)
    return True if d is None else (agora - d) <= timedelta(days=DIAS_VAGA_RECENTE)


# ------------------------------- FONTES ---------------------------------
GUPY_URL = "https://portal.gupy.io/api/job-search/jobs"
GUPY_HEADERS = {
    **HEADERS,
    "Accept": "application/json",
    "Referer": "https://portal.gupy.io/job-search",
}
TAMANHOS_PAGINA = (100, 50, 12)   # tenta o maior; se a API recusar (400), desce
MAX_PAGINAS_POR_TERMO = 4


def _extrair_lista(corpo):
    """A API pode devolver a lista em chaves diferentes; tenta as comuns."""
    if isinstance(corpo, list):
        return corpo
    if isinstance(corpo, dict):
        for chave in ("data", "results", "jobs", "items"):
            if isinstance(corpo.get(chave), list):
                return corpo[chave]
    return []


def _pegar(d, *chaves):
    for c in chaves:
        v = d.get(c)
        if v:
            return str(v).strip()
    return ""


def _gupy_get(termo, offset, tamanho):
    r = requests.get(
        GUPY_URL,
        params={"jobName": termo, "limit": tamanho, "offset": offset},
        headers=GUPY_HEADERS, timeout=30,
    )
    if r.status_code != 200:
        print(f"[aviso] Gupy '{termo}' offset {offset} limit {tamanho}: "
              f"HTTP {r.status_code} - {r.text[:200]!r}")
        return None
    return r.json()


def buscar_gupy(agora):
    vagas = []
    tamanho = None          # descoberto na primeira resposta válida
    primeira_resposta = True
    for termo in BUSCAS_GUPY:
        offset = 0
        for _ in range(MAX_PAGINAS_POR_TERMO):
            corpo = None
            for t in ([tamanho] if tamanho else TAMANHOS_PAGINA):
                try:
                    corpo = _gupy_get(termo, offset, t)
                except Exception as e:
                    print(f"[aviso] Gupy '{termo}': {e}")
                    corpo = None
                if corpo is not None:
                    tamanho = t
                    break
            if corpo is None:
                break
            itens = _extrair_lista(corpo)
            if primeira_resposta:
                primeira_resposta = False
                chaves = list(corpo.keys()) if isinstance(corpo, dict) else "lista"
                print(f"[debug] Gupy OK (limit={tamanho}); chaves da resposta: {chaves}")
                if itens:
                    print(f"[debug] campos de uma vaga: {sorted(itens[0].keys())}")
                else:
                    print(f"[debug] resposta sem lista de vagas: {str(corpo)[:300]}")
            for j in itens:
                if not recente(j.get("publishedDate"), agora):
                    continue
                modelo = (j.get("workplaceType") or "").lower()
                if j.get("isRemoteWork"):
                    modelo = "remote"
                vagas.append({
                    "titulo": _pegar(j, "name", "title"),
                    "empresa": _pegar(j, "careerPageName", "company", "companyName"),
                    "url": _pegar(j, "jobUrl", "applyUrl", "url"),
                    "modelo": modelo,
                })
            if len(itens) < tamanho:
                break
            offset += tamanho
            time.sleep(1)
        time.sleep(1)
    print(f"[info] Gupy: {len(vagas)} vagas recentes coletadas")
    return vagas


def buscar_remotive(agora):
    vagas = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params={"category": "data"}, headers=HEADERS, timeout=30)
        r.raise_for_status()
        itens = r.json().get("jobs", [])
    except Exception as e:
        print(f"[aviso] Remotive: {e}")
        return vagas
    for j in itens:
        local = norm(j.get("candidate_required_location", ""))
        if local and not any(k in local for k in LOCAIS_REMOTIVE_OK):
            continue
        if not recente(j.get("publication_date"), agora):
            continue
        vagas.append({
            "titulo": j.get("title", "").strip(),
            "empresa": j.get("company_name", "").strip(),
            "url": j.get("url", ""),
            "modelo": "remote",
        })
    return vagas


# --------------------------- PROCESSAMENTO ------------------------------
def carregar_vistas() -> dict:
    try:
        return json.loads(ARQ_VISTAS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def limpar_vistas(vistas: dict, agora) -> dict:
    limite = agora - timedelta(days=DIAS_MANTER_VISTAS)
    return {u: d for u, d in vistas.items()
            if (parse_data(d) or agora) >= limite}


def limpar_txt_antigos(agora):
    limite = (agora - timedelta(days=DIAS_MANTER_TXT)).date()
    for arq in PASTA_VAGAS.glob("vagas_*.txt"):
        m = re.search(r"vagas_(\d{4}-\d{2}-\d{2})\.txt", arq.name)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d").date() < limite:
            arq.unlink()
            print(f"[limpeza] removido {arq.name}")


def montar_texto(vagas, agora) -> str:
    ordem = ["Júnior", "Pleno", "Sênior", "Nível não informado"]
    emoji = {"Júnior": "🌱", "Pleno": "🚀", "Sênior": "🏆", "Nível não informado": "🔎"}
    por_nivel = {n: [] for n in ordem}
    for v in vagas:
        por_nivel[v["nivel"]].append(v)

    linhas = [
        f"#nerddatalab | Vagas de dados, {agora:%d/%m} 📊",
        "",
        "Candidate-se hoje mesmo nas que fazem sentido pro seu perfil. 💡",
        "",
    ]
    for nivel in ordem:
        grupo = sorted(por_nivel[nivel], key=lambda v: norm(v["titulo"]))
        if not grupo:
            continue
        linhas.append(f"━ {nivel.upper()} {emoji[nivel]}")
        linhas.append("")
        for v in grupo:
            tag = ""
            if v["modelo"] == "hybrid":
                tag = " 📍 híbrido"
            elif v["modelo"] in ("on-site", "onsite"):
                tag = " 📍 presencial"
            nome = f"{v['titulo']} | {v['empresa']}" if v["empresa"] else v["titulo"]
            linhas.append(f"[{nome}]{tag}")
            linhas.append(v["url"])
            linhas.append("")
    return "\n".join(linhas).rstrip() + "\n"


def main():
    agora = datetime.now(TZ)
    PASTA_VAGAS.mkdir(exist_ok=True)
    limpar_txt_antigos(agora)

    brutas = buscar_gupy(agora) + buscar_remotive(agora)
    print(f"[info] {len(brutas)} vagas brutas coletadas")

    vistas = limpar_vistas(carregar_vistas(), agora)
    novas, chaves = [], set()
    for v in brutas:
        if not v["url"] or not e_vaga_de_dados(v["titulo"]):
            continue
        if SOMENTE_REMOTO and v["modelo"] != "remote":
            continue
        chave = norm(v["titulo"] + v["empresa"])
        if v["url"] in vistas or chave in chaves:
            continue
        chaves.add(chave)
        v["nivel"] = classificar_nivel(v["titulo"])
        novas.append(v)

    if not novas:
        print("[info] Nenhuma vaga nova desta vez.")
        ARQ_VISTAS.write_text(json.dumps(vistas, indent=1), encoding="utf-8")
        return

    arquivo = PASTA_VAGAS / f"vagas_{agora:%Y-%m-%d}.txt"
    arquivo.write_text(montar_texto(novas, agora), encoding="utf-8")
    for v in novas:
        vistas[v["url"]] = agora.isoformat()
    ARQ_VISTAS.write_text(json.dumps(vistas, indent=1), encoding="utf-8")
    print(f"[ok] {len(novas)} vagas novas salvas em {arquivo.name}")


if __name__ == "__main__":
    main()