#!/bin/bash
# Run this once from your VM or local machine to deploy the kill switch.
# Prerequisites: gcloud CLI authenticated, billing API enabled.
#
# Usage: ./deploy.sh [PROJECT_ID]
# Set BILLING_ACCOUNT_ID env var before running, or edit the variable below.

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project)}"
BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-YOUR_BILLING_ACCOUNT_ID}"   # e.g. "01ABCD-123456-XXXXXX"
REGION="us-central1"
TOPIC_NAME="billing-killswitch"
FUNCTION_NAME="billing-killswitch"
SA_NAME="billing-killswitch-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if [[ "$BILLING_ACCOUNT_ID" == "YOUR_BILLING_ACCOUNT_ID" ]]; then
  echo "❌ Set BILLING_ACCOUNT_ID before running:"
  echo "   export BILLING_ACCOUNT_ID=01ABCD-123456-XXXXXX"
  echo "   ./deploy.sh"
  exit 1
fi

echo "🚀 Deploying billing kill switch to project: $PROJECT_ID"

# 1. Enable required APIs
echo "→ Enabling APIs..."
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  pubsub.googleapis.com \
  cloudbilling.googleapis.com \
  run.googleapis.com \
  --project="$PROJECT_ID"

# 2. Create Pub/Sub topic
echo "→ Creating Pub/Sub topic: $TOPIC_NAME"
gcloud pubsub topics create "$TOPIC_NAME" \
  --project="$PROJECT_ID" 2>/dev/null || echo "  (topic already exists)"

# 3. Create dedicated service account
echo "→ Creating service account: $SA_NAME"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Billing Kill Switch" \
  --project="$PROJECT_ID" 2>/dev/null || echo "  (SA already exists)"

# 4. Grant the SA permission to disable billing on the billing account
echo "→ Granting billing.admin to service account..."
gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/billing.admin"

# 5. Deploy the Cloud Function (Gen 2)
echo "→ Deploying Cloud Function..."
gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime=python312 \
  --region="$REGION" \
  --source=. \
  --entry-point=billing_killswitch \
  --trigger-topic="$TOPIC_NAME" \
  --service-account="$SA_EMAIL" \
  --set-env-vars="BILLING_ACCOUNT_ID=${BILLING_ACCOUNT_ID}" \
  --memory=256MB \
  --timeout=60s \
  --project="$PROJECT_ID"

echo ""
echo "✅ Kill switch deployed!"
echo ""
echo "👉 FINAL STEP — link the Pub/Sub topic to your budget alert:"
echo "   1. Go to: https://console.cloud.google.com/billing/${BILLING_ACCOUNT_ID}/budgets"
echo "   2. Click your budget alert"
echo "   3. Scroll to 'Manage notifications' → check 'Connect a Pub/Sub topic'"
echo "   4. Select project: $PROJECT_ID  |  Topic: $TOPIC_NAME"
echo "   5. Save"
echo ""
echo "That's it. When spend hits your budget — billing auto-disables across all projects."
