#!/usr/bin/env bash
# Railway entrypoint. Config comes from $PAI_CONFIG_TOML; the database and a clone of the
# vault repo live on the /data volume. VAULT_TOKEN stays in the environment, never on disk.
set -euo pipefail
DATA="${PAI_DATA:-/data}"
mkdir -p "$DATA"

# Maintenance: keep the container up without starting Pai (e.g. to copy files onto /data).
if [[ -n "${PAI_HOLD:-}" ]]; then
  echo "PAI_HOLD is set: idling instead of starting Pai"
  exec sleep infinity
fi

if [[ -n "${PAI_CONFIG_TOML:-}" ]]; then
  printf '%s\n' "$PAI_CONFIG_TOML" > "$DATA/config.toml"
fi
export PAI_CONFIG="$DATA/config.toml"

if [[ -n "${VAULT_REPO:-}" ]]; then
  git config --global credential.helper \
    '!f() { echo username=x-access-token; echo "password=${VAULT_TOKEN}"; }; f'
  # Who vault commits are from. Set VAULT_GIT_EMAIL to your GitHub noreply address
  # (ID+login@users.noreply.github.com) so GitHub shows them as yours. Never use
  # pai@users.noreply.github.com: that belongs to an unrelated GitHub user named "pai".
  git config --global user.name "${VAULT_GIT_NAME:-Pai}"
  git config --global user.email "${VAULT_GIT_EMAIL:-pai-bot@localhost}"
  [[ -d "$DATA/vault/.git" ]] || git clone -q "$VAULT_REPO" "$DATA/vault"
fi

exec pai
