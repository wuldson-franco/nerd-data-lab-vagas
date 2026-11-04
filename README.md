# Nerd Data Lab: radar de vagas de dados

Um robô (GitHub Actions) busca vagas de dados às segundas e quintas, gera um site
com filtros (GitHub Pages) e um texto pronto para o WhatsApp.

## Onde mexer, sem tocar no código

| Quero... | Arquivo | O que fazer |
|---|---|---|
| Trocar o endereço de uma fonte que quebrou | `config.json` → `fontes` | Edite o campo `url` |
| Desligar uma fonte | `config.json` → `fontes` | `"ativa": false` |
| Incluir um termo de busca | `config.json` → `buscas` | Acrescente o termo à lista |
| Incluir uma cidade na minha região | `config.json` → `regioes` | Acrescente à lista `cidades` |
| Mudar quantos dias de vaga entram | `config.json` → `prazos_em_dias` | Troque o número |
| Ajustar como o nível ou a área é detectado | `config.json` → `niveis` / `areas` | Edite os termos |
| Acompanhar as vagas de uma empresa | `config.json` → `fontes` → `empresas` → `lista` | Acrescente `{"nome": "Empresa", "url": "endereço da página de vagas"}` |
| Incluir uma vaga que vi por aí | `vagas_manuais.json` | Copie o `_exemplo` para dentro de `vagas` |
| Mudar os dias da semana do robô | `.github/workflows/vagas.yml` | Linha `cron` |

Em todos os casos: abra o arquivo no GitHub, clique no lápis e depois em **Commit changes**.
Cuidado com as vírgulas: todo item de uma lista, menos o último, termina com vírgula.
Se um erro de formato passar, o robô avisa: no `config.json` ele para sem alterar nada
(o e-mail de falha do GitHub chega); no `vagas_manuais.json` ele ignora o arquivo e o site
mostra um aviso.

## Como os dados chegam ao site

O robô grava `docs/data/vagas.json`. O site lê esse arquivo e filtra no navegador.
Qualquer pessoa pode ler o mesmo arquivo em
`https://SEU_USUARIO.github.io/nerd-data-lab-vagas/data/vagas.json`.

Campos de cada vaga: `titulo`, `empresa`, `url`, `nivel`, `area`, `modelo`
(`remote`, `hybrid` ou `on-site`), `cidade`, `estado`, `regiao`, `fonte`,
`publicada` (AAAA-MM-DD) e `visto_em`. Além da lista, o arquivo traz `atualizado_em`,
`regioes` e `fontes` (situação de cada fonte na última execução).

## Chaves de API

Gupy e Remotive não precisam de chave. As outras fontes precisam, e as chaves ficam
**só** nos Secrets do GitHub, nunca no código nem coladas em conversa:
**Settings → Secrets and variables → Actions → New repository secret**.

| Fonte | Secret | Limite do plano gratuito |
|---|---|---|
| Jooble | `JOOBLE_API_KEY` | 500 chamadas **no total** (não renova) |
| Google Vagas (via SerpAPI) | `SERPAPI_KEY` | 250 buscas por mês |
| Adzuna (desligada) | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | só 25 chamadas de teste no plano grátis |

Sem a chave, a fonte é pulada e o resto funciona. O robô conta as chamadas em
`uso_apis.json` e pausa a fonte ao chegar em `limite.maximo` (no `config.json`),
sempre deixando folga. Com o ritmo atual (2 execuções por semana), o Jooble dura mais
de um ano e o Google fica bem abaixo dos 250 por mês.

As vagas do Google não trazem data confiável: quando não dá para saber, elas aparecem
só no site e não entram no texto do WhatsApp.

## Empresas monitoradas

A lista em `config.json` → `fontes` → `empresas` faz o robô visitar a página de vagas de cada
empresa. Lever, Greenhouse, Ashby, Workable e Recruitee são lidos pela API aberta deles; outras
páginas, pelo código padrão de vaga (JobPosting), quando existe. Só entram vagas de dados.
No log do Actions, cada empresa mostra quantas vagas tinha e quantas eram de dados; se uma
página não puder ser lida, o log diz e o site mostra o aviso amarelo.

## Vagas encerradas

Antes de entrar no texto do WhatsApp, o robô abre o link de cada vaga nova e descarta se estiver
encerrada (página não encontrada ou texto como "candidaturas encerradas"). Na Gupy, vagas com prazo
vencido nem entram. A cada execução ele também confere uma parte das vagas do site que não
apareceram hoje e remove as encerradas. Se der erro de rede, a vaga fica. Ajustes em
`config.json` → `verificar_encerradas` (`"ativa": false` desliga).

## Histórico completo (banco de dados)

`dados/vagas_historico.csv` guarda **todas** as vagas já vistas e nunca apaga linhas. Colunas:
`id`, `titulo`, `empresa`, `url`, `nivel`, `area`, `modelo`, `cidade`, `estado`, `regiao`, `fonte`,
`publicada`, `primeira_vez_visto`, `ultima_vez_visto`, `status` e, quando encerrada,
`encerrada_em` e `motivo_encerrada`. O `status` é `aberta` (vista na última execução),
`nao_vista` (sumiu das fontes) ou `encerrada` (confirmada pela verificação dos links).
No Excel, abra com Dados → De Texto/CSV (codificação UTF-8). No Power BI, Obter dados → Texto/CSV.
Na primeira execução ele já nasce com as vagas que o site tinha (a data da primeira vez dessas é aproximada).

## O que continua no código

A leitura da resposta de cada fonte (qual campo é o título, qual é a cidade) está em
`buscar_vagas.py`. Quando uma fonte muda o formato da resposta, é aí que se ajusta.