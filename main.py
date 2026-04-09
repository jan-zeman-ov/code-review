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
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Konfigurace – nastav jako Environment Variables (nikdy nekládej klíče do kódu!)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Token pro každý repozitář zvlášť – přidej nový řádek pro každé repo
# V Railway nastav: BB_TOKEN_NETDIRECT_TEST, BB_TOKEN_JIP_SHOP atd.
BB_TOKENS = {
    "netdirect-test": os.environ.get("BB_TOKEN_NETDIRECT_TEST", ""),
    "jip-shop":       os.environ.get("BB_TOKEN_JIP_SHOP", ""),
}

JIRA_BASE_URL  = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL     = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

JIRA_ID_PATTERN = re.compile(r"([A-Z]{2,10}-\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def extract_jira_id(text: str) -> str | None:
    match = JIRA_ID_PATTERN.search(text or "")
    return match.group(1).upper() if match else None


async def get_bitbucket_diff(diff_url: str, token: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            diff_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text


async def get_jira_ticket(jira_id: str) -> dict:
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
        ac = _extract_text(fields.get("customfield_10016"))
        return {
            "id": jira_id,
            "summary": fields.get("summary", ""),
            "description": description,
            "acceptance_criteria": ac,
            "issue_type": fields.get("issuetype", {}).get("name", ""),
        }


def _extract_text(field) -> str:
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


def build_prompt(diff: str, jira: dict, pr_title: str) -> str:
    jira_section = ""
    if jira:
        jira_section = f"""
## Kontext Jira ticketu ({jira['id']})
**Typ:** {jira['issue_type']}
**Název:** {jira['summary']}
**Popis:** {jira['description'] or '(není)'}
**Acceptance criteria:** {jira['acceptance_criteria'] or '(není)'}
"""

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

Proveď důkladné code review následujícího pull requestu.

## Pull request
**Název PR:** {pr_title}
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
    "key_points": ["bod 1", "bod 2", "bod 3"]
  }},
  "inline_comments": [
    {{
      "file": "cesta/k/souboru.ts",
      "line": 42,
      "severity": "critical|major|minor|nit",
      "category": "bug|security|performance|test|readability|architecture",
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

Vrať POUZE validní JSON bez jakéhokoliv dalšího textu nebo markdown backticks.
"""


async def call_claude(prompt: str) -> str:
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

    # Inline komentář – přidá se přímo k danému řádku v souboru
    if file_path and line:
        body["inline"] = {"to": line, "path": file_path}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=15,
        )
        # Inline komentář může selhat pokud řádek neexistuje v diff – logujeme ale nepřerušujeme
        if not resp.is_success:
            print(f"[BB COMMENT ERROR] status={resp.status_code} file={file_path} line={line} body={resp.text}")
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
        "bug":          "🐛 Bug",
        "security":     "🔒 Bezpečnost",
        "performance":  "⚡ Výkon",
        "test":         "🧪 Test",
        "readability":  "📖 Čitelnost",
        "architecture": "🏗️ Architektura",
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

    print(f"[CR] PR #{pr_id} | branch: {branch} | Jira: {jira_id}")

    diff = await get_bitbucket_diff(diff_url, token)
    jira = await get_jira_ticket(jira_id) if jira_id else {}

    prompt  = build_prompt(diff, jira, pr_title)
    raw     = await call_claude(prompt)

    # Parsuj JSON odpověď od Claudea
    try:
        # Odstraň případné markdown backticks pokud Claude zapomněl
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        review = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"[JSON ERROR] {e}\nRaw: {raw[:500]}")
        # Fallback – vlož raw odpověď jako obecný komentář
        await post_bitbucket_comment(workspace, repo_slug, pr_id,
            f"🤖 **Automatické code review** (Claude AI){' | Jira: ' + jira_id if jira_id else ''}\n\n{raw}",
            token)
        return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id, "mode": "fallback"})

    summary   = review.get("summary", {})
    comments  = review.get("inline_comments", [])

    # 1. Souhrnný komentář nahoře v PR – obsahuje celé detailní review
    rec      = summary.get("recommendation", "")
    rec_icon = {"APPROVE": "✅", "REQUEST CHANGES": "❌", "NEEDS DISCUSSION": "💬"}.get(rec, "🤖")
    points   = "\n".join(f"* {p}" for p in summary.get("key_points", []))
    header = (
        f"🤖 **Automatické code review** (Claude AI)"
        f"{' | Jira: ' + jira_id if jira_id else ''}\n\n"
        f"### {rec_icon} {rec}\n\n"
        f"{summary.get('overview', '')}\n\n"
        f"**Klíčové body:**\n{points}\n\n"
        f"---\n"
        f"### 🐛 Bugy a logické chyby\n{summary.get('bugs', 'Nenalezeny')}\n\n"
        f"### 🔒 Bezpečnost\n{summary.get('security', 'Nenalezeny')}\n\n"
        f"### ⚡ Výkon\n{summary.get('performance', 'Nenalezeny')}\n\n"
        f"### 🧪 Unit testy\n{summary.get('tests', 'Pokryto')}\n\n"
        f"### 🏗️ Návrh a architektura\n{summary.get('architecture', 'OK')}\n\n"
        f"### 📖 Čitelnost a konvence\n{summary.get('readability', 'OK')}\n\n"
        f"### 🔄 Riziko regresí\n{summary.get('regression_risk', '—')}\n\n"
        f"### 🎯 Soulad se zadáním\n{summary.get('goal_alignment', '—')}\n\n"
        f"---\n*Podrobné inline komentáře jsou přidány přímo k řádkům kódu níže.*"
    )
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
    return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id, "inline_comments": posted})


@app.get("/health")
async def health():
    configured_repos = [repo for repo, token in BB_TOKENS.items() if token]
    return {
        "status": "ok",
        "anthropic": "ok" if ANTHROPIC_API_KEY else "missing",
        "repos_configured": configured_repos,
        "jira": "ok" if JIRA_BASE_URL else "missing JIRA_BASE_URL",
    }
