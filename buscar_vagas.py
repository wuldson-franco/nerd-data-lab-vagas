"""
Nerd Data Lab - Compilado de vagas de dados (Júnior / Pleno / Sênior / Estágio)

Fontes: Gupy (Brasil) e Remotive (remoto internacional).
Gera um .txt em vagas/ pronto para copiar e colar no WhatsApp,
evita repetir vagas já enviadas (vistas.json) e apaga .txt com mais de 7 dias.

Também mantém um histórico em docs/data/vagas.json, que alimenta o site
(docs/index.html) publicado no GitHub Pages.

Site (docs/): recebe TODAS as vagas de dados, de qualquer lugar; quem acessa filtra.
WhatsApp (.txt): só vagas remotas (de qualquer lugar) e presenciais/híbridas das
regiões definidas em REGIOES.
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
ARQ_HISTORICO = RAIZ / "docs" / "data" / "vagas.json"   # alimenta o site

DIAS_MANTER_TXT = 7        # apaga .txt mais antigos que isso
DIAS_MANTER_VISTAS = 45    # esquece links vistos há mais tempo que isso
DIAS_HISTORICO = 60        # vagas some do site se não forem vistas há X dias
DIAS_COLETA_SITE = 30      # o site recebe vagas publicadas nos últimos X dias
DIAS_VAGA_RECENTE = 10     # o .txt do WhatsApp só traz vagas publicadas nos últimos X dias
SOMENTE_REMOTO = False     # True = só vagas 100% remotas

BUSCAS_GUPY = [
    "analista de dados",
    "engenheiro de dados",
    "cientista de dados",
    "analista de BI",
    "analytics engineer",
    "data analyst",
    "machine learning",
    # estágio
    "estágio em dados",
    "estágio BI",
    "estagiário de dados",
    "estágio data analytics",
]

# Presencial/híbrido só entra se a cidade estiver aqui (remoto entra sempre).
# Formato: "região": (estado, UF, [cidades])
REGIOES = {
    "João Pessoa e região": ("paraiba", "pb", [
        "joao pessoa", "cabedelo", "bayeux", "santa rita", "conde", "lucena",
        "alhandra", "caapora", "pitimbu", "cruz do espirito santo", "rio tinto",
        "mamanguape", "sape"]),
    "Campina Grande e região": ("paraiba", "pb", [
        "campina grande", "queimadas", "lagoa seca", "puxinana", "massaranduba",
        "pocinhos", "esperanca", "alagoa nova", "boa vista", "fagundes",
        "montadas", "areial"]),
    "Recife e região": ("pernambuco", "pe", [
        "recife", "olinda", "jaboatao dos guararapes", "paulista", "camaragibe",
        "sao lourenco da mata", "cabo de santo agostinho", "ipojuca",
        "abreu e lima", "igarassu", "itamaraca", "itapissuma", "moreno",
        "aracoiaba"]),
    "Natal e região": ("rio grande do norte", "rn", [
        "natal", "parnamirim", "sao goncalo do amarante", "macaiba", "extremoz",
        "ceara-mirim", "ceara mirim", "nisia floresta", "sao jose de mipibu",
        "monte alegre"]),
}

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
    r"data entry|digitador|trainee|data center|jovem aprendiz|aprendiz"
)
RE_ESTAGIO = re.compile(r"\b(estagi\w*|intern|internship)\b")


def e_vaga_de_dados(titulo: str) -> bool:
    n = norm(titulo)
    return bool(RE_DADOS.search(n)) and not RE_EXCLUIR.search(n)


def classificar_nivel(titulo: str) -> str:
    n = norm(titulo)
    if RE_ESTAGIO.search(n):
        return "Estágio"
    if re.search(r"\b(junior|jr)\b", n):
        return "Júnior"
    if re.search(r"\b(pleno|pl)\b", n):
        return "Pleno"
    if re.search(r"\b(senior|sr)\b", n):
        return "Sênior"
    return "Nível não informado"


def _texto_local(j) -> tuple:
    """Devolve (cidade, texto_local, estado) a partir dos campos de localização."""
    cidade = _pegar(j, "city", "cityName")
    estado = _pegar(j, "state", "stateName")
    loc = _pegar(j, "location")
    return cidade, norm(" ".join([cidade, loc])).strip(), norm(estado)


def achar_regiao(texto_local: str, estado: str):
    """Nome da região se o local estiver em REGIOES; senão None."""
    for nome, (est, uf, cidades) in REGIOES.items():
        for c in cidades:
            if re.search(rf"\b{re.escape(c)}\b", texto_local):
                # se a vaga informa o estado, ele precisa bater
                if estado and est not in estado and estado != uf:
                    continue
                if not estado and uf not in texto_local.split() and est not in texto_local:
                    # sem estado informado: aceita só cidades não ambíguas
                    if c in ("santa rita", "conde", "lucena", "moreno", "paulista",
                             "boa vista", "monte alegre", "areial", "esperanca",
                             "montadas", "fagundes"):
                        continue
                return nome
    return None


def classificar_area(titulo: str) -> str:
    n = norm(titulo)
    if re.search(r"governanca|qualidade de dados|data governance|data quality", n):
        return "Governança de dados"
    if re.search(r"engenheir\w* de dados|data engineer|analytics engineer|"
                 r"banco de dados|\bdba\b|\betl\b|\bdbt\b|big data", n):
        return "Engenharia de dados"
    if re.search(r"cientista|data scien|machine learning|\bml\b|\bia\b|"
                 r"inteligencia artificial|\bnlp\b|mlops|\bia generativa", n):
        return "Ciência de dados e IA"
    if re.search(r"\bbi\b|business intelligence|power bi|dashboard|visualiza", n):
        return "BI"
    return "Análise de dados"


def parse_data(txt):
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return None


def recente(txt_data, agora, dias=None) -> bool:
    d = parse_data(txt_data)
    dias = DIAS_COLETA_SITE if dias is None else dias
    return True if d is None else (agora - d) <= timedelta(days=dias)


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
                if isinstance(corpo, dict) and "pagination" in corpo:
                    print(f"[debug] paginação: {corpo['pagination']}")
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
                cidade, texto_local, estado = _texto_local(j)
                vagas.append({
                    "titulo": _pegar(j, "name", "title"),
                    "empresa": _pegar(j, "careerPageName", "company", "companyName"),
                    "url": _pegar(j, "jobUrl", "applyUrl", "url"),
                    "modelo": modelo,
                    "cidade": cidade,
                    "texto_local": texto_local,
                    "estado": estado,
                    "estado_nome": _pegar(j, "state", "stateName"),
                    "fonte": "Gupy",
                    "publicada": _pegar(j, "publishedDate"),
                })
            if len(itens) < tamanho:
                break
            offset += tamanho
            if _ + 1 == MAX_PAGINAS_POR_TERMO:
                print(f"[aviso] '{termo}' atingiu o limite de {MAX_PAGINAS_POR_TERMO} "
                      f"páginas ({MAX_PAGINAS_POR_TERMO * tamanho} vagas); pode haver mais")
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
            "cidade": "", "texto_local": "", "estado": "", "estado_nome": "",
            "fonte": "Remotive",
            "publicada": j.get("publication_date", ""),
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
        m = re.search(r"vagas_(\d{4}-\d{2}-\d{2})(?:_\d{4})?\.txt", arq.name)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d").date() < limite:
            arq.unlink()
            print(f"[limpeza] removido {arq.name}")


def atualizar_historico(todas, agora):
    """Guarda as vagas (sem repetir por link) em docs/data/vagas.json para o site."""
    try:
        atual = json.loads(ARQ_HISTORICO.read_text(encoding="utf-8")).get("vagas", [])
    except (FileNotFoundError, json.JSONDecodeError):
        atual = []
    por_url = {v["url"]: v for v in atual if v.get("url")}
    hoje = f"{agora:%Y-%m-%d}"
    for v in todas:
        d = parse_data(v.get("publicada"))
        registro = por_url.get(v["url"], {})
        por_url[v["url"]] = {
            "titulo": v["titulo"],
            "empresa": v["empresa"],
            "url": v["url"],
            "nivel": v["nivel"],
            "area": v["area"],
            "modelo": v["modelo"] if v["modelo"] in ("remote", "hybrid") else "on-site",
            "cidade": v.get("cidade", ""),
            "estado": v.get("estado_nome", ""),
            "regiao": v.get("regiao", "Remoto" if v["modelo"] == "remote" else "Outras regiões"),
            "fonte": v["fonte"],
            "publicada": f"{d:%Y-%m-%d}" if d else registro.get("publicada", hoje),
            "visto_em": hoje,
        }
    limite = f"{(agora - timedelta(days=DIAS_HISTORICO)):%Y-%m-%d}"
    vagas = [v for v in por_url.values() if v["visto_em"] >= limite]
    vagas.sort(key=lambda v: (v["publicada"], v["titulo"]), reverse=True)
    ARQ_HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    ARQ_HISTORICO.write_text(json.dumps(
        {"atualizado_em": agora.isoformat(timespec="minutes"),
         "total": len(vagas), "vagas": vagas},
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[info] histórico do site: {len(vagas)} vagas em {ARQ_HISTORICO.name}")


def montar_texto(vagas, agora) -> str:
    ordem = ["Júnior", "Pleno", "Sênior", "Nível não informado", "Estágio"]
    emoji = {"Júnior": "🌱", "Pleno": "🚀", "Sênior": "🏆",
             "Nível não informado": "🔎", "Estágio": "🎓"}
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
            local = v.get("cidade") or v.get("regiao", "")
            sufixo = f" - {local}" if local else ""
            if v["modelo"] == "remote":
                tag = " 🏠 remoto"
            elif v["modelo"] == "hybrid":
                tag = f" 📍 híbrido{sufixo}"
            else:
                tag = f" 📍 presencial{sufixo}"
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
    novas, todas, chaves = [], [], set()
    fora_da_regiao = sem_local = 0
    for v in brutas:
        if not v["url"] or not e_vaga_de_dados(v["titulo"]):
            continue
        # mesma vaga em cidades diferentes conta como vagas diferentes
        chave = norm(v["titulo"] + v["empresa"] + v["cidade"])
        if chave in chaves:
            continue
        chaves.add(chave)
        v["nivel"] = classificar_nivel(v["titulo"])
        v["area"] = classificar_area(v["titulo"])

        # região: remoto = "Remoto"; presencial/híbrido = região da comunidade ou "Outras regiões"
        vai_pro_whats = True
        if v["modelo"] == "remote":
            v["regiao"] = "Remoto"
        else:
            texto = v["texto_local"] or norm(v["titulo"])  # sem local: tenta o título
            regiao = achar_regiao(texto, v["estado"])
            v["regiao"] = regiao or "Outras regiões"
            if not regiao:
                vai_pro_whats = False
                if v["texto_local"]:
                    fora_da_regiao += 1
                else:
                    sem_local += 1
        if SOMENTE_REMOTO and v["modelo"] != "remote":
            vai_pro_whats = False
        if not recente(v.get("publicada"), agora, DIAS_VAGA_RECENTE):
            vai_pro_whats = False   # vaga mais antiga: só no site

        todas.append(v)                                   # site: tudo
        if vai_pro_whats and v["url"] not in vistas:      # WhatsApp: filtrado e sem repetir
            novas.append(v)

    atualizar_historico(todas, agora)
    print(f"[info] fora do .txt do WhatsApp (continuam no site): {fora_da_regiao} fora das "
          f"regiões, {sem_local} sem informação de local")
    if not novas:
        print("[info] Nenhuma vaga nova desta vez.")
        ARQ_VISTAS.write_text(json.dumps(vistas, indent=1), encoding="utf-8")
        return

    arquivo = PASTA_VAGAS / f"vagas_{agora:%Y-%m-%d}.txt"
    if arquivo.exists():   # segunda execução no mesmo dia: não sobrescreve
        arquivo = PASTA_VAGAS / f"vagas_{agora:%Y-%m-%d}_{agora:%H%M}.txt"
    arquivo.write_text(montar_texto(novas, agora), encoding="utf-8")
    for v in novas:
        vistas[v["url"]] = agora.isoformat()
    ARQ_VISTAS.write_text(json.dumps(vistas, indent=1), encoding="utf-8")
    print(f"[ok] {len(novas)} vagas novas salvas em {arquivo.name}")


if __name__ == "__main__":
    main()