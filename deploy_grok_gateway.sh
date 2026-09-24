#!/usr/bin/env bash
# deploy_grok_gateway.sh — развернуть "newapi для grok" на EU-сервере (Ubuntu/Debian).
#
# Использование (на сервере, от root):
#   bash deploy_grok_gateway.sh
#
# Что делает:
#   1. ставит Docker + compose plugin (если нет)
#   2. создаёт /opt/grok-gateway/{grok2api,new-api}
#   3. пишет grok2api config.yaml ( jwtSecret + credentialEncryptionKey + admin — ГЕНЕРИТСЯ на месте)
#   4. docker compose up -d (grok2api + new-api)
#   5. печатает: IP панели new-api, admin-пароль grok2api, следующий шаг (импорт аккаунтов + канал)
#
# Секреты генерируются локально и пишутся в /opt/grok-gateway/SECRETS.txt (chmod 600).
# Миграция аккаунтов (SSO) — отдельным шагом с локальной машины (см. migrate_accounts.py).
set -euo pipefail

GW_DIR=/opt/grok-gateway
mkdir -p "$GW_DIR"/{grok2api,new-api/data,new-api/logs}
cd "$GW_DIR"

echo "[1/5] Docker..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing"; exit 1; }

echo "[2/5] generate secrets..."
if [ ! -f SECRETS.txt ]; then
  JWT=$(openssl rand -hex 32)
  CRED=$(openssl rand -base64 32)
  ADMIN_PW=$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)
  cat > SECRETS.txt <<EOF
# grok-gateway secrets — KEEP PRIVATE
grok2api_admin_user=admin
grok2api_admin_pass=$ADMIN_PW
jwtSecret=$JWT
credentialEncryptionKey=$CRED
EOF
  chmod 600 SECRETS.txt
  echo "  generated -> $GW_DIR/SECRETS.txt"
else
  echo "  SECRETS.txt exists, reusing"
fi
# shellcheck disable=SC1091
source SECRETS.txt

echo "[3/5] write grok2api config.yaml..."
cat > grok2api/config.yaml <<EOF
server:
  listen: "0.0.0.0:8000"
  maxBodyBytes: 33554432
  trustedProxies: []
  readTimeout: 15m
  requestTimeout: 2h
  swaggerEnabled: false
auth:
  accessTokenTTL: 15m
  refreshTokenTTL: 720h
  secureCookies: false
secrets:
  jwtSecret: "$jwtSecret"
  credentialEncryptionKey: "$credentialEncryptionKey"
bootstrapAdmin:
  username: "$grok2api_admin_user"
  password: "$grok2api_admin_pass"
frontend:
  staticPath: "./frontend/dist"
database:
  driver: sqlite
  sqlite:
    path: "./data/backend.db"
runtimeStore:
  driver: memory
deployment:
  replicas: 1
  clusterID: "grok-gateway"
  sharedMedia: false
media:
  driver: local
  local:
    path: "./data/media"
routing:
  reasoningReplayEnabled: true
  segmentedSelectorEnabled: true
  segmentedSelectorMinCandidates: 3000
  segmentedSelectorWindowSize: 64
audit:
  bufferSize: 16384
  batchSize: 256
  flushInterval: 250ms
  retentionDays: 7
  ledgerMode: enforce
qualityGuard:
  enabled: false
EOF

echo "[4/5] docker compose up..."
# compose файл ожидается рядом (docker-compose.grok.yml) — копируем если нет
if [ ! -f docker-compose.grok.yml ]; then
  echo "  ERROR: docker-compose.grok.yml not found in $GW_DIR" >&2
  exit 1
fi
docker compose -f docker-compose.grok.yml up -d

echo "[5/5] waiting for health..."
for i in $(seq 1 30); do
  sleep 2
  if curl -fs http://127.0.0.1:8000/healthz >/dev/null 2>&1 && \
     curl -fs http://127.0.0.1:3000/api/status >/dev/null 2>&1; then
    echo "  both UP"
    break
  fi
done

IP=$(curl -fs --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "================ DEPLOYED ================"
echo "new-api panel : http://$IP:3000  (root / 123456 — СМЕНИТЬ!)"
echo "grok2api      : http://127.0.0.1:8000 (internal)"
echo "grok2api admin: $grok2api_admin_user / $grok2api_admin_pass"
echo "secrets file  : $GW_DIR/SECRETS.txt"
echo ""
echo "NEXT:"
echo "  1. migrate accounts:  python migrate_accounts.py  (с локальной машины, льёт SSO в grok2api)"
echo "  2. new-api: Channels -> Add -> OpenAI, base http://grok2api:8000, key g2a_xxx, models grok-4.6,grok-4.7,grok-chat-fast"
echo "  3. new-api: Tokens -> Add -> sk-xxx для потребителей"
echo "==========================================="
