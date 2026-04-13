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
import hmac
import hashlib
import sqlite3
import time
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Dedup přes SQLite — přežije restart serveru
# ---------------------------------------------------------------------------
DEDUP_TTL = 900  # 15 minut
_db = sqlite3.connect("dedup.db", check_same_thread=False)
_db.execute(
    "CREATE TABLE IF NOT EXISTS processed_prs "
    "(key TEXT PRIMARY KEY, ts REAL)"
)
_db.commit()

def _is_already_processed(key: str) -> bool:
    row = _db.execute(
        "SELECT ts FROM processed_prs WHERE key = ?", (key,)
    ).fetchone()
    if row and time.time() - row[0] < DEDUP_TTL:
        return True
    # Expirovaný záznam — smaž
    _db.execute("DELETE FROM processed_prs WHERE key = ?", (key,))
    _db.commit()
    return False

def _mark_as_processed(key: str) -> None:
    _db.execute(
        "INSERT OR REPLACE INTO processed_prs (key, ts) VALUES (?, ?)",
        (key, time.time()),
    )
    # Uklidění starých záznamů
    _db.execute(
        "DELETE FROM processed_prs WHERE ts < ?",
        (time.time() - DEDUP_TTL,),
    )
    _db.commit()

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

BB_WEBHOOK_SECRET = os.environ.get("BB_WEBHOOK_SECRET", "")

def _verify_webhook_signature(secret: str, body: bytes, signature: str) -> bool:
    """Ověří Bitbucket webhook HMAC podpis. Přeskoč pokud secret není nastaven."""
    if not secret:
        return True  # secret není nakonfigurován — přeskoč ověření
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature or "")


async def _fetch_json_file(client: httpx.AsyncClient, workspace: str, repo_slug: str,
                            token: str, path: str) -> dict | None:
    """Stáhne JSON soubor ze specifické cesty v repozitáři."""
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/src/HEAD/{path}"
    )
    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if not resp.is_success:
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def _list_dir(client: httpx.AsyncClient, workspace: str, repo_slug: str,
                     token: str, path: str = "") -> list[dict]:
    """Vrátí VŠECHNY soubory v dané cestě repozitáře — se stránkováním."""
    all_values = []
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/src/HEAD/{path}"
        f"?pagelen=100"
    )
    while url:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if not resp.is_success:
            break
        data = resp.json()
        all_values.extend(data.get("values", []))
        url = data.get("next")  # None pokud není další stránka
    return all_values


async def get_angular_version(workspace: str, repo_slug: str, token: str) -> str | None:
    """
    Načte hlavní verzi Angularu z package.json.
    Hledá v rootu a pak v podsložkách (AdminMvc/, FlexMvc/ atd.)
    Pokud @angular/core není nalezen vůbec, vrátí "6" jako bezpečný fallback
    protože starší Angular projekty bývají v6 a Claude by měl použít NgModules kontext.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # Kandidátní cesty — root + časté podsložky MVC projektů
            candidates = ["package.json"]
            root_files = await _list_dir(client, workspace, repo_slug, token)
            for f in root_files:
                if f.get("type") == "commit_directory":
                    candidates.append(f"{f['path']}/package.json")

            seen_versions = set()
            for path in candidates:
                pkg = await _fetch_json_file(client, workspace, repo_slug, token, path)
                if not pkg:
                    continue
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                version = deps.get("@angular/core", "")
                if not version:
                    continue
                match = re.search(r"(\d+)", version)
                if match:
                    seen_versions.add(match.group(1))

            if not seen_versions:
                # Angular projekt ale verze nenalezena → fallback na v6 (NgModules)
                return "6"
            # Vrať nejvyšší nalezenou verzi
            return str(max(int(v) for v in seen_versions))
        except Exception:
            return "6"  # bezpečný fallback


async def get_dotnet_version(workspace: str, repo_slug: str, token: str) -> str | None:
    """
    Načte verzi .NET z .csproj souboru.
    Hledá v rootu a pak v podsložkách (jeden level).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # Hledej .csproj v rootu i podsložkách
            candidates: list[str] = []
            root_files = await _list_dir(client, workspace, repo_slug, token)
            for f in root_files:
                if f["path"].endswith(".csproj"):
                    candidates.append(f["path"])
                elif f.get("type") == "commit_directory":
                    sub_files = await _list_dir(client, workspace, repo_slug, token, f["path"])
                    for sf in sub_files:
                        if sf["path"].endswith(".csproj"):
                            candidates.append(sf["path"])

            versions = []
            for csproj_path in candidates:
                url = (
                    f"https://api.bitbucket.org/2.0/repositories/"
                    f"{workspace}/{repo_slug}/src/HEAD/{csproj_path}"
                )
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}, timeout=10
                )
                if not resp.is_success:
                    continue
                match = re.search(
                    r"<TargetFramework(?:Version)?>(.*?)</TargetFramework(?:Version)?>",
                    resp.text,
                )
                if match:
                    versions.append(match.group(1).strip())

            if not versions:
                return None
            # Majority vote — vrať nejčastější verzi (v4.8 x5 vyhraje nad v4.5 x1)
            from collections import Counter
            return Counter(versions).most_common(1)[0][0]
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


