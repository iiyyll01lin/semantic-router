#!/usr/bin/env bash
#
# install-lemonade.sh -- idempotent provisioner for the Lemonade Server on a Strix Halo box.
#
# Lemonade (lemonade-sdk) ships a local OpenAI-compatible server plus, on Linux,
# a llama.cpp(rocm) backend and an EXPERIMENTAL vLLM+rocm backend that already
# targets gfx1151 -- which is the practical vLLM workaround for Strix Halo today.
#
# Run this ONCE PER BOX (Halo-A and Halo-B). It is safe to re-run.
#
# Env (all optional):
#   PORT         serve port                       (default 13305 -- Lemonade default)
#   START        1 => launch `lemonade-server serve` after install (default 0)
#   PULL_MODEL   model id to pre-pull, e.g. Qwen2.5-7B-Instruct-GGUF (default: none)
#   PIPX         1 => prefer pipx (isolated)      (default 1, falls back to pip --user)
#
# Usage:  bash install-lemonade.sh                 # install + verify
#         START=1 bash install-lemonade.sh         # install, then serve on :13305
set -uo pipefail

PORT="${PORT:-13305}"
START="${START:-0}"
PULL_MODEL="${PULL_MODEL:-}"
PIPX="${PIPX:-1}"

have() { command -v "$1" >/dev/null 2>&1; }

echo "==> [lemonade] target port=${PORT}  host=$(hostname 2>/dev/null || echo box)"

if have lemonade-server; then
  echo "==> [lemonade] already installed: $(lemonade-server --version 2>/dev/null || echo present)"
else
  installed=0
  if [[ "${PIPX}" == "1" ]] && have pipx; then
    echo "==> [lemonade] installing via pipx ..."
    if pipx install lemonade-sdk; then installed=1; fi
  fi
  if [[ "${installed}" != "1" ]]; then
    PY_BIN=""
    for c in python3 python; do have "$c" && { PY_BIN="$c"; break; }; done
    [[ -n "${PY_BIN}" ]] || { echo "ERROR: no python3/python found to install lemonade-sdk" >&2; exit 1; }
    echo "==> [lemonade] installing via ${PY_BIN} -m pip install --user ..."
    "${PY_BIN}" -m pip install --user --upgrade lemonade-sdk || {
      echo "ERROR: pip install lemonade-sdk failed" >&2; exit 1; }
    # user-base bin may not be on PATH yet
    USER_BIN="$("${PY_BIN}" -c 'import site,sys,os;print(os.path.join(site.USER_BASE,"bin"))' 2>/dev/null)"
    [[ -n "${USER_BIN}" && ":${PATH}:" != *":${USER_BIN}:"* ]] && export PATH="${USER_BIN}:${PATH}"
  fi
fi

if ! have lemonade-server; then
  echo "ERROR: lemonade-server still not on PATH after install." >&2
  echo "       Add your pip user bin dir to PATH (e.g. ~/.local/bin) and re-run." >&2
  exit 1
fi
echo "==> [lemonade] installed OK: $(lemonade-server --version 2>/dev/null || echo present)"

if [[ -n "${PULL_MODEL}" ]]; then
  echo "==> [lemonade] pulling model: ${PULL_MODEL}"
  lemonade-server pull "${PULL_MODEL}" || echo "WARN: pull failed (continuing)"
fi

if [[ "${START}" == "1" ]]; then
  echo "==> [lemonade] starting server on :${PORT} (background)"
  pkill -f "lemonade-server serve" 2>/dev/null || true
  nohup lemonade-server serve --port "${PORT}" >/tmp/lemonade-server.log 2>&1 &
  for _ in $(seq 1 30); do
    curl -fsS "http://localhost:${PORT}/api/v1/models" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -fsS "http://localhost:${PORT}/api/v1/models" >/dev/null 2>&1; then
    echo "==> [lemonade] serving at http://localhost:${PORT}/api/v1 (log: /tmp/lemonade-server.log)"
  else
    echo "WARN: server not answering yet on :${PORT}; check /tmp/lemonade-server.log" >&2
  fi
else
  echo "==> [lemonade] to serve:  lemonade-server serve --port ${PORT}"
fi
