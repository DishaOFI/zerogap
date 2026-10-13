"""Parity test: runs a Python node on the inputs recorded from the original n8n (JS) node and compares the output.
Usage: python -m tests.parity <module> [fixture-glob]   e.g.  python -m tests.parity build_case_file"""
from __future__ import annotations
import datetime as dt, glob, importlib, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zerogap.ctx import Ctx  # noqa: E402

NOW = dt.datetime(2026, 10, 12, 5, 0, 0, tzinfo=dt.timezone.utc)
# module -> (node name, input node name per agent)
NODES = {
    'build_case_file': ('Build Case File', {'aris': 'Read SOP Text', 'zero': 'Read SOP Text'}),
    'build_aris_history_query': ('Build ARIS History Query', {'aris': 'Build Case File'}),
    'aris_case_history': ('ARIS Case History', {'aris': None}),          # input = the MCP answer (rebuilt below)
    'apply_sop_rules': ('Apply SOP Rules', {'aris': 'ARIS Case History', 'zero': 'Look Up Applicant in Event Log'}),
    'completeness_rules_engine': ('Completeness Rules Engine', {'aris': 'Apply SOP Rules', 'zero': 'Apply SOP Rules'}),
    'compliance_controls_aris': ('Compliance Controls (ARIS)', {'aris': 'Completeness Rules Engine'}),
    'guardrails': ('Guardrails: Validate AI Draft', {'aris': 'AI'}),
    'zero_context_agent': ('Zero-Context Agent', {'zero': 'Completeness Rules Engine'}),
    'prepare_letter_review_email': ('Prepare Letter & Review Email', {'zero': None}),
    'prepare_dispatch': ('Prepare Dispatch', {'aris': None}),
}
VOLATILE = {'written_at', 'generated_at', 'checked_at', 'reviewed_at',
            'review_facts'}   # written later onto the same object by the review renderer (n8n object sharing)


def diff(a, b, path='', out=None, limit=40):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in VOLATILE:
                continue
            if k not in a:
                out.append(f'{path}.{k}: missing in python (js={json.dumps(b[k], ensure_ascii=False)[:120]})')
            elif k not in b:
                out.append(f'{path}.{k}: extra in python (py={json.dumps(a[k], ensure_ascii=False)[:120]})')
            else:
                diff(a[k], b[k], f'{path}.{k}', out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f'{path}: list length py={len(a)} js={len(b)}')
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f'{path}[{i}]', out, limit)
    else:
        if isinstance(a, float) and isinstance(b, (int, float)) and not isinstance(b, bool):
            if not (math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)):
                out.append(f'{path}: py={a!r} js={b!r}')
        elif isinstance(a, int) and isinstance(b, float) and float(a) == b:
            pass
        elif a != b:
            out.append(f'{path}: py={json.dumps(a, ensure_ascii=False)[:160]} js={json.dumps(b, ensure_ascii=False)[:160]}')
    return out


def run(module: str, pattern: str = '*') -> bool:
    node, inputs = NODES[module]
    mod = importlib.import_module(f'zerogap.nodes.{module}')
    ok = True
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), 'fixtures', pattern + '.json')))
    for f in files:
        fx = json.load(open(f, encoding='utf-8'))
        agent = fx['agent']
        if agent not in inputs or node not in fx['nodes']:
            continue
        nodes = {k: [it['json'] for it in v] for k, v in fx['nodes'].items()}
        order = list(nodes)
        prior = {k: nodes[k] for k in order[:order.index(node)]}
        src = inputs[agent]
        if src is None:
            items = fx.get('inputs', {}).get(node) or nodes.get('__input__' + node) or [{}]
        else:
            items = nodes[src]
        ctx = Ctx(now=NOW, outputs=dict(prior))
        try:
            got = mod.run(ctx, items)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            print(f'FAIL {os.path.basename(f)}: exception {e!r}'); ok = False; continue
        want = nodes[node]
        d = diff(got, want)
        if d:
            ok = False
            print(f'FAIL {os.path.basename(f)} ({len(d)} differences, first ones):')
            for x in d[:25]:
                print('   ', x)
        else:
            print(f'ok   {os.path.basename(f)}')
    return ok


if __name__ == '__main__':
    sys.exit(0 if run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '*') else 1)
