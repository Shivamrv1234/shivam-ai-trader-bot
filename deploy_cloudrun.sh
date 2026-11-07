#!/usr/bin/env bash
# Deploys the bot to Google Cloud Run and connects Telegram to it (instant replies).
# Run inside Google Cloud Shell, from the repository folder:
#     bash deploy_cloudrun.sh            # first deploy, or redeploy after code changes
#     bash deploy_cloudrun.sh disconnect # stop instant replies (GitHub checks take over again)
set -euo pipefail

SERVICE="shivam-trader-bot"
REGION="${REGION:-us-central1}"   # free-tier outbound data is counted from North America

PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
if [ -z "$PROJECT" ]; then
  echo "No Google Cloud project selected. Run:  gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi
echo "Project: $PROJECT   Region: $REGION   Service: $SERVICE"

ask_secret() {  # hidden input; keeps an existing value if you just press Enter on a redeploy
  local name="$1" value=""
  read -rsp "$name (input hidden, Enter to keep current): " value; echo >&2
  printf '%s' "$value"
}
ask_plain() {
  local name="$1" value=""
  read -rp "$name (Enter to keep current): " value
  printf '%s' "$value"
}

if [ "${1:-}" = "disconnect" ]; then
  TOKEN="$(ask_secret TELEGRAM_BOT_TOKEN)"
  curl -s "https://api.telegram.org/bot${TOKEN}/deleteWebhook" ; echo
  echo "Disconnected. Re-enable the 'Bot commands' workflow on GitHub if you want GitHub to answer again."
  exit 0
fi

echo "Turning on the needed Google Cloud services (first time takes a minute)..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --quiet

EXISTS="no"
if gcloud run services describe "$SERVICE" --region "$REGION" >/dev/null 2>&1; then EXISTS="yes"; fi
if [ "$EXISTS" = "yes" ]; then
  echo "Service exists: press Enter on any value you don't want to change."
fi

echo "Enter your settings (same values as your GitHub secrets):"
TELEGRAM_BOT_TOKEN="$(ask_secret TELEGRAM_BOT_TOKEN)"
TELEGRAM_CHAT_ID="$(ask_plain TELEGRAM_CHAT_ID)"
ANGEL_API_KEY="$(ask_secret ANGEL_API_KEY)"
ANGEL_CLIENT_CODE="$(ask_plain ANGEL_CLIENT_CODE)"
ANGEL_PIN="$(ask_secret ANGEL_PIN)"
ANGEL_TOTP_SECRET="$(ask_secret ANGEL_TOTP_SECRET)"
GEMINI_API_KEY="$(ask_secret 'GEMINI_API_KEY (optional)')"
GEMINI_MODEL="$(ask_plain 'GEMINI_MODEL (optional, e.g. a current Flash model ID)')"

if [ "$EXISTS" = "no" ] && { [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; }; then
  echo "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required on the first deploy."
  exit 1
fi

# Write only the values you typed into a private temporary file (deleted afterwards).
ENVFILE="$(mktemp)"
chmod 600 "$ENVFILE"
trap 'rm -f "$ENVFILE"' EXIT
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID ANGEL_API_KEY ANGEL_CLIENT_CODE ANGEL_PIN ANGEL_TOTP_SECRET \
       GEMINI_API_KEY GEMINI_MODEL
python3 - "$ENVFILE" <<'PY'
import json, os, sys
keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN",
        "ANGEL_TOTP_SECRET", "GEMINI_API_KEY", "GEMINI_MODEL"]
vals = {k: os.environ.get(k, "").strip() for k in keys}
vals = {k: v for k, v in vals.items() if v}
with open(sys.argv[1], "w") as fh:
    for k, v in vals.items():
        fh.write(f"{k}: {json.dumps(v)}\n")   # JSON strings are valid YAML
print(f"{len(vals)} setting(s) will be saved on the service.")
PY

ENV_FLAG=()
if [ -s "$ENVFILE" ]; then
  if [ "$EXISTS" = "yes" ]; then ENV_FLAG=(--update-env-vars "$(python3 - "$ENVFILE" <<'PY'
import json, sys
pairs = []
for line in open(sys.argv[1]):
    k, v = line.rstrip("\n").split(": ", 1)
    pairs.append(f"{k}={json.loads(v)}")
print("^@^" + "@".join(pairs))   # '@' as separator so commas in values are safe
PY
)")
  else ENV_FLAG=(--env-vars-file "$ENVFILE"); fi
fi

echo "Building and deploying (about 3-6 minutes the first time)..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 \
  --concurrency 1 --max-instances 3 --min-instances 0 \
  --timeout 300 \
  "${ENV_FLAG[@]}" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
echo "Service URL: $URL"
curl -s "$URL/" ; echo

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "Token not re-entered, so the Telegram connection was left as it is."
  exit 0
fi
SECRET="$(printf '%s' "$TELEGRAM_BOT_TOKEN" | sha256sum | cut -c1-32)"
echo "Connecting Telegram to the service..."
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${URL}/telegram" \
  --data-urlencode "secret_token=${SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' \
  --data-urlencode "max_connections=4" ; echo
echo
echo "Done. Send /start to your bot: it should reply within seconds (the first reply after a quiet"
echo "period can take 10-20 seconds while the server wakes up)."
echo "Now open GitHub -> Actions -> 'Bot commands' -> '...' -> Disable workflow (alerts keep running)."
