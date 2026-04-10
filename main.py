# -*- coding: utf-8 -*-
"""
Bitbucket PR → Jira → Claude Code Review Bot
=============================================
Nasazení: Railway / Render / vlastní server
Požadavky: viz requirements.txt
"""

from __future__ import annotations

import re
import os
import json
import asyncio
import fnmatch
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# Cache zpracovaných PR – zabraňuje duplicitnímu review při Bitbucket retry
import time
_processed_prs: dict[str, float] = {}
DEDUP_TTL = 900  # 15 minut

def _is_already_processed(key: str) -> bool:
    if key in _processed_prs:
        if time.time() - _processed_prs[key] < DEDUP_TTL:
            return True
        del _processed_prs[key]
    return False

def _mark_as_processed(key: str) -> None:
    _processed_prs[key] = time.time()
    cutoff = time.time() - DEDUP_TTL
    for k in list(_processed_prs.keys()):
        if _processed_prs[k] < cutoff:
            del _processed_prs[k]

# ---------------------------------------------------------------------------
# Konfigurace – nastav jako Environment Variables (nikdy nekládej klíče do kódu!)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Tokeny se načítají automaticky z environment variables s prefixem BB_TOKEN_
# Stačí přidat do Railway: BB_TOKEN_BIESSE_WEB, BB_TOKEN_JIP_SHOP atd.
# Název proměnné: BB_TOKEN_ + název repo VELKÝMI PÍSMENY s pomlčkami nahrazenými podtržítky
# Příklad: repo "biesse-web" → BB_TOKEN_BIESSE_WEB
def _load_bb_tokens() -> dict[str, str]:
    tokens = {}
    for key, value in os.environ.items():
        if key.startswith("BB_TOKEN_") and value:
            # BB_TOKEN_BIESSE_WEB → biesse-web
            repo_slug = key[len("BB_TOKEN_"):].lower().replace("_", "-")
            tokens[repo_slug] = value
    return tokens

BB_TOKENS = _load_bb_tokens()

JIRA_BASE_URL  = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL     = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# ---------------------------------------------------------------------------
# PoC Konfigurace — upravuj zde
# ---------------------------------------------------------------------------

# Maximální počet změněných řádků — PR nad tento limit se NEPOŠLE na Claude
POC_MAX_LINES = 1500

# Soubory které se ignorují při počítání řádků i při review
# Důvod: automaticky generované soubory, nemá smysl je reviewovat
IGNORED_FILES = [
    "package-lock.json",    # npm závislosti — generováno automaticky
    "yarn.lock",            # yarn závislosti — generováno automaticky
    "pnpm-lock.yaml",       # pnpm závislosti — generováno automaticky
    "composer.lock",        # PHP závislosti — generováno automaticky
    "Gemfile.lock",         # Ruby závislosti — generováno automaticky
    "poetry.lock",          # Python závislosti — generováno automaticky
    "*.min.js",             # minifikované JS soubory
    "*.min.css",            # minifikované CSS soubory
]

