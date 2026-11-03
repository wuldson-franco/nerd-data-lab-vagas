"""
Nerd Data Lab - Compilado de vagas de dados (Júnior / Pleno / Sênior / Estágio)

O QUE MUDAR SEM MEXER NO CÓDIGO
  config.json          fontes (endereços), termos de busca, regiões, níveis, áreas, prazos
  vagas_manuais.json   vagas que você cola à mão (entram no site e no WhatsApp)

O QUE ESTE SCRIPT FAZ
  1. Lê as fontes ativas: Gupy, Adzuna (precisa de chave) e Remotive.
  2. Junta com as vagas manuais, remove repetidas e classifica nível e área.
  3. docs/data/vagas.json  -> alimenta o site (docs/index.html) no GitHub Pages.
     Recebe TODAS as vagas de dados, de qualquer lugar; quem acessa filtra.
  4. vagas/vagas_AAAA-MM-DD.txt -> compilado para o WhatsApp, só com vagas remotas
     (de qualquer lugar) e presenciais/híbridas das regiões do config.json.
  5. vistas.json evita repetir no WhatsApp uma vaga já enviada.
"""
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Sao_Paulo")
RAIZ = Path(__file__).parent
CAMINHO_CONFIG = RAIZ / "config.json"
ARQ_MANUAIS = RAIZ / "vagas_manuais.json"
PASTA_VAGAS = RAIZ / "vagas"
ARQ_VISTAS = RAIZ / "vistas.json"
ARQ_USO = RAIZ / "uso_apis.json"                         # contador de chamadas das APIs com cota
ARQ_HISTORICO = RAIZ / "docs" / "data" / "vagas.json"   # alimenta o site

HEADERS = {"User-Agent": "NerdDataLab-JobBot/1.0 (projeto pessoal)"}
NIVEIS_FIXOS = ("Júnior", "Pleno", "Sênior", "Estágio")   # o site depende destes nomes
NIVEL_PADRAO = "Nível não informado"

# Preenchidos por iniciar_config() a partir do config.json
CFG = {}
REGIOES = {}          # nome -> (estado, uf, [cidades], {cidades que exigem o estado})
RE_DADOS = RE_EXCLUIR = None
NIVEIS_RE = []        # [(nome, regex)]
AREAS_RE = []         # [(nome, regex)]
AREA_PADRAO = ""
DIAS_MANTER_TXT = DIAS_MANTER_VISTAS = DIAS_HISTORICO = DIAS_COLETA_SITE = DIAS_VAGA_RECENTE = 0
SOMENTE_REMOTO = False
STATUS = {}           # fonte -> {"estado": ok|parcial|falhou|pulada|desativada, "vagas": n, "erros": n}


# ----------------------------- UTILITÁRIOS ------------------------------
def norm(txt: str) -> str:
    """minúsculo e sem acento, para facilitar regex."""
    txt = unicodedata.normalize("NFKD", txt or "")
    return "".join(c for c in txt if not unicodedata.combining(c)).lower()


def _pegar(d, *chaves):
    for c in chaves:
        v = d.get(c)
        if v:
            return str(v).strip()
    return ""


def _sem_html(txt):
    return re.sub(r"<[^>]+>", "", txt or "").strip()


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


def regex_de_termos(termos):
    """Lista de termos -> regex. Cada termo vira palavra inteira; '*' = qualquer final."""
    partes = []
    for t in termos:
        t = norm(str(t)).strip()
        if t:
            partes.append(re.escape(t).replace(r"\*", r"\w*"))
    if not partes:
        return re.compile(r"(?!x)x")          # nunca combina
    return re.compile(r"\b(?:" + "|".join(partes) + r")\b")


# ------------------------------ CONFIGURAÇÃO ----------------------------
class ConfigInvalida(Exception):
    pass


