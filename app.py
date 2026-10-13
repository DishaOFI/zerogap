"""Zero Gap Agent – replaces the n8n flows.

  python app.py                     start the web app (portal on http://localhost:8000)
  python app.py run FORM.pdf        run both agents once on a form PDF and print the result
  python app.py google-login        one-time Google sign-in (Gmail, Drive, Docs, Sheets)
  python app.py aris-login          one-time ARIS sign-in and list the MCP tools
"""
import json, sys


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'serve'
    from zerogap.config import settings
    if cmd == 'serve':
        import uvicorn
        uvicorn.run('zerogap.web.server:app', host=settings.host, port=settings.port, log_level='info')
    elif cmd == 'google-login':
        from zerogap.sources.google import login
        login(settings.google_client_file, settings.google_token_file)
    elif cmd == 'aris-login':
        from zerogap.sources.aris_mcp import ArisMCP
        print('\n'.join(ArisMCP(settings).list_tools(interactive=True)))
    elif cmd == 'run' and len(argv) > 2:
        import time
        from zerogap.db import DB
        from zerogap.pdftext import pdf_to_text
        from zerogap.pipeline import Pipeline
        from zerogap.web.server import pipeline, db
        text = pdf_to_text(open(argv[2], 'rb').read())
        for agent in settings.agents:
            rid = pipeline.start(agent, {'date': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'from': 'command line', 'subject': argv[2],
                                         'form_text': text, 'source': 'cli'}, background=False)
            r = db.run(rid)
            print(json.dumps({k: r[k] for k in ('id', 'agent', 'status', 'applicant_name', 'insurance_number', 'final_status', 'route', 'error')}, ensure_ascii=False))
    else:
        print(__doc__)


if __name__ == '__main__':
    main(sys.argv)