def _build_angular_performance_note(version: str | None) -> str:
    """Vrátí verzově specifické performance tipy pro Angular."""
    if not version:
        return (
            "angular change detection (chybějící OnPush), "
            "memory leak (subscribe bez unsubscribe/takeUntil), "
            "trackBy chybí v *ngFor pro velké listy"
        )
    v = int(version)
    if v <= 8:
        return (
            "angular change detection (chybějící OnPush — v Angular 6-8 kritické), "
            "memory leak (subscribe bez unsubscribe v ngOnDestroy), "
            "trackBy chybí v *ngFor pro velké listy, "
            "pure pipe místo metody v template (metoda se volá při každém CD cyklu)"
        )
    elif v <= 12:
        return (
            "angular change detection (chybějící OnPush), "
            "memory leak (subscribe bez unsubscribe/takeUntil), "
            "trackBy chybí v *ngFor, "
            "async pipe preferován před manuálním subscribe"
        )
    elif v <= 16:
        return (
            "angular change detection (chybějící OnPush), "
            "memory leak (subscribe bez takeUntil nebo async pipe), "
            "trackBy chybí v *ngFor, "
            "inject() místo constructor injection pro lepší tree-shaking"
        )
    elif v == 17:
        return (
            "angular change detection (OnPush nebo Signals pro reaktivní stav), "
            "memory leak (subscribe bez takeUntil — nebo toSignal() pro automatický cleanup), "
            "track expression chybí v @for pro velké listy"
        )
    else:  # v18+
        return (
            "Signals preferované před RxJS pro lokální stav (méně paměti, žádný leak), "
            "memory leak (subscribe bez takeUntil pokud RxJS stále použit), "
            "effect() bez cleanup funkce může leakovat, "
            "@for track expression povinný — zkontroluj správnost trackování"
        )


def _build_dotnet_performance_note(version: str | None) -> str:
    """Vrátí verzově specifické performance tipy pro .NET."""
    if not version:
        return (
            "connection pool exhaustion (chybějící using() u DB spojení), "
            "string concatenation v cyklu (+ místo StringBuilder)"
        )
    if "v4." in version or "net4" in version:
        return (
            "EF6 lazy loading (navigační properties v cyklu bez Include() — lazy loading DEFAULT ZAPNUTÝ), "
            "missing pagination (ToList() bez Skip/Take nad velkou tabulkou), "
            "large payload (celá EF entita místo DTO), "
            "connection pool exhaustion (chybějící using() u SqlConnection nebo DbContext), "
            "string concatenation v cyklu (+ místo StringBuilder), "
            "sync over async (Task.Result nebo .Wait() blokuje thread pool), "
            "boxing/unboxing (value types v ArrayList nebo non-generic kolekci), "
            "missing output caching (stejná data bez [OutputCache])"
        )
    major = re.search(r"net(\d+)", version)
    v = int(major.group(1)) if major else 0
    if v >= 8:
        return (
            "EF Core lazy loading (chybějící Include() nebo AsNoTracking() pro read-only dotazy), "
            "missing pagination (ToListAsync() bez Skip/Take), "
            "large payload (celá entita místo DTO projekce přímo v LINQ), "
            "IHttpClientFactory nevyužit (přímý new HttpClient() — socket exhaustion), "
            "missing CancellationToken (async metody bez propagace tokenu), "
            "string concatenation v cyklu (+ místo StringBuilder nebo interpolace), "
            "sync over async (Task.Result nebo .Wait())"
        )
    return (
        "EF Core lazy loading (chybějící Include() nebo AsNoTracking()), "
        "missing pagination (ToListAsync() bez Skip/Take), "
        "connection pool exhaustion (chybějící using()), "
        "sync over async (Task.Result nebo .Wait())"
    )