def _sem_ajuda(d):
    """Ignora chaves que começam com '_' (são só comentários)."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def iniciar_config(caminho=None):
    global CFG, REGIOES, RE_DADOS, RE_EXCLUIR, NIVEIS_RE, AREAS_RE, AREA_PADRAO
    global DIAS_MANTER_TXT, DIAS_MANTER_VISTAS, DIAS_HISTORICO, DIAS_COLETA_SITE
    global DIAS_VAGA_RECENTE, SOMENTE_REMOTO
    caminho = Path(caminho or CAMINHO_CONFIG)
    try:
        cfg = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigInvalida(f"Não achei {caminho.name}. Ele precisa ficar na raiz do repositório.")
    except json.JSONDecodeError as e:
        raise ConfigInvalida(
            f"{caminho.name} tem erro de formatação na linha {e.lineno}, coluna {e.colno} "
            f"({e.msg}). Procure uma vírgula faltando ou sobrando, ou uma aspa sem fechar.")

    obrigatorios = ("prazos_em_dias", "buscas", "filtros_de_titulo", "niveis", "areas",
                    "regioes", "fontes")
    faltam = [k for k in obrigatorios if k not in cfg]
    if faltam:
        raise ConfigInvalida(f"{caminho.name}: faltam as seções {', '.join(faltam)}.")

    prazos = cfg["prazos_em_dias"]
    for k in ("vaga_no_whatsapp", "coletar_para_o_site", "manter_no_site", "manter_txt", "manter_vistas"):
        if not isinstance(prazos.get(k), int):
            raise ConfigInvalida(f"{caminho.name}: prazos_em_dias.{k} precisa ser um número inteiro.")
    if not cfg["buscas"] or not all(isinstance(b, str) and b.strip() for b in cfg["buscas"]):
        raise ConfigInvalida(f"{caminho.name}: 'buscas' precisa ter ao menos um termo de texto.")

    filtros = cfg["filtros_de_titulo"]
    niveis = _sem_ajuda(cfg["niveis"])
    fora = [n for n in niveis if n not in NIVEIS_FIXOS]
    if fora:
        raise ConfigInvalida(f"{caminho.name}: nível desconhecido {fora}. Use só: {', '.join(NIVEIS_FIXOS)}.")

    regioes = {}
    for nome, r in _sem_ajuda(cfg["regioes"]).items():
        for k in ("estado", "uf", "cidades"):
            if k not in r:
                raise ConfigInvalida(f"{caminho.name}: a região '{nome}' não tem '{k}'.")
        regioes[nome] = (norm(r["estado"]), norm(r["uf"]),
                         [norm(c) for c in r["cidades"]],
                         {norm(c) for c in r.get("exigem_estado", [])})

    for nome in ("gupy", "adzuna", "jooble", "google", "remotive"):
        if nome not in cfg["fontes"] or "url" not in cfg["fontes"][nome]:
            raise ConfigInvalida(f"{caminho.name}: a fonte '{nome}' precisa de 'url'.")
    emp = cfg["fontes"].get("empresas")
    if not isinstance(emp, dict) or not isinstance(emp.get("lista"), list):
        raise ConfigInvalida(f"{caminho.name}: a fonte 'empresas' precisa de uma 'lista'.")
    for i, e in enumerate(emp["lista"], 1):
        if not isinstance(e, dict) or not str(e.get("url", "")).startswith(("http://", "https://")):
            raise ConfigInvalida(f"{caminho.name}: empresas.lista, item {i}: faltou o 'url' "
                                 f"(precisa começar com https://).")
    for nome in ("jooble", "google"):
        lim = cfg["fontes"][nome].get("limite") or {}
        if lim.get("tipo") not in ("total", "mensal") or not isinstance(lim.get("maximo"), int):
            raise ConfigInvalida(f"{caminho.name}: fontes.{nome}.limite precisa de "
                                 f"\"tipo\" (total ou mensal) e \"maximo\" (número inteiro).")

    CFG = cfg
    REGIOES = regioes
    RE_DADOS = regex_de_termos(filtros.get("deve_conter", []))
    RE_EXCLUIR = regex_de_termos(filtros.get("nao_pode_conter", []))
    NIVEIS_RE = [(n, regex_de_termos(t)) for n, t in niveis.items()]
    AREAS_RE = [(n, regex_de_termos(t)) for n, t in _sem_ajuda(cfg["areas"]).items()]
    AREA_PADRAO = cfg.get("area_padrao", "Análise de dados")
    DIAS_VAGA_RECENTE = prazos["vaga_no_whatsapp"]
    DIAS_COLETA_SITE = prazos["coletar_para_o_site"]
    DIAS_HISTORICO = prazos["manter_no_site"]
    DIAS_MANTER_TXT = prazos["manter_txt"]
    DIAS_MANTER_VISTAS = prazos["manter_vistas"]
    SOMENTE_REMOTO = bool(cfg.get("somente_remoto_no_whatsapp", False))


# --------------------------- CLASSIFICAÇÃO ------------------------------
def e_vaga_de_dados(titulo: str) -> bool:
    n = norm(titulo)
    return bool(RE_DADOS.search(n)) and not RE_EXCLUIR.search(n)


def classificar_nivel(titulo: str) -> str:
    n = norm(titulo)
    for nome, rx in NIVEIS_RE:
        if rx.search(n):
            return nome
    return NIVEL_PADRAO


def classificar_area(titulo: str) -> str:
    n = norm(titulo)
    for nome, rx in AREAS_RE:
        if rx.search(n):
            return nome
    return AREA_PADRAO


def _texto_local(j) -> tuple:
    """Devolve (cidade, texto_local, estado) a partir dos campos de localização."""
    cidade = _pegar(j, "city", "cityName")
    estado = _pegar(j, "state", "stateName")
    loc = _pegar(j, "location")
    return cidade, norm(" ".join([cidade, loc])).strip(), norm(estado)


def achar_regiao(texto_local: str, estado: str):
    """Nome da região se o local estiver nas regiões do config.json; senão None."""
    for nome, (est, uf, cidades, exigem_estado) in REGIOES.items():
        for c in cidades:
            if re.search(rf"\b{re.escape(c)}\b", texto_local):
                # se a vaga informa o estado, ele precisa bater
                if estado and est not in estado and estado != uf:
                    continue
                if not estado and uf not in texto_local.split() and est not in texto_local:
                    # sem estado informado: aceita só cidades de nome único
                    if c in exigem_estado:
                        continue
                return nome
    return None


# ------------------------------- FONTES ---------------------------------
def registrar(nome, vagas, erros):
    estado = "falhou" if (erros and not vagas) else "parcial" if erros else "ok"
    STATUS[nome] = {"estado": estado, "vagas": len(vagas), "erros": erros}


def _extrair_lista(corpo):
    """A API pode devolver a lista em chaves diferentes; tenta as comuns."""
    if isinstance(corpo, list):
        return corpo
    if isinstance(corpo, dict):
        for chave in ("data", "results", "jobs", "items"):
            if isinstance(corpo.get(chave), list):
                return corpo[chave]
    return []


def _gupy_get(cfg, termo, offset, tamanho):
    headers = {**HEADERS, "Accept": "application/json"}
    if cfg.get("referer"):
        headers["Referer"] = cfg["referer"]
    r = requests.get(cfg["url"], params={"jobName": termo, "limit": tamanho, "offset": offset},
                     headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"[aviso] Gupy '{termo}' offset {offset} limit {tamanho}: "
              f"HTTP {r.status_code} - {r.text[:200]!r}")
        return None
    return r.json()


def buscar_gupy(agora, cfg):
    vagas, erros = [], 0
    tamanhos = tuple(cfg.get("tamanhos_de_pagina", (100, 50, 12)))
    max_paginas = cfg.get("max_paginas_por_busca", 4)
    tamanho = None          # descoberto na primeira resposta válida
    primeira_resposta = True
    for termo in cfg.get("buscas", CFG["buscas"]):
        offset = 0
        for pagina in range(max_paginas):
            corpo = None
            for t in ([tamanho] if tamanho else tamanhos):
                try:
                    corpo = _gupy_get(cfg, termo, offset, t)
                except Exception as e:
                    print(f"[aviso] Gupy '{termo}': {e}")
                    corpo = None
                if corpo is not None:
                    tamanho = t
                    break
            if corpo is None:
                erros += 1
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
            if pagina + 1 == max_paginas:
                print(f"[aviso] '{termo}' atingiu o limite de {max_paginas} páginas "
                      f"({max_paginas * tamanho} vagas); pode haver mais")
            time.sleep(1)
        time.sleep(1)
    print(f"[info] Gupy: {len(vagas)} vagas recentes coletadas")
    registrar("Gupy", vagas, erros)
    return vagas


def buscar_adzuna(agora, cfg):
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not (app_id and app_key):
        print("[info] Adzuna pulada: faltam os Secrets ADZUNA_APP_ID e ADZUNA_APP_KEY.")
        STATUS["Adzuna"] = {"estado": "pulada", "vagas": 0, "erros": 0}
        return []
    re_remoto = regex_de_termos(cfg.get("palavras_remoto", []))
    re_hibrido = regex_de_termos(cfg.get("palavras_hibrido", []))
    paginas = cfg.get("paginas_por_busca", 2)
    por_pagina = cfg.get("vagas_por_pagina", 50)
    vagas, erros, primeira, interromper = [], 0, True, False
    for termo in cfg.get("buscas", CFG["buscas"]):
        for pagina in range(1, paginas + 1):
            try:
                r = requests.get(
                    cfg["url"].format(pagina=pagina),
                    params={"app_id": app_id, "app_key": app_key,
                            "results_per_page": por_pagina, "what": termo,
                            "max_days_old": DIAS_COLETA_SITE, "sort_by": "date"},
                    headers={**HEADERS, "Accept": "application/json"}, timeout=30)
            except Exception as e:
                print(f"[aviso] Adzuna '{termo}': {e}")
                erros += 1
                break
            if r.status_code in (401, 403):
                print(f"[aviso] Adzuna: chave recusada (HTTP {r.status_code}). Confira os Secrets.")
                erros += 1
                interromper = True
                break
            if r.status_code == 429:
                print("[aviso] Adzuna: limite de requisições atingido; usando o que já veio.")
                erros += 1
                interromper = True
                break
            if r.status_code != 200:
                print(f"[aviso] Adzuna '{termo}' pág {pagina}: HTTP {r.status_code} - {r.text[:200]!r}")
                erros += 1
                break
            try:
                corpo = r.json()
            except ValueError:
                print(f"[aviso] Adzuna '{termo}': resposta não é JSON")
                erros += 1
                break
            itens = corpo.get("results", []) if isinstance(corpo, dict) else []
            if primeira:
                primeira = False
                print(f"[debug] Adzuna OK; chaves: {list(corpo.keys())}")
                if itens:
                    print(f"[debug] Adzuna campos: {sorted(itens[0].keys())}; "
                          f"location: {itens[0].get('location')}")
            for j in itens:
                criada = j.get("created", "")
                if not recente(criada, agora):
                    continue
                titulo = _sem_html(j.get("title"))
                loc = j.get("location") or {}
                area = [a for a in (loc.get("area") or []) if a]
                nome_local = loc.get("display_name", "") or ""
                if len(area) >= 3:
                    estado_nome, cidade = area[1], area[-1]
                elif len(area) == 2:
                    estado_nome, cidade = area[1], ""
                else:
                    partes = [p.strip() for p in nome_local.split(",") if p.strip()]
                    cidade = partes[0] if partes else ""
                    estado_nome = partes[-1] if len(partes) > 1 else ""
                texto = norm(titulo + " " + _sem_html(j.get("description")))
                modelo = ("remote" if re_remoto.search(texto)
                          else "hybrid" if re_hibrido.search(texto) else "on-site")
                vagas.append({
                    "titulo": titulo,
                    "empresa": _sem_html((j.get("company") or {}).get("display_name")),
                    "url": j.get("redirect_url", ""),
                    "modelo": modelo,
                    "cidade": cidade,
                    "texto_local": norm(cidade + " " + nome_local).strip(),
                    "estado": norm(estado_nome),
                    "estado_nome": estado_nome,
                    "fonte": "Adzuna",
                    "publicada": criada,
                })
            if len(itens) < por_pagina:
                break
            time.sleep(1)
        if interromper:
            break
        time.sleep(1)
    print(f"[info] Adzuna: {len(vagas)} vagas recentes coletadas")
    registrar("Adzuna", vagas, erros)
    return vagas


def buscar_remotive(agora, cfg):
    vagas = []
    try:
        r = requests.get(cfg["url"], params={"category": cfg.get("categoria", "data")},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        itens = r.json().get("jobs", [])
    except Exception as e:
        print(f"[aviso] Remotive: {e}")
        registrar("Remotive", vagas, 1)
        return vagas
    aceitos = [norm(x) for x in cfg.get("locais_aceitos", [])]
    for j in itens:
        local = norm(j.get("candidate_required_location", ""))
        if local and not any(k in local for k in aceitos):
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
    registrar("Remotive", vagas, 0)
    return vagas


# ----------------- APOIO PARA FONTES COM COTA (Jooble, Google) -----------------
def _sem_segredo(txt, *segredos):
    """Tira chaves de API de qualquer texto antes de imprimir (o log do Actions é público)."""
    txt = str(txt)
    for s in segredos:
        if s:
            txt = txt.replace(s, "***")
    return txt


def nome_estado(txt):
    """'PE' -> 'Pernambuco'; 'pernambuco' -> 'Pernambuco'; senão ''."""
    mapa = _sem_ajuda(CFG.get("estados", {}))
    t = (txt or "").strip()
    if t.upper() in mapa:
        return mapa[t.upper()]
    for nome in mapa.values():
        if norm(nome) == norm(t):
            return nome
    return ""


def separar_local(loc):
    """'Recife, PE' / 'São Paulo - SP' / 'Recife, State of Pernambuco, Brazil' -> (cidade, estado)."""
    partes = [p.strip() for p in re.split(r"[,\-/|]", loc or "") if p.strip()]
    cidade = estado = ""
    for i, p in enumerate(partes):
        p_sem = re.sub(r"^(state of|estado de|estado do|estado da)\s+", "", norm(p)).strip()
        com_prefixo = p_sem != norm(p)
        e = nome_estado(p_sem if com_prefixo else p)
        if i == 0 and len(partes) > 1 and not com_prefixo and len(p) > 2:
            e = ""            # 1º pedaço é a cidade ('São Paulo - SP'), mesmo se o nome é de estado
        if e and not estado:
            estado = e
        elif norm(p) in ("brasil", "brazil", "br", "anywhere", "qualquer lugar"):
            continue
        elif not cidade:
            cidade = p
    return cidade, estado


_UNIDADES = {"minuto": 1 / 1440, "hora": 1 / 24, "dia": 1, "semana": 7, "mes": 30, "meses": 30}


def parse_relativo(txt, agora):
    """'há 3 dias', '3 days ago', 'ontem', '2 semanas atrás' -> datetime; None se não entender."""
    t = norm(txt or "")
    if not t:
        return None
    if re.search(r"\b(hoje|today|agora|just posted|recem)\b", t):
        return agora
    if re.search(r"\b(ontem|yesterday)\b", t):
        return agora - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*(minuto|min|hora|hour|hr|dia|day|semana|week|mes|meses|month)", t)
    if not m:
        return None
    n, un = int(m.group(1)), m.group(2)
    un = {"min": "minuto", "hour": "hora", "hr": "hora", "day": "dia",
          "week": "semana", "month": "mes"}.get(un, un)
    return agora - timedelta(days=n * _UNIDADES[un])


class Cota:
    """Conta chamadas de API em uso_apis.json para nunca estourar o plano gratuito."""
    def __init__(self, fonte, cfg, agora, arquivo=None):
        self.fonte, self.agora = fonte, agora
        self.arquivo = Path(arquivo or ARQ_USO)
        lim = cfg.get("limite") or {}
        self.tipo = lim.get("tipo", "mensal")
        self.maximo = int(lim.get("maximo", 0))
        self.periodo = "total" if self.tipo == "total" else f"{agora:%Y-%m}"

    def _ler(self):
        try:
            return json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def usado(self):
        return int(self._ler().get(self.fonte, {}).get(self.periodo, 0))

    def pode(self):
        return self.usado() < self.maximo

    def gastar(self):
        """Registra a chamada ANTES de fazê-la (conservador) e grava na hora."""
        dados = self._ler()
        d = dados.setdefault(self.fonte, {})
        if self.tipo != "total":                       # guarda só o mês corrente
            d = dados[self.fonte] = {k: v for k, v in d.items() if k == self.periodo}
        d[self.periodo] = int(d.get(self.periodo, 0)) + 1
        self.arquivo.write_text(json.dumps(dados, indent=1, ensure_ascii=False), encoding="utf-8")

    def devolver(self):
        """Busca sem resultado não é cobrada pelo SerpAPI: tira do contador."""
        dados = self._ler()
        d = dados.get(self.fonte, {})
        if d.get(self.periodo, 0) > 0:
            d[self.periodo] -= 1
            self.arquivo.write_text(json.dumps(dados, indent=1, ensure_ascii=False), encoding="utf-8")

    def resumo(self):
        return f"{self.usado()}/{self.maximo} ({'no total' if self.tipo == 'total' else 'neste mês'})"


def _modelo_por_texto(texto, re_remoto, re_hibrido):
    t = norm(texto)
    return "remote" if re_remoto.search(t) else "hybrid" if re_hibrido.search(t) else "on-site"


def buscar_jooble(agora, cfg):
    chave = os.environ.get("JOOBLE_API_KEY", "").strip()
    if not chave:
        print("[info] Jooble pulada: falta o Secret JOOBLE_API_KEY.")
        STATUS["Jooble"] = {"estado": "pulada", "vagas": 0, "erros": 0}
        return []
    cota = Cota("jooble", cfg, agora)
    re_remoto = regex_de_termos(cfg.get("palavras_remoto", []))
    re_hibrido = regex_de_termos(cfg.get("palavras_hibrido", []))
    por_pagina = cfg.get("vagas_por_pagina", 50)
    desde = f"{agora - timedelta(days=DIAS_COLETA_SITE):%Y-%m-%d}"
    vagas, erros, primeira, parar = [], 0, True, False
    print(f"[info] Jooble: cota usada {cota.resumo()}")
    for termo in cfg.get("buscas", CFG["buscas"]):
        for pagina in range(1, cfg.get("paginas_por_busca", 1) + 1):
            if not cota.pode():
                print(f"[aviso] Jooble: cota de {cota.maximo} chamadas atingida; fonte pausada "
                      f"para preservar a chave. Aumente 'limite.maximo' no config.json se quiser.")
                parar = True
                break
            cota.gastar()
            try:
                r = requests.post(
                    cfg["url"].format(chave=chave),
                    json={"keywords": termo, "location": cfg.get("localizacao", "Brasil"),
                          "page": pagina, "ResultOnPage": por_pagina, "datecreatedfrom": desde},
                    headers={**HEADERS, "Content-Type": "application/json",
                             **({"User-Agent": cfg["user_agent"]} if cfg.get("user_agent") else {})},
                    timeout=30)
            except Exception as e:
                print(f"[aviso] Jooble '{termo}': {_sem_segredo(e, chave)}")
                erros += 1
                parar = True          # falha de rede/endereço: não gasta o resto da cota
                break
            if r.status_code != 200:
                print(f"[aviso] Jooble '{termo}': HTTP {r.status_code} - "
                      f"{_sem_segredo(r.text[:200], chave)!r}. Fonte interrompida.")
                erros += 1
                parar = True          # 1ª resposta ruim: não insiste (cada chamada é contada)
                break
            try:
                corpo = r.json()
            except ValueError:
                print(f"[aviso] Jooble '{termo}': resposta não é JSON. Fonte interrompida.")
                erros += 1
                parar = True
                break
            itens = corpo.get("jobs", []) if isinstance(corpo, dict) else []
            if primeira:
                primeira = False
                print(f"[debug] Jooble OK; chaves: {list(corpo.keys()) if isinstance(corpo, dict) else type(corpo)}; "
                      f"totalCount: {corpo.get('totalCount') if isinstance(corpo, dict) else '?'}")
                if itens:
                    print(f"[debug] Jooble campos: {sorted(itens[0].keys())}; "
                          f"location: {itens[0].get('location')!r}; updated: {itens[0].get('updated')!r}")
            for j in itens:
                atualizada = j.get("updated", "")
                if not recente(atualizada, agora):
                    continue
                cidade, estado_nome = separar_local(j.get("location", ""))
                titulo = _sem_html(j.get("title"))
                modelo = _modelo_por_texto(f"{titulo} {_sem_html(j.get('snippet'))} {j.get('type', '')}",
                                           re_remoto, re_hibrido)
                vagas.append({
                    "titulo": titulo,
                    "empresa": _sem_html(j.get("company")),
                    "url": j.get("link", ""),
                    "modelo": modelo,
                    "cidade": cidade,
                    "texto_local": norm(f"{cidade} {j.get('location', '')}").strip(),
                    "estado": norm(estado_nome),
                    "estado_nome": estado_nome,
                    "fonte": "Jooble",
                    "publicada": atualizada,
                })
            if len(itens) < por_pagina:
                break
            time.sleep(1)
        if parar:
            break
        time.sleep(1)
    print(f"[info] Jooble: {len(vagas)} vagas recentes coletadas; cota agora {cota.resumo()}")
    registrar("Jooble", vagas, erros)
    return vagas


def buscar_google(agora, cfg):
    chave = os.environ.get("SERPAPI_KEY", "").strip()
    if not chave:
        print("[info] Google pulada: falta o Secret SERPAPI_KEY.")
        STATUS["Google"] = {"estado": "pulada", "vagas": 0, "erros": 0}
        return []
    cota = Cota("google", cfg, agora)
    re_remoto = regex_de_termos(cfg.get("palavras_remoto", []))
    re_hibrido = regex_de_termos(cfg.get("palavras_hibrido", []))
    vagas, erros, primeira, parar = [], 0, True, False
    print(f"[info] Google: cota usada {cota.resumo()}")
    for termo in cfg.get("buscas", CFG["buscas"]):
        token = None
        for pagina in range(1, cfg.get("paginas_por_busca", 2) + 1):
            if not cota.pode():
                print(f"[aviso] Google: cota de {cota.maximo} buscas neste mês atingida; "
                      f"fonte pausada até o mês que vem.")
                parar = True
                break
            params = {"engine": "google_jobs", "q": termo, "gl": cfg.get("pais", "br"),
                      "hl": cfg.get("idioma", "pt-br"), "api_key": chave}
            if cfg.get("localizacao"):
                params["location"] = cfg["localizacao"]
            if token:
                params["next_page_token"] = token
            cota.gastar()
            try:
                r = requests.get(cfg["url"], params=params, headers=HEADERS, timeout=60)
            except Exception as e:
                print(f"[aviso] Google '{termo}': {_sem_segredo(e, chave)}")
                erros += 1
                parar = True
                break
            if r.status_code != 200:
                print(f"[aviso] Google '{termo}': HTTP {r.status_code} - "
                      f"{_sem_segredo(r.text[:200], chave)!r}. Fonte interrompida.")
                erros += 1
                parar = True
                break
            try:
                corpo = r.json()
            except ValueError:
                print(f"[aviso] Google '{termo}': resposta não é JSON. Fonte interrompida.")
                erros += 1
                parar = True
                break
            if not isinstance(corpo, dict):
                corpo = {}
            if corpo.get("error"):
                msg = _sem_segredo(corpo["error"], chave)
                if "hasn't returned any results" in msg or "no results" in msg.lower():
                    print(f"[debug] Google '{termo}' pág {pagina}: sem resultados ({msg[:120]})")
                    cota.devolver()
                    break             # busca sem resultados: normal
                print(f"[aviso] Google '{termo}': {msg}")
                erros += 1
                break
            itens = corpo.get("jobs_results", []) or []
            if not itens and not corpo.get("error"):
                meta = corpo.get("search_metadata") or {}
                print(f"[debug] Google '{termo}' pág {pagina}: resposta sem jobs_results; "
                      f"chaves: {list(corpo.keys())}; status: {meta.get('status')}")
            if primeira:
                primeira = False
                print(f"[debug] Google OK; chaves: {list(corpo.keys())}")
                if itens:
                    print(f"[debug] Google campos: {sorted(itens[0].keys())}; "
                          f"location: {itens[0].get('location')!r}; "
                          f"extensions: {itens[0].get('extensions')}; "
                          f"detected: {itens[0].get('detected_extensions')}")
            for j in itens:
                ext = j.get("detected_extensions") or {}
                lista_ext = [str(x) for x in (j.get("extensions") or []) if x]
                data = parse_relativo(ext.get("posted_at", ""), agora)
                if data is None:                      # a data vem como texto solto na lista
                    for item in lista_ext:
                        data = parse_relativo(item, agora)
                        if data:
                            break
                if data and not recente(data.isoformat(), agora):
                    continue
                apply = [a.get("link") for a in (j.get("apply_options") or []) if a.get("link")]
                link = apply[0] if apply else j.get("share_link", "")
                titulo = _sem_html(j.get("title") or j.get("job_title"))
                loc = j.get("location", "")
                cidade, estado_nome = separar_local(loc)
                modelo = ("remote" if ext.get("work_from_home") else
                          _modelo_por_texto(f"{titulo} {loc} {' '.join(lista_ext)}",
                                            re_remoto, re_hibrido))
                vagas.append({
                    "titulo": titulo,
                    "empresa": _sem_html(j.get("company_name")),
                    "url": link,
                    "modelo": modelo,
                    "cidade": cidade,
                    "texto_local": norm(f"{cidade} {loc}").strip(),
                    "estado": norm(estado_nome),
                    "estado_nome": estado_nome,
                    "fonte": "Google",
                    "publicada": data.isoformat() if data else "",
                    "data_incerta": data is None,   # sem data confiável: só no site
                })
            token = (corpo.get("serpapi_pagination") or {}).get("next_page_token")
            if not token or not itens:
                break
            time.sleep(1)
        if parar:
            break
    print(f"[info] Google: {len(vagas)} vagas coletadas; cota agora {cota.resumo()}")
    registrar("Google", vagas, erros)
    return vagas


# --------------- CARREIRAS DE EMPRESAS (lista monitorada no config.json) ---------------
def _detectar_sistema(url):
    """Descobre o sistema de recrutamento pelo endereço: (tipo, identificador)."""
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    partes = [p for p in u.path.split("/") if p]
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and partes:
        return "lever", partes[0]
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and partes:
        return "greenhouse", partes[0]
    if host == "jobs.ashbyhq.com" and partes:
        return "ashby", partes[0]
    if host == "apply.workable.com" and partes:
        return "workable", partes[0]
    if host.endswith(".recruitee.com"):
        return "recruitee", host.split(".")[0]
    return "pagina", url.strip()


def _ms_para_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, TZ).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _vaga_de_empresa(nome, titulo, url, local, modelo, data):
    cidade, estado_nome = separar_local(local)
    return {
        "titulo": _sem_html(titulo), "empresa": nome, "url": url or "", "modelo": modelo,
        "cidade": cidade, "texto_local": norm(f"{cidade} {local}").strip(),
        "estado": norm(estado_nome), "estado_nome": estado_nome,
        "fonte": "Carreiras", "publicada": data or "", "data_incerta": not data,
    }


def _jsonld_vagas(html):
    """Acha blocos JSON-LD do tipo JobPosting dentro do HTML de uma página."""
    achados = []

    def visitar(no):
        if isinstance(no, list):
            for x in no:
                visitar(x)
        elif isinstance(no, dict):
            tipo = no.get("@type")
            tipos = tipo if isinstance(tipo, list) else [tipo]
            if "JobPosting" in tipos:
                achados.append(no)
            for k in ("@graph", "itemListElement", "item", "mainEntity"):
                if k in no:
                    visitar(no[k])
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            visitar(json.loads(m.group(1).strip()))
        except ValueError:
            continue
    return achados


def _empresa_lever(nome, slug, agora, re_remoto, re_hibrido):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    itens = r.json()
    if not isinstance(itens, list):
        raise ValueError("resposta do Lever não é uma lista")
    vagas = []
    for j in itens:
        cat = j.get("categories") or {}
        loc = cat.get("location", "") or ""
        wp = norm(j.get("workplaceType", ""))
        modelo = ("remote" if wp == "remote" else "hybrid" if wp == "hybrid" else
                  _modelo_por_texto(f"{j.get('text', '')} {loc}", re_remoto, re_hibrido))
        vagas.append(_vaga_de_empresa(nome, j.get("text"), j.get("hostedUrl"), loc, modelo,
                                      _ms_para_iso(j.get("createdAt"))))
    return vagas, itens


def _empresa_greenhouse(nome, slug, agora, re_remoto, re_hibrido):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    itens = (r.json() or {}).get("jobs", [])
    vagas = []
    for j in itens:
        loc = (j.get("location") or {}).get("name", "") or ""
        data = j.get("first_published") or j.get("updated_at") or ""
        vagas.append(_vaga_de_empresa(nome, j.get("title"), j.get("absolute_url"), loc,
                                      _modelo_por_texto(f"{j.get('title', '')} {loc}", re_remoto, re_hibrido),
                                      data))
    return vagas, itens


def _empresa_ashby(nome, slug, agora, re_remoto, re_hibrido):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    itens = (r.json() or {}).get("jobs", [])
    vagas = []
    for j in itens:
        loc = j.get("location", "") or ""
        modelo = ("remote" if j.get("isRemote") else
                  _modelo_por_texto(f"{j.get('title', '')} {loc}", re_remoto, re_hibrido))
        vagas.append(_vaga_de_empresa(nome, j.get("title"), j.get("jobUrl"), loc, modelo,
                                      j.get("publishedAt") or ""))
    return vagas, itens


def _empresa_workable(nome, slug, agora, re_remoto, re_hibrido):
    r = requests.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
                      json={"query": "", "location": [], "department": [], "worktype": [], "remote": []},
                      headers={**HEADERS, "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    itens = (r.json() or {}).get("results", [])
    vagas = []
    for j in itens:
        l = j.get("location") or {}
        loc = ", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
        modelo = ("remote" if j.get("workplace") == "remote" or j.get("remote") else
                  "hybrid" if j.get("workplace") == "hybrid" else
                  _modelo_por_texto(f"{j.get('title', '')} {loc}", re_remoto, re_hibrido))
        vagas.append(_vaga_de_empresa(nome, j.get("title"),
                                      f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
                                      loc, modelo, j.get("published") or ""))
    return vagas, itens


def _empresa_recruitee(nome, slug, agora, re_remoto, re_hibrido):
    r = requests.get(f"https://{slug}.recruitee.com/api/offers/", headers=HEADERS, timeout=30)
    r.raise_for_status()
    itens = (r.json() or {}).get("offers", [])
    vagas = []
    for j in itens:
        loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        modelo = "remote" if j.get("remote") else _modelo_por_texto(
            f"{j.get('title', '')} {loc}", re_remoto, re_hibrido)
        vagas.append(_vaga_de_empresa(nome, j.get("title"), j.get("careers_url"), loc, modelo,
                                      j.get("created_at") or ""))
    return vagas, itens


_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
          "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}


def _data_publicada(html):
    """Acha 'Publicada em 03 de Novembro de 2026' no texto de uma página de vaga."""
    m = re.search(r"publicad[ao]\s+em\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", norm(_sem_html(html)))
    if not m or m.group(2) not in _MESES:
        return ""
    try:
        return f"{int(m.group(3)):04d}-{_MESES[m.group(2)]:02d}-{int(m.group(1)):02d}"
    except ValueError:
        return ""


def _titulo_da_pagina(html):
    """Título da vaga numa página: og:title, depois <h1>, depois <title> (sem o ' | Empresa')."""
    for padrao in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                   r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                   r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(padrao, html or "", re.S | re.I)
        if m:
            t = re.sub(r"\s+", " ", _sem_html(m.group(1))).strip()
            t = re.split(r"\s+[|·]\s+", t)[0].strip()
            if t:
                return t[:120]
    return ""


_RE_CTA = re.compile(r"^(confira|ver|veja|saiba|candidat|aplic|inscrev|acesse|clique|leia|conheca|apply|view|see)\b")


class _Navegador:
    """Navegador sem janela (Playwright) para páginas que montam a lista por JavaScript.
    Só abre quando alguma empresa tem "javascript": true no config.json."""
    def __init__(self):
        self._pw = self._browser = self._ctx = None

    def _abrir(self):
        if self._ctx:
            return
        from playwright.sync_api import sync_playwright      # só importa se for usar
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(locale="pt-BR", user_agent=HEADERS["User-Agent"])

    def html(self, url, espera_ms=1500):
        self._abrir()
        pagina = self._ctx.new_page()
        try:
            pagina.goto(url, wait_until="networkidle", timeout=45000)
            pagina.wait_for_timeout(espera_ms)
            return pagina.content()
        finally:
            pagina.close()

    def fechar(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = None


def _obter_html(url, nav=None):
    if nav is not None:
        return nav.html(url)
    r = requests.get(url, headers={**HEADERS, "Accept": "text/html"}, timeout=30)
    r.raise_for_status()
    return r.text


def _vagas_por_links(nome, url, html, agora, re_remoto, re_hibrido, max_paginas=15, nav=None):
    """Página-lista sem código padrão: pega os links cujo texto parece vaga de dados e abre cada
    um só para ler a data de publicação. O modelo (remoto etc.) NÃO é deduzido do texto da página,
    porque benefícios como 'auxílio home office' enganariam; só vale o que está no título."""
    from urllib.parse import urljoin
    candidatos, genericos, vistos = [], [], set()
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or "", re.S | re.I):
        href = urljoin(url, m.group(1).strip()).split("#")[0]
        texto = re.sub(r"\s+", " ", _sem_html(m.group(2))).strip()
        if (not href.startswith(("http://", "https://")) or href in vistos or not texto
                or len(texto) > 120 or href.rstrip("/") == url.rstrip("/")):
            continue
        if e_vaga_de_dados(texto):
            vistos.add(href)
            candidatos.append((texto, href))
        elif _RE_CTA.match(norm(texto)) and len(texto) <= 40:     # "Confira a vaga": título fica fora do link
            vistos.add(href)
            genericos.append(href)
    for href in genericos[:25]:                                    # abre a página e lê o título dela
        try:
            try:
                pag = _obter_html(href)
                titulo = _titulo_da_pagina(pag)
                if not titulo and nav is not None:
                    titulo = _titulo_da_pagina(_obter_html(href, nav))
            except Exception:
                titulo = ""
        except Exception:
            titulo = ""
        if titulo and e_vaga_de_dados(titulo):
            candidatos.append((titulo, href))
        time.sleep(1)
    todos = re.findall(r"<a\s", html or "", re.I)
    print(f"[debug] Carreiras - {nome}: página com {len(html or '')} caracteres, {len(todos)} links, "
          f"{len(candidatos)} parecem vaga de dados ({len(genericos)} links genéricos abertos); ld+json: {'sim' if 'ld+json' in (html or '') else 'não'}")
    if not candidatos:
        amostra = [re.sub(r"\s+", " ", _sem_html(m.group(1))).strip()[:50]
                   for m in re.finditer(r'<a\s[^>]*>(.*?)</a>', html or "", re.S | re.I)][:12]
        print(f"[debug] Carreiras - {nome}: primeiros textos de link: {amostra}")
    vagas = []
    for texto, href in candidatos[:max_paginas]:
        data = ""
        try:
            data = _data_publicada(_obter_html(href))
            if not data and nav is not None:        # a data pode ser montada por JavaScript
                data = _data_publicada(_obter_html(href, nav))
        except Exception:
            pass
        vagas.append(_vaga_de_empresa(nome, texto, href, "",
                                      _modelo_por_texto(texto, re_remoto, re_hibrido), data))
        time.sleep(1)
    return vagas, candidatos


def _empresa_pagina(nome, url, agora, re_remoto, re_hibrido, nav=None):
    html = _obter_html(url, nav)
    itens = _jsonld_vagas(html)
    if not itens:
        return _vagas_por_links(nome, url, html, agora, re_remoto, re_hibrido, nav=nav)
    vagas = []
    for j in itens:
        end = ""
        loc_raw = j.get("jobLocation")
        loc_raw = loc_raw[0] if isinstance(loc_raw, list) and loc_raw else loc_raw
        if isinstance(loc_raw, dict):
            a = loc_raw.get("address") or {}
            if isinstance(a, dict):
                end = ", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x)
        remoto = "TELECOMMUTE" in str(j.get("jobLocationType", "")).upper()
        modelo = "remote" if remoto else _modelo_por_texto(
            f"{j.get('title', '')} {end}", re_remoto, re_hibrido)
        org = j.get("hiringOrganization")
        vagas.append(_vaga_de_empresa(nome, j.get("title"), j.get("url") or url, end, modelo,
                                      j.get("datePosted") or ""))
    return vagas, itens


_LEITORES = {"lever": _empresa_lever, "greenhouse": _empresa_greenhouse, "ashby": _empresa_ashby,
             "workable": _empresa_workable, "recruitee": _empresa_recruitee, "pagina": _empresa_pagina}


def buscar_empresas(agora, cfg):
    """Lê a página de vagas de cada empresa da lista do config.json."""
    lista = [e for e in cfg.get("lista", []) if isinstance(e, dict) and e.get("url")]
    re_remoto = regex_de_termos(cfg.get("palavras_remoto", []))
    re_hibrido = regex_de_termos(cfg.get("palavras_hibrido", []))
    vagas, erros = [], 0
    nav = _Navegador() if any(e.get("javascript") for e in lista) else None
    for e in lista:
        nome = e.get("nome") or e["url"]
        tipo, ident = _detectar_sistema(e["url"])
        try:
            if tipo == "pagina":
                achadas, brutas = _empresa_pagina(nome, e["url"], agora, re_remoto, re_hibrido,
                                                  nav=nav if e.get("javascript") else None)
            else:
                achadas, brutas = _LEITORES[tipo](nome, ident, agora, re_remoto, re_hibrido)
        except ImportError:
            print(f"[aviso] Carreiras - {nome}: falta o Playwright (pip install playwright e "
                  f"playwright install chromium). Empresa pulada.")
            erros += 1
            continue
        except Exception as ex:
            print(f"[aviso] Carreiras - {nome} ({tipo}): {ex}")
            erros += 1
            continue
        recentes = [v for v in achadas if v["url"].startswith(("http://", "https://"))
                    and recente(v["publicada"], agora)]
        de_dados = [v for v in recentes if e_vaga_de_dados(v["titulo"])]
        print(f"[debug] Carreiras - {nome} ({tipo}): {len(brutas)} vagas na página, "
              f"{len(recentes)} recentes, {len(de_dados)} de dados")
        if tipo == "pagina" and not brutas:
            print(f"[aviso] Carreiras - {nome}: não achei vagas de dados nem o código padrão de vaga "
                  f"(JobPosting) na página; ela provavelmente carrega as vagas por JavaScript. Veja se "
                  f"a empresa usa Greenhouse, Lever, Ashby, Workable ou Recruitee e troque o endereço.")
        vagas += recentes
        time.sleep(1)
    if nav is not None:
        nav.fechar()
    print(f"[info] Carreiras: {len(vagas)} vagas recentes de {len(lista)} empresas")
    registrar("Carreiras", vagas, erros)
    return vagas


# --------------------------- VAGAS MANUAIS ------------------------------
def _data_manual(txt, agora):
    txt = str(txt or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        return f"{txt}T12:00:00-03:00"
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", txt)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}T12:00:00-03:00"
    return agora.isoformat()


def _modelo_manual(txt, cidade):
    n = norm(str(txt or ""))
    if n in ("remoto", "remote"):
        return "remote"
    if n in ("hibrido", "hybrid"):
        return "hybrid"
    if n in ("presencial", "on-site", "onsite"):
        return "on-site"
    return "on-site" if cidade else "remote"


def carregar_manuais(agora):
    """Vagas coladas à mão em vagas_manuais.json (não passam pelo filtro de título)."""
    if not ARQ_MANUAIS.exists():
        return []
    try:
        dados = json.loads(ARQ_MANUAIS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[aviso] {ARQ_MANUAIS.name} tem erro na linha {e.lineno}, coluna {e.colno} "
              f"({e.msg}). As vagas manuais foram ignoradas nesta execução.")
        STATUS["Manual"] = {"estado": "falhou", "vagas": 0, "erros": 1}
        return []
    itens = dados.get("vagas", []) if isinstance(dados, dict) else dados
    vagas, erros = [], 0
    for i, j in enumerate(itens, 1):
        if not isinstance(j, dict):
            continue
        titulo = str(j.get("titulo", "")).strip()
        url = str(j.get("link") or j.get("url") or "").strip()
        if not titulo or not url.startswith(("http://", "https://")):
            print(f"[aviso] vaga manual #{i} ignorada: precisa de 'titulo' e de um 'link' que comece com http.")
            erros += 1
            continue
        cidade = str(j.get("cidade", "")).strip()
        estado_nome = str(j.get("estado", "")).strip()
        nivel = j.get("nivel")
        vagas.append({
            "titulo": titulo,
            "empresa": str(j.get("empresa", "")).strip(),
            "url": url,
            "modelo": _modelo_manual(j.get("modelo"), cidade),
            "cidade": cidade,
            "texto_local": norm(cidade).strip(),
            "estado": norm(estado_nome),
            "estado_nome": estado_nome,
            "fonte": "Manual",
            "publicada": _data_manual(j.get("data"), agora),
            "manual": True,
            "nivel_manual": nivel if nivel in NIVEIS_FIXOS + (NIVEL_PADRAO,) else None,
            "area_manual": str(j["area"]).strip() if j.get("area") else None,
        })
    if itens or erros:
        print(f"[info] Manual: {len(vagas)} vagas lidas de {ARQ_MANUAIS.name}")
        STATUS["Manual"] = {"estado": "parcial" if erros and vagas else "falhou" if erros else "ok",
                            "vagas": len(vagas), "erros": erros}
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
         "total": len(vagas), "regioes": list(REGIOES), "fontes": STATUS, "vagas": vagas},
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


FONTES = (("Gupy", "gupy", buscar_gupy),
          ("Adzuna", "adzuna", buscar_adzuna),
          ("Jooble", "jooble", buscar_jooble),
          ("Google", "google", buscar_google),
          ("Remotive", "remotive", buscar_remotive),
          ("Carreiras", "empresas", buscar_empresas))


def main():
    try:
        iniciar_config()
    except ConfigInvalida as e:
        print(f"[ERRO] {e}")
        print("[ERRO] Nada foi alterado. Corrija o arquivo e rode de novo.")
        raise SystemExit(1)

    agora = datetime.now(TZ)
    STATUS.clear()
    PASTA_VAGAS.mkdir(exist_ok=True)
    limpar_txt_antigos(agora)

    brutas = carregar_manuais(agora)          # manuais primeiro: vencem repetidas
    for nome, chave, buscar in FONTES:
        cfg = CFG["fontes"][chave]
        if not cfg.get("ativa", True):
            print(f"[info] {nome} desativada no config.json.")
            STATUS[nome] = {"estado": "desativada", "vagas": 0, "erros": 0}
            continue
        brutas += buscar(agora, cfg)
    print(f"[info] {len(brutas)} vagas brutas coletadas")

    vistas = limpar_vistas(carregar_vistas(), agora)
    novas, todas, chaves = [], [], set()
    fora_da_regiao = sem_local = 0
    for v in brutas:
        manual = v.get("manual", False)
        if not v["url"].startswith(("http://", "https://")):
            continue
        if not manual and not e_vaga_de_dados(v["titulo"]):
            continue
        # mesma vaga em cidades diferentes conta como vagas diferentes
        chave = norm(v["titulo"] + v["empresa"] + v["cidade"])
        if chave in chaves:
            continue
        chaves.add(chave)
        v["nivel"] = v.get("nivel_manual") or classificar_nivel(v["titulo"])
        v["area"] = v.get("area_manual") or classificar_area(v["titulo"])

        # região: remoto = "Remoto"; presencial/híbrido = região da comunidade ou "Outras regiões"
        vai_pro_whats = True
        if v["modelo"] == "remote":
            v["regiao"] = "Remoto"
        else:
            texto = v["texto_local"] or norm(v["titulo"])  # sem local: tenta o título
            regiao = achar_regiao(texto, v["estado"])
            v["regiao"] = regiao or "Outras regiões"
            if not regiao and not manual:
                vai_pro_whats = False
                if v["texto_local"]:
                    fora_da_regiao += 1
                else:
                    sem_local += 1
        if SOMENTE_REMOTO and v["modelo"] != "remote" and not manual:
            vai_pro_whats = False
        if not manual and not recente(v.get("publicada"), agora, DIAS_VAGA_RECENTE):
            vai_pro_whats = False   # vaga mais antiga: só no site
        if v.get("data_incerta"):
            vai_pro_whats = False   # sem data confiável (Google): só no site

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