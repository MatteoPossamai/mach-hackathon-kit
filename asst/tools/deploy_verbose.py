"""Come `city deploy`, ma stampa anche status/step/detail del server a ogni cambio,
compresa la posizione nella coda globale dei rollout ("waiting for a rollout slot, N ahead").

Uso, dalla cartella asst/:
    ~/.local/share/uv/tools/city-cli/bin/python tools/deploy_verbose.py deploy <agente>
"""
import sys, time
import city_deploy.cli as cli

_orig_get, _orig_post = cli._Client.get, cli._Client.post
_last = {}

def _show(path, response):
    try:
        body = response.json()
    except Exception:
        return
    if not isinstance(body, dict):
        return
    key = (body.get("status"), body.get("step"), body.get("detail"))
    if _last.get(path) != key:
        _last[path] = key
        print(f"[{time.strftime('%H:%M:%S')}] {path} status={key[0]} step={key[1]} detail={key[2]} outcome={body.get('outcome')}", file=sys.stderr, flush=True)

def get(self, path, *a, **k):
    r = _orig_get(self, path, *a, **k)
    if "/deployments/" in path:
        _show(path, r)
    return r

def post(self, path, *a, **k):
    r = _orig_post(self, path, *a, **k)
    if path.startswith("/deployments") or path.startswith("/uploads/"):
        _show(path, r)
        try:
            print(f"[{time.strftime('%H:%M:%S')}] POST {path} -> {r.status_code} deployment_id={r.json().get('deployment_id')}", file=sys.stderr, flush=True)
        except Exception:
            pass
    return r

cli._Client.get, cli._Client.post = get, post
sys.argv = ["city"] + sys.argv[1:]
sys.exit(cli.main())