def _build_dotnet_security_note(version: str | None) -> str:
    """Vrátí verzově specifické security tipy pro .NET."""
    if not version:
        return (
            "CSRF (chybějící AntiForgeryToken), "
            "mass assignment (chybějící Bind whitelist), "
            "verbose errors (stack trace viditelný uživateli)"
        )
    if "v4." in version or "net4" in version:
        return (
            "CSRF (chybějící [ValidateAntiForgeryToken] na POST akcích v MVC), "
            "IDOR (přístup k cizím datům bez ověření vlastnictví záznamu), "
            "mass assignment (chybějící [Bind(Include=...)] whitelist v MVC modelech), "
            "verbose errors (stack trace nebo DB chyba viditelná uživateli v produkci), "
            "weak cryptography (MD5/SHA1 pro hesla — použij PBKDF2 nebo bcrypt), "
            "SQL injection přes string concatenation (i s EF6 přes ExecuteSqlCommand)"
        )
    major = re.search(r"net(\d+)", version)
    v = int(major.group(1)) if major else 0
    if v >= 8:
        return (
            "IDOR (přístup k cizím datům bez ověření vlastnictví), "
            "mass assignment (chybějící [Bind] nebo samostatné DTO), "
            "verbose errors (UseExceptionHandler chybí nebo developer page v produkci), "
            "weak cryptography (MD5/SHA1 místo bcrypt/PBKDF2), "
            "SQL injection přes raw SQL v EF Core (FromSqlRaw bez parametrizace)"
        )
    return (
        "CSRF (chybějící antiforgery middleware), "
        "IDOR (přístup k cizím datům bez ověření vlastnictví), "
        "mass assignment (chybějící DTO), "
        "verbose errors (stack trace viditelný uživateli)"
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
    angular_ctx  = _angular_note(angular_version)
    dotnet_ctx   = _dotnet_note(dotnet_version)
    angular_perf = _build_angular_performance_note(angular_version)
    dotnet_perf  = _build_dotnet_performance_note(dotnet_version)
    dotnet_sec   = _build_dotnet_security_note(dotnet_version)

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
- security: XSS, SQL injection, citlivá data, autorizace, {dotnet_sec}
- performance: N+1, zbytečné dotazy, velké cykly, {dotnet_perf}, {angular_perf}
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
Všechny texty v JSON (overview, key_points, bugs, security, performance, tests, architecture, readability, regression_risk, goal_alignment, comment) piš v češtině. Technické termíny, názvy metod, proměnných, knihoven a programátorský slang ponechej v originále (např. "N+1 query", "race condition", "memory leak", názvy funkcí atd.).
"""


async def call_claude(prompt: str) -> str:
    """Zavolá Anthropic Claude API s exponential backoff retry (529, 503, timeout)."""
    delays = [2, 5, 10]  # sekundy mezi pokusy
    last_error: Exception | None = None
    for attempt, delay in enumerate([0] + delays, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
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
                        "max_tokens": 8192,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code in (529, 503):
                    print(f"[Claude] Attempt {attempt} — overloaded ({resp.status_code}), retry...")
                    last_error = Exception(f"Claude overloaded: {resp.status_code}")
                    continue
                if not resp.is_success:
                    print(f"[Claude ERROR] status={resp.status_code} body={resp.text}")
                resp.raise_for_status()
                data = resp.json()
                return data["content"][0]["text"]
        except httpx.TimeoutException as e:
            print(f"[Claude] Attempt {attempt} — timeout, retry...")
            last_error = e
            continue
    raise Exception(f"Claude selhal po {len(delays)+1} pokusech: {last_error}")


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

    body = await request.body()

    # 2. Ověření webhook podpisu
    if BB_WEBHOOK_SECRET:
        signature = request.headers.get("X-Hub-Signature", "")
        if not _verify_webhook_signature(BB_WEBHOOK_SECRET, body, signature):
            raise HTTPException(401, "Neplatný webhook podpis")

    payload = json.loads(body)

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
    commit_hash = pr.get("source", {}).get("commit", {}).get("hash", "")
    dedup_key = f"{workspace}/{repo_slug}/{pr_id}/{commit_hash}"
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

    # 2. Inline komentáře přímo k řádkům — paralelně
    async def _post_inline(item: dict) -> bool:
        file_path = item.get("file", "")
        line      = item.get("line")
        comment   = item.get("comment", "")
        severity  = item.get("severity", "minor")
        category  = item.get("category", "")
        if not file_path or not line or not comment:
            return False
        text = f"{format_severity(severity)} {format_category(category)}\n\n{comment}"
        await post_bitbucket_comment(workspace, repo_slug, pr_id, text, token,
                                     file_path=file_path, line=line)
        return True

    results = await asyncio.gather(*[_post_inline(item) for item in comments])
    posted = sum(1 for r in results if r)

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
        "webhook_secret": "ok" if BB_WEBHOOK_SECRET else "⚠️ not set — webhook not verified",
        "poc_config": {
            "max_lines": POC_MAX_LINES,
            "ignored_files": IGNORED_FILES,
        },
    }
