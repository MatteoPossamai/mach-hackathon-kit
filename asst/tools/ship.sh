#!/usr/bin/env bash
# Porta su main il lavoro pronto del branch diego/live, poi rilascia UN agente.
#
# Uso, da qualunque cartella del repo:
#   bash asst/tools/ship.sh <agente>        # es. bash asst/tools/ship.sh booking_centre
#
# Passi: pull di main -> merge di origin/diego/live -> push di main -> deploy con
# deploy_verbose.py (mostra la coda) -> se il deploy riesce, commit e push della card.
set -euo pipefail

agent="${1:?uso: bash asst/tools/ship.sh <agente>}"
root="$(git rev-parse --show-toplevel)"
cd "$root"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Ci sono modifiche non salvate: fai commit o stash prima di ship." >&2
  exit 1
fi

git checkout -q main
git pull -q --no-edit origin main
if git ls-remote --exit-code --heads origin diego/live >/dev/null 2>&1; then
  git fetch -q origin diego/live
  if ! git merge --no-edit -q origin/diego/live; then
    git merge --abort || true
    echo "Conflitto tra main e diego/live: avvisa Claude, nessun deploy fatto." >&2
    exit 1
  fi
fi
git push -q origin main
echo "main aggiornato: $(git log --oneline -1)"

cd "$root/asst"
"$HOME/.local/share/uv/tools/city-cli/bin/python" tools/deploy_verbose.py deploy "$agent" || {
  echo "Deploy non concluso. Se e' UPSTREAM_TIMEOUT continua sulla city: city status $agent. Non rilanciare." >&2
  exit 1
}

if ! git diff --quiet -- "$agent/card.yaml"; then
  git add "$agent/card.yaml"
  git commit -q -m "$agent: card dopo deploy"
  git push -q origin main
  echo "card di $agent salvata su main"
fi
