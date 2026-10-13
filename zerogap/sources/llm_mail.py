"""LLM drafting (OpenAI) and outgoing e-mail (Gmail API, SMTP or a local outbox folder)."""
from __future__ import annotations
import smtplib, time
from email.message import EmailMessage
from pathlib import Path

import httpx


def llm_draft(settings, prompt: str) -> str:
    """Returns the model's raw text (JSON expected). Without an API key it returns '' and the guardrails fall back
    to the deterministic, rule-checked template letter – the same behaviour as an unusable LLM answer in n8n."""
    if not settings.openai_api_key:
        return ''
    r = httpx.post('https://api.openai.com/v1/chat/completions', timeout=90,
                   headers={'Authorization': f'Bearer {settings.openai_api_key}'},
                   json={'model': settings.openai_model, 'temperature': 0.2, 'messages': [{'role': 'user', 'content': prompt}]})
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


def send_mail(settings, google, to: str, subject: str, html: str, attachments: list[tuple[str, bytes, str]] | None = None) -> str:
    """Sends (or, in outbox mode, stores) an HTML e-mail. Returns a short description of what happened."""
    msg = EmailMessage()
    msg['To'] = to
    msg['From'] = settings.mail_from or settings.smtp_user or 'zero-gap-agent@localhost'
    msg['Subject'] = subject
    msg.set_content('This message is HTML – please open it in an e-mail client that shows HTML.')
    msg.add_alternative(html, subtype='html')
    for name, data, mime in attachments or []:
        main, sub = (mime or 'application/octet-stream').split('/', 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    mode = settings.mail_mode
    if mode == 'gmail':
        google.gmail_send(msg.as_bytes())
        return f'sent via Gmail to {to}'
    if mode == 'smtp':
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return f'sent via SMTP to {to}'
    out = Path(settings.data_dir) / 'outbox'
    out.mkdir(parents=True, exist_ok=True)
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in subject)[:80]
    p = out / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.eml"
    p.write_bytes(msg.as_bytes())
    return f'stored in outbox ({p.name}) – not sent'
