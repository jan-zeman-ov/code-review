"""
Bitbucket PR → Jira → Claude Code Review Bot
=============================================
Nasazení: Railway / Render / vlastní server
Požadavky: viz requirements.txt
"""

from __future__ import annotations

import re
import os
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Konfigurace – nastav jako Environment Variables (nikdy nekládej klíče do kódu!)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BB_USERNAME       = os.environ.get("BB_USERNAME", "")
BB_APP_PASSWORD   = os.environ.get("BB_APP_PASSWORD", "")
JIRA_BASE_URL     = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL        = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN    = os.environ.get("JIRA_API_TOKEN", "")

# re.IGNORECASE = funguje i pro malá písmena (jip-357 → JIP-357)
JIRA_ID_PATTERN = re.compile(r"([A-Z]{2,10}-\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def extract_jira_id(text: str) -> str | None:
    """Vytáhne první Jira ID z textu a převede na velká písmena.
    Funguje pro: revert/keep-jip-353, JIP-357, EMT-94 atd.
    """
    match = JIRA_ID_PATTERN.search(text or "")
    return match.group(1).upper() if match else None


async def get_bitbucket_diff(workspace: str, repo_slug: str, pr_id: int) -> str:
    """Stáhne unified diff PR z Bitbucket Cloud API."""
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/pullrequests/{pr_id}/diff"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, auth=(BB_USERNAME, BB_APP_PASSWORD), timeout=30)
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

        # Acceptance criteria jsou typicky v custom field nebo v description
        description = _extract_text(fields.get("description"))
        ac = _extract_text(fields.get("customfield_10016"))  # uprav podle vaší instance

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
    # ADF – rekurzivně vytáhni text uzly
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


def build_prompt(diff: str, jira: dict, pr_title: str) -> str:
    """Sestaví prompt pro Claude."""
    jira_section = ""
    if jira:
        jira_section = f"""
## Kontext Jira ticketu ({jira['id']})
**Typ:** {jira['issue_type']}
**Název:** {jira['summary']}
**Popis:** {jira['description'] or '(není)'}
**Acceptance criteria:** {jira['acceptance_criteria'] or '(není)'}
"""

    # Diff zkrátíme na max ~8000 znaků, aby se vešel do kontextového okna
    diff_preview = diff[:8000] + ("\n...[diff zkrácen]" if len(diff) > 8000 else "")

    return f"""Jsi senior software engineer a QA inženýr s 10+ lety zkušeností.
Proveď důkladné code review následujícího pull requestu.

## Pull request
**Název PR:** {pr_title}
{jira_section}
## Git diff
```diff
{diff_preview}
```

Proveď code review tohoto PR. Zaměř se jen na podstatné problémy a přínosné připomínky.

Pravidla:
* Uváděj konkrétní nálezy s odkazem na soubor a řádky.
* U každého nálezu uveď závažnost: `critical / major / minor / nit`.
* Rozlišuj mezi **bugem/rizikem** a **doporučením**.
* Ke každému relevantnímu problému navrhni stručnou opravu.
* Pokud něco bez širšího kontextu nelze posoudit, napiš to explicitně.
* Pokud nejsou nalezeny žádné problémy, napiš to stručně a nevymýšlej je.

Strukturuj odpověď do sekcí:

### 🔍 Přehled změn
Stručně shrň, co PR dělá.

### 🐛 Bugy a logické chyby
Konkrétní problémy s odkazem na řádky. Pokud žádné, napiš „Nenalezeny“.

### 🔒 Bezpečnost
XSS, SQL injection, autorizace, citlivá data v logu atd.

### ⚡ Výkon
Zbytečné dotazy, N+1, paměť, velké cykly.

### 🧪 Pokrytí testy
Chybí testy? Které scénáře nebo edge cases nejsou pokryty?

### 🏗️ Návrh a architektura
Je řešení dobře navržené? Nevzniká zbytečná složitost nebo těsná vazba?

### 📖 Čitelnost a konvence
Pojmenování, komentáře, složitost funkcí, DRY.

### 🔄 Riziko regresí
Co může tato změna nepřímo rozbít a co by se mělo otestovat ručně?

### 🎯 Soulad se zadáním
Plní změna očekávaný cíl? Není něco nedokončené nebo zavádějící?

### ✅ Závěr

**Doporučení:** APPROVE / REQUEST CHANGES / NEEDS DISCUSSION
**Klíčové body:**
* max 3 nejdůležitější body

Na konci přidej krátké shrnutí, co je blocker a co je jen doporučení.
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
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


async def post_bitbucket_comment(
    workspace: str, repo_slug: str, pr_id: int, comment: str
) -> None:
    """Přidá komentář do Bitbucket PR."""
    url = (
        f"https://api.bitbucket.org/2.0/repositories/"
        f"{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            auth=(BB_USERNAME, BB_APP_PASSWORD),
            json={"content": {"raw": comment}},
            timeout=15,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/bitbucket")
async def bitbucket_webhook(request: Request):
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "BB_USERNAME": BB_USERNAME,
        "BB_APP_PASSWORD": BB_APP_PASSWORD,
    }.items() if not v]
    if missing:
        raise HTTPException(500, f"Chybí env variables: {', '.join(missing)}")

    payload = await request.json()

    # Ověř event typ
    event = request.headers.get("X-Event-Key", "")
    if event not in ("pullrequest:created", "pullrequest:updated"):
        return JSONResponse({"status": "ignored", "event": event})

    pr        = payload.get("pullrequest", {})
    pr_id     = pr.get("id")
    pr_title  = pr.get("title", "")
    pr_desc   = pr.get("description", "")
    branch    = pr.get("source", {}).get("branch", {}).get("name", "")
    repo      = payload.get("repository", {})

    # full_name = "workspace/repo-slug" – spolehlivější než repo.workspace.slug
    full_name = repo.get("full_name", "")
    workspace, _, repo_slug = full_name.partition("/")

    if not all([pr_id, workspace, repo_slug]):
        raise HTTPException(400, "Chybí povinná data v payloadu")

    # Jira ID hledáme v branch → title → description (v tomto pořadí)
    jira_id = extract_jira_id(branch) or extract_jira_id(pr_title) or extract_jira_id(pr_desc)

    print(f"[CR] PR #{pr_id} | branch: {branch} | Jira: {jira_id}")

    # Paralelně stáhni diff a Jira ticket
    diff = await get_bitbucket_diff(workspace, repo_slug, pr_id)
    jira = await get_jira_ticket(jira_id) if jira_id else {}

    prompt = build_prompt(diff, jira, pr_title)
    review = await call_claude(prompt)

    # Přidej prefix s info o CR botu
    header = (
        f"🤖 **Automatické code review** (Claude AI)"
        f"{' | Jira: ' + jira_id if jira_id else ''}\n\n"
    )
    await post_bitbucket_comment(workspace, repo_slug, pr_id, header + review)

    return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id})


@app.get("/health")
async def health():
    missing = [k for k, v in {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "BB_USERNAME": BB_USERNAME,
        "BB_APP_PASSWORD": BB_APP_PASSWORD,
        "JIRA_BASE_URL": JIRA_BASE_URL,
    }.items() if not v]
    return {
        "status": "ok",
        "config": "complete" if not missing else f"missing: {missing}"
    }