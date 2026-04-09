"""
Bitbucket PR → Jira → Claude Code Review Bot
=============================================
Nasazení: Railway / Render / vlastní server
Požadavky: viz requirements.txt
"""

import re
import os
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Konfigurace – nastav jako Environment Variables (nikdy nekládej klíče do kódu!)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
BB_USERNAME         = os.environ["BB_USERNAME"]          # Bitbucket Cloud username
BB_APP_PASSWORD     = os.environ["BB_APP_PASSWORD"]      # Bitbucket App Password
JIRA_BASE_URL       = os.environ["JIRA_BASE_URL"]        # https://yourcompany.atlassian.net
JIRA_EMAIL          = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN      = os.environ["JIRA_API_TOKEN"]       # Jira API token
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "")  # volitelné ověření

JIRA_ID_PATTERN = re.compile(r"([A-Z]{2,10}-\d+)")


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def extract_jira_id(text: str) -> str | None:
    """Vytáhne první Jira ID (např. EMT-94) z libovolného textu."""
    match = JIRA_ID_PATTERN.search(text or "")
    return match.group(1) if match else None


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

Proveď review z těchto pohledů a strukturuj odpověď do sekcí:

### 🔍 Přehled změn
Stručně shrň, co PR dělá.

### 🐛 Bugy a logické chyby
Konkrétní problémy s odkazem na řádky. Pokud žádné, napiš "Nenalezeny".

### 🔒 Bezpečnost
XSS, SQL injection, autorizace, citlivá data v logu atd.

### ⚡ Výkon
Zbytečné dotazy, N+1, paměť, velké cykly.

### 🧪 Pokrytí testy
Chybí testy? Které případy nejsou pokryty?

### 📖 Čitelnost a konvence
Pojmenování, komentáře, složitost funkcí, DRY.

### ✅ Závěr
**Doporučení:** APPROVE / REQUEST CHANGES / NEEDS DISCUSSION
**Klíčové body:** (max 3 odrážky co je nejdůležitější)
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
    payload = await request.json()

    # Ověř event typ
    event = request.headers.get("X-Event-Key", "")
    if event not in ("pullrequest:created", "pullrequest:updated"):
        return JSONResponse({"status": "ignored", "event": event})

    pr = payload.get("pullrequest", {})
    pr_id    = pr.get("id")
    pr_title = pr.get("title", "")
    source   = pr.get("source", {})
    branch   = source.get("branch", {}).get("name", "")
    repo     = payload.get("repository", {})
    workspace  = repo.get("workspace", {}).get("slug", "")
    repo_slug  = repo.get("slug", "")

    if not all([pr_id, workspace, repo_slug]):
        raise HTTPException(400, "Chybí povinná data v payloadu")

    # Najdi Jira ID – nejdřív v branch, pak v titulku PR
    jira_id = extract_jira_id(branch) or extract_jira_id(pr_title)

    print(f"[CR] PR #{pr_id} | branch: {branch} | Jira: {jira_id}")

    # Paralelně stáhni diff a Jira ticket
    diff_task  = get_bitbucket_diff(workspace, repo_slug, pr_id)
    jira_task  = get_jira_ticket(jira_id) if jira_id else None

    diff = await diff_task
    jira = await jira_task if jira_task else {}

    # Sestav prompt a zavolej Claude
    prompt  = build_prompt(diff, jira, pr_title)
    review  = await call_claude(prompt)

    # Přidej prefix s info o CR botu
    header = (
        f"🤖 **Automatické code review** (Claude AI)"
        f"{' | Jira: ' + jira_id if jira_id else ''}\n\n"
    )
    await post_bitbucket_comment(workspace, repo_slug, pr_id, header + review)

    return JSONResponse({"status": "ok", "pr_id": pr_id, "jira_id": jira_id})


@app.get("/health")
async def health():
    return {"status": "ok"}