# re.IGNORECASE = funguje i pro malá písmena (jip-357 → JIP-357)
JIRA_ID_PATTERN = re.compile(r"([A-Z]{2,10}-\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pomocné funkce — filtrování diffu
# ---------------------------------------------------------------------------

def should_ignore_file(filename: str) -> bool:
    """
    Rozhodne zda soubor ignorovat.
    Porovnává přesný název i vzory s hvězdičkou (*.min.js).
    """
    for pattern in IGNORED_FILES:
        if filename == pattern:
            return True
        if fnmatch.fnmatch(filename, pattern):
            return True
        # Kontrola i jen názvu souboru bez cesty (path/to/package-lock.json)
        basename = filename.split("/")[-1]
        if basename == pattern or fnmatch.fnmatch(basename, pattern):
            return True
    return False


def filter_diff(raw_diff: str) -> tuple[str, list[str]]:
    """
    Odfiltruje ignorované soubory z diffu.
    Vrátí: (filtrovaný diff, seznam ignorovaných souborů)
    """
    ignored = []
    filtered_blocks = []
    current_block = []
    current_file = None
    skip_current = False

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            # Ulož předchozí blok pokud nebyl ignorován
            if current_block and not skip_current:
                filtered_blocks.extend(current_block)

            # Zjisti název souboru: "diff --git a/foo.js b/foo.js" → "foo.js"
            parts = line.strip().split(" ")
            current_file = parts[-1].lstrip("b/") if len(parts) >= 4 else ""
            skip_current = should_ignore_file(current_file)

            if skip_current and current_file:
                ignored.append(current_file)

            current_block = [line]
        else:
            current_block.append(line)

    # Zpracuj poslední blok
    if current_block and not skip_current:
        filtered_blocks.extend(current_block)

    return "".join(filtered_blocks), ignored


def count_changed_lines(diff: str) -> int:
    """
    Spočítá počet změněných řádků v diffu.
    Počítá řádky začínající + nebo - (ale ne +++ a --- které jsou hlavičky).
    """
    count = 0
    for line in diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or \
           (line.startswith("-") and not line.startswith("---")):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Detekce stack verzi — Angular a .NET
# ---------------------------------------------------------------------------

async def get_angular_version(workspace: str, repo_slug: str, token: str) -> str | None:
    """Načte hlavní verzi Angularu z package.json v repozitáři."""
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/src/HEAD/package.json"
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if not resp.is_success:
                return None
            pkg = resp.json()
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            version = deps.get("@angular/core", "")
            match = re.search(r"(\d+)", version)
            return match.group(1) if match else None
        except Exception:
            return None


async def get_dotnet_version(workspace: str, repo_slug: str, token: str) -> str | None:
    """Načte verzi .NET z .csproj souboru — prohledá root repozitáře."""
    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/src/HEAD/"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if not resp.is_success:
                return None
            files = resp.json().get("values", [])
            csproj = next(
                (f["path"] for f in files if f["path"].endswith(".csproj")), None
            )
            if not csproj:
                return None

            proj_url = (
                f"https://api.bitbucket.org/2.0/repositories/"
                f"{workspace}/{repo_slug}/src/HEAD/{csproj}"
            )
            proj_resp = await client.get(
                proj_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if not proj_resp.is_success:
                return None

            content = proj_resp.text
            # <TargetFramework>net8.0</TargetFramework> nebo <TargetFrameworkVersion>v4.8</TargetFrameworkVersion>
            match = re.search(
                r"<TargetFramework(?:Version)?>(.*?)</TargetFramework(?:Version)?>",
                content,
            )
            return match.group(1).strip() if match else None
        except Exception:
            return None


def _angular_note(version: str | None) -> str:
    """Vrátí kontext pro Claude podle verze Angularu."""
    if not version:
        return ""
    v = int(version)
    if v <= 8:
        return (
            f"Angular {v} — NgModules architektura, žádné standalone komponenty, "
            f"HttpClient přes HttpClientModule, RxJS pipeable operators"
        )
    elif v <= 12:
        return (
            f"Angular {v} — přechodné období, Ivy renderer (možná ještě ViewEngine), "
            f"NgModules jako standard, žádné Signals"
        )
    elif v <= 16:
        return (
            f"Angular {v} — Ivy, standalone komponenty jako opt-in (ne default), "
            f"žádné Signals, inject() funkce dostupná"
        )
    elif v == 17:
        return (
            f"Angular {v} — standalone komponenty jako DEFAULT, "
            f"Signals jako developer preview, nový @if/@for control flow jako opt-in"
        )
    else:
        return (
            f"Angular {v} — Signals stabilní a preferované před RxJS pro lokální stav, "
            f"@if/@for/@switch jako standard (ne *ngIf/*ngFor), "
            f"standalone komponenty jako výchozí"
        )


def _dotnet_note(version: str | None) -> str:
    """Vrátí kontext pro Claude podle verze .NET."""
    if not version:
        return ""
    if "v4." in version or "net4" in version:
        return (
            f".NET Framework {version} — "
            f"bez ConfigureAwait(false) hrozí deadlock v synchronizačním kontextu, "
            f"HttpClient musí být static/singleton (není IHttpClientFactory), "
            f"žádné record types ani pattern matching, "
            f"Entity Framework 6 (NE EF Core) — lazy loading je DEFAULT ZAPNUTÝ pozor na N+1, "
            f"async void je problém mimo event handlery"
        )
    major = re.search(r"net(\d+)", version)
    v = int(major.group(1)) if major else 0
    if v >= 8:
        return (
            f".NET {v} — primary constructors, collection expressions, frozen collections, "
            f"IHttpClientFactory jako standard, EF Core s lazy loading DEFAULT VYPNUTÝM"
        )
    return (
        f".NET {v} — moderní async/await, IHttpClientFactory, "
        f"EF Core s lazy loading DEFAULT VYPNUTÝM"
    )


# ---------------------------------------------------------------------------
# Pomocné funkce — Bitbucket, Jira, Claude
# ---------------------------------------------------------------------------

def extract_jira_id(text: str) -> str | None:
    """Vytáhne první Jira ID z textu a převede na velká písmena."""
    match = JIRA_ID_PATTERN.search(text or "")
    return match.group(1).upper() if match else None


async def get_bitbucket_diff(diff_url: str, token: str) -> str:
    """Stáhne unified diff PR přímo z URL z Bitbucket payloadu."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            diff_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text


async def get_jira_ticket(jira_id: str) -> dict:
    """Načte detail Jira ticketu (název, popis, acceptance criteria)."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{jira_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
        fields = data.get("fields", {})
        description = _extract_text(fields.get("description"))
        ac = _extract_text(fields.get("customfield_10016"))  # uprav podle vaší instance Jiry!
        return {
            "id": jira_id,
            "summary": fields.get("summary", ""),
            "description": description,
            "acceptance_criteria": ac,
            "issue_type": fields.get("issuetype", {}).get("name", ""),
        }


def _extract_text(field) -> str:
    """Převede Atlassian Document Format (ADF) nebo plain string na text."""
    if not field:
        return ""
    if isinstance(field, str):
        return field
    texts = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(field)
    return " ".join(texts).strip()


def build_prompt(
    diff: str,
    jira: dict,
    pr_title: str,
    line_count: int,
    ignored_files: list[str],
    angular_version: str | None = None,
    dotnet_version: str | None = None,
) -> str:
    """Sestaví prompt pro Claude — včetně stack kontextu a filtrovaných souborů."""
    jira_section = ""
    if jira:
        jira_section = f"""
## Kontext Jira ticketu ({jira['id']})
**Typ:** {jira['issue_type']}
**Název:** {jira['summary']}
**Popis:** {jira['description'] or '(není)'}
**Acceptance criteria:** {jira['acceptance_criteria'] or '(není)'}
"""

    filter_note = ""
    if ignored_files:
        filter_note = f"\n> ℹ️ Automaticky ignorované soubory (generované, bez review): {', '.join(ignored_files)}\n"

    # Stack kontext — přidá se pouze pokud se podařilo detekovat verzi
    angular_ctx = _angular_note(angular_version)
    dotnet_ctx  = _dotnet_note(dotnet_version)

    stack_section = ""
    if angular_ctx:
        stack_section += f"- Pro Angular: {angular_ctx}\n"
    else:
        stack_section += (
            "- Pro Angular (verze nezjištěna): sleduj OnPush change detection, "
            "memory leaky v subscriptions (chybějící unsubscribe/takeUntil), přímé DOM manipulace\n"
        )
    if dotnet_ctx:
        stack_section += f"- Pro .NET: {dotnet_ctx}\n"
    else:
        stack_section += (
            "- Pro .NET (verze nezjištěna): sleduj async/await správnost, "
            "IDisposable, N+1 dotazy v ORM\n"
        )

    diff_preview = diff[:16000] + ("\n...[diff zkrácen]" if len(diff) > 16000 else "")

    return f"""Jsi principal software engineer s 20+ lety zkušeností na produkčních systémech s miliony uživatelů.
Tvým úkolem je code review z pohledu člověka, který bude tento kód udržovat za 2 roky v noci při výpadku produkce.
Buď přísný, konkrétní a nelítostný — ale spravedlivý. Nevymýšlej problémy, ale žádný skutečný problém nepřehlédni.

Při review se ptej:
- Bude tento kód čitelný za 2 roky bez původního autora?
- Co se stane když tato funkce dostane 10x více requestů?
- Kde jsou skryté memory leaky, race conditions nebo N+1 dotazy?
- Co rozbije první deployment v pátek v 17:00?
- Jsou edge cases ošetřeny nebo jen happy path?
- Je kód testovatelný? Lze ho mockovat a unit testovat?
- Vzniká technický dluh který bude za rok bolet?

Zásady pro tvoje hodnocení:
- Pokud něco nevíš nebo nemáš dostatečný kontext, napiš to explicitně — NEVYMÝŠLEJ.
- Pokud diff ukazuje jen část souboru a nevidíš celý kontext, uveď to jako omezení.
- Raději méně konkrétních nálezů než mnoho vágních spekulací.
- Nezmiňuj obecné "best practices" pokud nejsou porušeny přímo v tomto diffu.
- Pokud je kód v pořádku, řekni to — nepřidávej umělé výhrady jen aby review vypadalo důkladněji.
- U každého nálezu musíš být schopen říct: "Na řádku X v souboru Y vidím konkrétně toto."
- Piš stručně — autor PR zná kontext, nepotřebuje vysvětlení základních pojmů. Maximálně 2-3 věty na každý nález.
- Každý nález začni prefixem: "BLOCKER:" nebo "DOPORUČENÍ:" nebo "OTÁZKA:"
- Pokud nevidíš testové soubory v diffu, napiš pouze: "Testy v diffu nejsou — ověřit ručně."
- Ignoruj triviální nálezy jako zakomentovaný kód, chybějící mezery, nebo drobné formátování.
- Pokud nález nepomůže předejít bugu, výpadku nebo technickému dluhu, nevypisuj ho.
- Ignoruj importy a přejmenovávání souborů jako standalone nálezy.
- Pokud vidíš jen přesun kódu bez změny logiky, uveď to v overview.
- Pokud diff obsahuje více než 20 souborů, zaměř se primárně na core business logiku.

Tento projekt je multi-stack: Angular (TypeScript), .NET (C#), HTML, SASS.
Přizpůsob review danému jazyku a jeho konvencím podle těchto pravidel:

{stack_section}
- Pro HTML/SASS: sleduj Core Web Vitals:
  - LCP: chybějící lazy loading na obrázcích, chybějící preload na kritických zdrojích, render-blocking CSS/JS
  - CLS: chybějící width/height na obrázcích a embedech, layout shifty při načítání fontů (font-display)
  - INP: těžké CSS animace na width/margin/top místo transform/opacity které jdou přes compositor
  Přístupnost (WCAG 2.2) — reportuj pouze pokud vidíš konkrétní porušení v diffu:
  - Chybějící alt text na obrázcích (nebo alt="" pro dekorativní obrázky)
  - Interaktivní prvky bez label (input bez label/aria-label, button bez textu nebo aria-label)
  - Špatná heading hierarchie (h1→h3 bez h2, více h1 na stránce)
  - Chybějící focus styles (outline: none bez náhrady)
  - Nízký kontrast barev — POUZE pokud vidíš hardcoded barvy v HTML/SASS, nespekuluj
  - Klikatelné div/span elementy bez role="button" a tabindex="0"
  - Formuláře bez správných label vazeb, chybějící aria-required
  - Dynamický obsah bez aria-live pro screen readery

## Pull request
**Název PR:** {pr_title}
**Počet změněných řádků:** {line_count} (po vyfiltrování generovaných souborů)
{filter_note}
{jira_section}
## Git diff
```diff
{diff_preview}
```

Proveď code review a vrať odpověď jako JSON objekt s touto strukturou:

{{
  "summary": {{
    "overview": "Stručný přehled co PR dělá (2-3 věty)",
    "recommendation": "APPROVE nebo REQUEST CHANGES nebo NEEDS DISCUSSION",
    "key_points": ["bod 1", "bod 2", "bod 3"],
    "bugs": "🐛 Bugy a logické chyby – konkrétní problémy, nebo null pokud žádné",
    "security": "🔒 Bezpečnost – konkrétní rizika, nebo null pokud žádné",
    "performance": "⚡ Výkon – konkrétní problémy, nebo null pokud žádné",
    "tests": "🧪 Unit testy – které testy chybí s názvy, nebo null pokud vše pokryto",
    "architecture": "🏗️ Návrh a architektura – konkrétní problémy, nebo null pokud OK",
    "readability": "📖 Čitelnost a konvence – konkrétní problémy, nebo null pokud OK",
    "regression_risk": "🔄 Riziko regresí – co může rozbít, nebo null pokud žádné riziko",
    "goal_alignment": "🎯 Soulad se zadáním – problémy nebo null pokud vše OK"
  }},
  "inline_comments": [
    {{
      "file": "cesta/k/souboru.ts",
      "line": 42,
      "severity": "critical|major|minor|nit",
      "category": "bug|security|performance|test|readability|architecture|config|error_handling|logging|migration|dependency|concurrency",
      "comment": "Popis problému a navržená oprava"
    }}
  ]
}}

Pravidla pro inline komentáře:
- `file` musí být přesná cesta souboru z diff (např. "src/orders/order.service.ts")
- `line` musí být číslo řádku z diff (číslo řádku v novém souboru po změně, označené "+")
- Uváděj jen konkrétní, podstatné problémy — ne obecné poznámky
- Ke každému problému navrhni stručnou opravu
- Pokud nejsou nalezeny žádné problémy v dané kategorii, nevymýšlej je

Kategorie:
- bug: chyba v logice, neošetřená výjimka, špatná podmínka
- security: XSS, SQL injection, citlivá data, autorizace
- performance: N+1, zbytečné dotazy, velké cykly
- test: chybějící unit testy pro změněné funkce
- readability: špatné pojmenování, složitost, DRY
- architecture: těsná vazba, špatný návrh
- config: hardcoded hodnoty, chybějící env variables, secrets v kódu
- error_handling: spolykané výjimky, chybějící fallback, špatné HTTP status kódy
- logging: chybějící logy pro kritické operace, logování citlivých dat
- migration: breaking changes v DB schématu, chybějící rollback strategie
- dependency: nová závislost bez zdůvodnění, zranitelná verze balíčku
- concurrency: race condition, chybějící zamykání, problém při paralelním zpracování

Vrať POUZE validní JSON bez jakéhokoliv dalšího textu nebo markdown backticks.
"""


async def call_claude(prompt: str) -> str:
    """Zavolá Anthropic Claude API a vrátí text odpovědi."""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if not resp.is_success:
            print(f"[Claude ERROR] status={resp.status_code} body={resp.text}")
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def post_bitbucket_comment(
    workspace: str, repo_slug: str, pr_id: int, comment: str, token: str,
    file_path: str | None = None, line: int | None = None
) -> None:
    """Přidá komentář do PR – inline pokud je zadán soubor a řádek, jinak obecný."""
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    )
    body: dict = {"content": {"raw": comment}}

    if file_path and line:
        body["inline"] = {"to": line, "path": file_path}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=15,
        )
        print(f"[BB COMMENT] status={resp.status_code} url={url} file={file_path} line={line} response={resp.text[:300]}")
        if not resp.is_success:
            print(f"[BB COMMENT ERROR] status={resp.status_code} body={resp.text}")
        else:
            resp.raise_for_status()


