#!/usr/bin/env bash
# Deploys the bot to Google Cloud Run and connects Telegram to it (instant replies).
# Run inside Google Cloud Shell, from the repository folder:
#     bash deploy_cloudrun.sh            # first deploy, or redeploy after code changes
#     bash deploy_cloudrun.sh disconnect # stop instant replies (GitHub checks take over again)
#     bash deploy_cloudrun.sh cleanup    # only delete old container images (stay inside the free 0.5 GB)
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

# Container images: every deploy stores a new image (~150-250 MB) in Artifact Registry, whose free
# storage is 0.5 GB. Keep the 2 newest (and the one the service runs); delete the rest now, and set a
# cleanup policy so Google also deletes anything older than 1 day automatically (it runs about daily).
REPO="cloud-run-source-deploy"
cleanup_images() {
  local image="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}" live="" policy
  policy="$(mktemp)"
  cat > "$policy" <<'JSON'
[
  {"name": "delete-older-than-1-day", "action": {"type": "Delete"},
   "condition": {"tagState": "any", "olderThan": "86400s"}},
  {"name": "keep-2-newest", "action": {"type": "Keep"},
   "mostRecentVersions": {"keepCount": 2}}
]
JSON
  echo "Setting the automatic image cleanup policy (keep 2 newest, delete older than 1 day)..."
  gcloud artifacts repositories set-cleanup-policies "$REPO" --project="$PROJECT" --location="$REGION" \
    --policy="$policy" --no-dry-run --quiet >/dev/null \
    && echo "Cleanup policy set." || echo "Could not set the cleanup policy (images will still be cleaned below)."
  rm -f "$policy"

  live="$(gcloud run revisions describe "$(gcloud run services describe "$SERVICE" --region "$REGION" \
          --format 'value(status.latestReadyRevisionName)' 2>/dev/null)" --region "$REGION" \
          --format 'value(status.imageDigest)' 2>/dev/null || true)"
  gcloud artifacts docker images list "$image" --format=json 2>/dev/null > /tmp/bot-images.json || echo "[]" > /tmp/bot-images.json
  python3 - "$live" /tmp/bot-images.json > /tmp/bot-old-images.txt <<'PY'
import json, sys
live, path = sys.argv[1], sys.argv[2]
try:
    imgs = json.load(open(path))
except ValueError:
    imgs = []
imgs.sort(key=lambda i: i.get("updateTime") or i.get("createTime") or "", reverse=True)
for img in imgs[2:]:                      # keep the 2 newest
    digest = img.get("version", "")
    if digest and digest not in live:     # never the image the service is running
        print(digest)
PY
  local n=0
  while read -r digest; do
    [ -z "$digest" ] && continue
    gcloud artifacts docker images delete "${image}@${digest}" --delete-tags --quiet >/dev/null 2>&1 && n=$((n+1))
  done < /tmp/bot-old-images.txt
  rm -f /tmp/bot-images.json /tmp/bot-old-images.txt
  echo "Deleted $n old image(s)."
  local bytes
  bytes="$(gcloud artifacts repositories describe "$REPO" --location="$REGION" --format='value(sizeBytes)' 2>/dev/null || true)"
  if [ -n "$bytes" ]; then
    python3 -c "import sys; b=int(sys.argv[1]); print(f'Image storage now: {b/1e6:.0f} MB of the free 500 MB')" "$bytes" || true
  fi

  # Source uploads (small) also go to a Cloud Storage bucket: delete them after 1 day as well.
  local life
  life="$(mktemp)"
  echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}' > "$life"
  for b in "run-sources-${PROJECT}-${REGION}" "${PROJECT}_cloudbuild"; do
    if gcloud storage buckets describe "gs://$b" >/dev/null 2>&1; then
      gcloud storage buckets update "gs://$b" --lifecycle-file="$life" >/dev/null 2>&1 \
        && echo "Bucket gs://$b: uploads auto-delete after 1 day."
    fi
  done
  rm -f "$life"
}

if [ "${1:-}" = "cleanup" ]; then
  cleanup_images
  exit 0
fi

if [ "${1:-}" = "disconnect" ]; then
  TOKEN="$(ask_secret TELEGRAM_BOT_TOKEN)"
  curl -s "https://api.telegram.org/bot${TOKEN}/deleteWebhook" ; echo
  echo "Disconnected. Re-enable the 'Bot commands' workflow on GitHub if you want GitHub to answer again."
  exit 0
fi

echo "Turning on the needed Google Cloud services (first time takes a minute)..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  storage.googleapis.com --quiet

# Paper trades and approved group chats live in a small JSON file in Cloud Storage (Cloud Run's own disk is
# wiped on every restart). Regional buckets in us-central1 are inside Google's free tier (5 GB).
BUCKET="${BUCKET:-${PROJECT}-shivam-bot-data}"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  echo "Creating the storage bucket gs://${BUCKET} for paper trades..."
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access --quiet
fi
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUN_SA="$(gcloud run services describe "$SERVICE" --region "$REGION" \
          --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
RUN_SA="${RUN_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
echo "Letting the bot (${RUN_SA}) read and write only that bucket..."
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${RUN_SA}" \
  --role="roles/storage.objectAdmin" --quiet >/dev/null
BOT_STORE="gs://${BUCKET}/store.json"

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
TWELVE_DATA_API_KEY="$(ask_secret 'TWELVE_DATA_API_KEY (optional: live forex/gold)')"
ALLOWED_CHATS="$(ask_plain 'ALLOWED_CHATS (optional: group IDs, comma separated; or use /allow in the group)')"
ALERT_CHATS="$(ask_plain 'ALERT_CHATS (optional: extra chats that also get alerts)')"

if [ "$EXISTS" = "no" ] && { [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; }; then
  echo "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required on the first deploy."
  exit 1
fi

# Write only the values you typed into a private temporary file (deleted afterwards).
ENVFILE="$(mktemp)"
chmod 600 "$ENVFILE"
trap 'rm -f "$ENVFILE"' EXIT
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID ANGEL_API_KEY ANGEL_CLIENT_CODE ANGEL_PIN ANGEL_TOTP_SECRET \
       GEMINI_API_KEY GEMINI_MODEL TWELVE_DATA_API_KEY ALLOWED_CHATS ALERT_CHATS BOT_STORE
python3 - "$ENVFILE" <<'PY'
import json, os, sys
keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN",
        "ANGEL_TOTP_SECRET", "GEMINI_API_KEY", "GEMINI_MODEL", "TWELVE_DATA_API_KEY", "ALLOWED_CHATS",
        "ALERT_CHATS", "BOT_STORE"]
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

cleanup_images

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
  --data-urlencode "max_connections=1" ; echo   # one message at a time: the bot's memory stays on one server
echo
echo "Done. Send /start to your bot: it should reply within seconds (the first reply after a quiet"
echo "period can take 10-20 seconds while the server wakes up)."
echo "Now open GitHub -> Actions -> 'Bot commands' -> '...' -> Disable workflow (alerts keep running)."
echo "Paper trades are saved in ${BOT_STORE}."
echo "Groups: add the bot to a group, then send /allow there (only you, the owner, can approve a group)."
