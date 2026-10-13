# Zero Gap Agent – Python app (replaces the n8n flows)

Two agents check every incoming long-term-care application and prepare the next step for a caseworker:

| | Agent **with ARIS context** | Agent with **zero context** |
|---|---|---|
| Documents & SOPs | ARIS repository (MCP), incl. policy amendments PA-2026-01…04 | Google Drive (old SOP copies, no amendments) |
| Case history | ARIS process data, exact insurance number | Event log, searched **by name** |
| Result | Letter, escalation to the Case Manager, or release for assessment | Always a letter (with the known risks C1–W10) |

Nothing is sent until a caseworker approves – in the review e-mail or in the portal.

## Run it (demo mode – no credentials needed)

```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:8000 → **Portal – agent with ARIS context** → **⬆ New application** → choose a PDF from `sample_forms/`.
Both agents start; switch between the two portals with the tabs at the top. Review e-mails are written to `data/outbox/`
(open the `.eml` files – the Approve / Reject buttons work while the app is running).

Command line: `python app.py run sample_forms/Form_6_No_Benefit_Type_Same_Name_Open_ARIS_Case_No_Email.pdf`

## Go live (step by step)

1. `cp .env.example .env` and set `BASE_URL` to the address people will open (the links in the e-mails use it).
2. **Google** (Drive for the zero-context agent, Gmail intake, sending mail):
   create an OAuth client of type *Desktop app* in Google Cloud (enable Gmail, Drive, Docs and Sheets APIs),
   save it as `secrets/google_client.json`, run `python app.py google-login` once.
   Then set `DRIVE_SOURCE=live`, `GMAIL_INTAKE=true`, `MAIL_MODE=gmail`.
   Word files and the event-log CSV in the folder are converted to Google Docs / a Google Sheet inside Drive on the
   first run – nothing large is ever downloaded; the event log is queried for the applicant's name only.
3. **ARIS**: run `python app.py aris-login` (browser sign-in, prints the MCP tools it can see) or set `ARIS_MCP_TOKEN`.
   Check the tool names in `.env` match the list, then set `ARIS_SOURCE=live`.
   (If ARIS answers *“Tenant is not licensed … ARIS MCP Server”*, the ARIS admin has to assign that licence.)
4. Optional: `OPENAI_API_KEY` for AI-written letters (every draft still goes through the guardrails).
5. While testing set `TEST_RECIPIENT` – all letters to applicants then go to that address.

Run it permanently on a server or VM: `python app.py` (or `docker build -t zerogap . && docker run -p 8000:8000 --env-file .env -v $PWD/data:/app/data -v $PWD/secrets:/app/secrets zerogap`).
Put the portal behind your company login / VPN – it shows personal data.

## What is where

| Path | Content |
|---|---|
| `zerogap/pipeline.py` | the two agents, step by step (former n8n nodes, same names) |
| `zerogap/nodes/` | the business logic of each former n8n Code node, ported 1:1 |
| `zerogap/sources/` | ARIS MCP, Google (Gmail/Drive/Docs/Sheets), LLM + e-mail, local demo data |
| `zerogap/web/` | web server (portal, approve/reject links, upload, exports) and the portal page |
| `data/` | demo data + `zerogap.sqlite` (all runs, steps, letters) + `outbox/` |
| `tests/` | parity tests: Python output vs the recorded output of the original n8n nodes (`python -m tests.run_all`) |

## Exports for ARIS Process Mining
- `GET /api/export/event-log.csv` – one row per step of every run (case = run, activity, start/end, duration)
- `GET /api/export/cases.csv` – one row per run with the KPI fields (person match, next step, items, risks, decision)