def format_severity(severity: str) -> str:
    icons = {
        "critical": "🔴 **CRITICAL**",
        "major":    "🟠 **MAJOR**",
        "minor":    "🟡 minor",
        "nit":      "⚪ nit",
    }
    return icons.get(severity, severity)


def format_category(category: str) -> str:
    icons = {
        # Původní kategorie
        "bug":            "🐛 Bug",
        "security":       "🔒 Bezpečnost",
        "performance":    "⚡ Výkon",
        "test":           "🧪 Test",
        "readability":    "📖 Čitelnost",
        "architecture":   "🏗️ Architektura",
        # Nové kategorie
        "config":         "⚙️ Konfigurace",
        "error_handling": "🚨 Error handling",
        "logging":        "📋 Logování",
        "migration":      "🗄️ Migrace",
        "dependency":     "📦 Závislost",
        "concurrency":    "🔀 Konkurence",
    }
    return icons.get(category, category)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/bitbucket")
async def bitbucket_webhook(request: Request):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "Chybí ANTHROPIC_API_KEY")

    payload = await request.json()

    event = request.headers.get("X-Event-Key", "")
    if event not in ("pullrequest:created", "pullrequest:updated"):
        return JSONResponse({"status": "ignored", "event": event})

    pr        = payload.get("pullrequest", {})
    pr_id     = pr.get("id")
    pr_title  = pr.get("title", "")
    pr_desc   = pr.get("description", "")
    branch    = pr.get("source", {}).get("branch", {}).get("name", "")
    repo      = payload.get("repository", {})
    diff_url  = pr.get("links", {}).get("diff", {}).get("href", "")
    full_name = repo.get("full_name", "")
    workspace, _, repo_slug = full_name.partition("/")

    if not all([pr_id, workspace, repo_slug, diff_url]):
        raise HTTPException(400, f"Chybí data: pr_id={pr_id} workspace='{workspace}' repo_slug='{repo_slug}'")

    # Vyber token podle repozitáře
    token = BB_TOKENS.get(repo_slug, "")
    if not token:
        env_name = f"BB_TOKEN_{repo_slug.upper().replace('-', '_')}"
        raise HTTPException(400, f"Chybí BB token pro repozitář '{repo_slug}' – přidej '{env_name}' do Railway Variables")

    jira_id = extract_jira_id(branch) or extract_jira_id(pr_title) or extract_jira_id(pr_desc)

    # Deduplikace — ignoruj Bitbucket retry webhooky
    dedup_key = f"{workspace}/{repo_slug}/{pr_id}"
    if _is_already_processed(dedup_key):
        print(f"[CR] PR #{pr_id} duplicate — ignoruji")
        return JSONResponse({"status": "duplicate", "pr_id": pr_id})
    _mark_as_processed(dedup_key)

    print(f"[CR] PR #{pr_id} | branch: {branch} | Jira: {jira_id}")

    # Stáhni diff + detekuj stack verze paralelně
    raw_diff, angular_version, dotnet_version = await asyncio.gather(
        get_bitbucket_diff(diff_url, token),
        get_angular_version(workspace, repo_slug, token),
        get_dotnet_version(workspace, repo_slug, token),
    )

    print(f"[CR] Stack: Angular={angular_version or '?'} | .NET={dotnet_version or '?'}")

    # -----------------------------------------------------------------------
    # FILTR 1 — Odstraň ignorované soubory (package-lock.json atd.)
    # -----------------------------------------------------------------------
    filtered_diff, ignored_files = filter_diff(raw_diff)
    if ignored_files:
        print(f"[CR] Ignorované soubory: {', '.join(ignored_files)}")

    # -----------------------------------------------------------------------
    # FILTR 2 — Zkontroluj počet řádků
    # Pokud je změn příliš mnoho → přidej informativní komentář, nepošli na Claude
    # -----------------------------------------------------------------------
    line_count = count_changed_lines(filtered_diff)
    print(f"[CR] Změněných řádků po filtraci: {line_count} (limit: {POC_MAX_LINES})")

    if line_count > POC_MAX_LINES:
        skip_message = (
            f"🤖 **Automatické code review** (Claude AI)"
            f"{' | Jira: ' + jira_id if jira_id else ''}\n\n"
            f"⚠️ **PR přeskočen — příliš velká změna**\n\n"
            f"| Položka | Hodnota |\n"
            f"|---------|--------|\n"
            f"| Změněných řádků (bez generovaných souborů) | **{line_count}** |\n"
            f"| Limit | {POC_MAX_LINES} řádků |\n"
        )
        if ignored_files:
            skip_message += f"| Ignorované soubory | {', '.join(ignored_files)} |\n"
        skip_message += (
            f"\n💡 Pro ruční review doporučujeme rozdělit PR na menší části.\n"
            f"Automatické review bude aktivováno po snížení počtu změn pod {POC_MAX_LINES} řádků."
        )
        await post_bitbucket_comment(workspace, repo_slug, pr_id, skip_message, token)
        return JSONResponse({
            "status": "skipped",
            "reason": "too_many_lines",
            "line_count": line_count,
            "limit": POC_MAX_LINES,
            "pr_id": pr_id,
        })

    # -----------------------------------------------------------------------
    # PR je v limitu → pokračuj s Claude review
    # -----------------------------------------------------------------------
    jira = await get_jira_ticket(jira_id) if jira_id else {}

    prompt = build_prompt(
        filtered_diff, jira, pr_title, line_count, ignored_files,
        angular_version=angular_version,
        dotnet_version=dotnet_version,
    )
    raw = await call_claude(prompt)

    # Parsuj JSON odpověď od Claudea
    try:
        clean  = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        review = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"[JSON ERROR] {e}\nRaw: {raw[:500]}")
        await post_bitbucket_comment(workspace, repo_slug, pr_id,
            f"🤖 **Automatické code review** (Claude AI){' | Jira: ' + jira_id if jira_id else ''}\n\n{raw}",
            token)
        return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id, "mode": "fallback"})

    summary  = review.get("summary", {})
    comments = review.get("inline_comments", [])

    # 1. Souhrnný komentář nahoře v PR
    rec      = summary.get("recommendation", "")
    rec_icon = {"APPROVE": "✅", "REQUEST CHANGES": "❌", "NEEDS DISCUSSION": "💬"}.get(rec, "🤖")
    points   = "\n".join(f"* {p}" for p in summary.get("key_points", []))

    header = (
        f"🤖 **Automatické code review** (Claude AI)"
        f"{' | Jira: ' + jira_id if jira_id else ''}\n\n"
        f"| Položka | Hodnota |\n"
        f"|---------|--------|\n"
        f"| Změněných řádků | {line_count} |\n"
    )
    if angular_version:
        header += f"| Angular verze | {angular_version} |\n"
    if dotnet_version:
        header += f"| .NET verze | {dotnet_version} |\n"
    if ignored_files:
        header += f"| Ignorované soubory | {', '.join(ignored_files)} |\n"
    header += (
        f"\n---\n"
        f"### {rec_icon} {rec}\n\n"
        f"{summary.get('overview', '')}\n\n"
        f"**Klíčové body:**\n{points}\n\n"
        f"---\n"
    )

    sections = [
        ("🐛 Bugy a logické chyby",  summary.get("bugs")),
        ("🔒 Bezpečnost",             summary.get("security")),
        ("⚡ Výkon",                  summary.get("performance")),
        ("🧪 Unit testy",             summary.get("tests")),
        ("🏗️ Návrh a architektura",  summary.get("architecture")),
        ("📖 Čitelnost a konvence",   summary.get("readability")),
        ("🔄 Riziko regresí",         summary.get("regression_risk")),
        ("🎯 Soulad se zadáním",      summary.get("goal_alignment")),
    ]
    for title, value in sections:
        if value and value not in (None, "null", "", "OK", "Nenalezeny", "Pokryto"):
            header += f"### {title}\n{value}\n\n"

    header += "---\n*Podrobné inline komentáře jsou přidány přímo k řádkům kódu níže.*"

    await post_bitbucket_comment(workspace, repo_slug, pr_id, header, token)

    # 2. Inline komentáře přímo k řádkům
    posted = 0
    for item in comments:
        file_path = item.get("file", "")
        line      = item.get("line")
        comment   = item.get("comment", "")
        severity  = item.get("severity", "minor")
        category  = item.get("category", "")

        if not file_path or not line or not comment:
            continue

        text = f"{format_severity(severity)} {format_category(category)}\n\n{comment}"
        await post_bitbucket_comment(workspace, repo_slug, pr_id, text, token,
                                     file_path=file_path, line=line)
        posted += 1

    print(f"[CR] PR #{pr_id} hotovo | inline komentářů: {posted}")
    return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id,
                         "line_count": line_count, "inline_comments": posted})


@app.get("/health")
async def health():
    configured_repos = [repo for repo, token in BB_TOKENS.items() if token]
    return {
        "status": "ok",
        "anthropic": "ok" if ANTHROPIC_API_KEY else "missing",
        "repos_configured": configured_repos,
        "jira": "ok" if JIRA_BASE_URL else "missing JIRA_BASE_URL",
        "poc_config": {
            "max_lines": POC_MAX_LINES,
            "ignored_files": IGNORED_FILES,
        },
    }
