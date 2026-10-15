#!/usr/bin/env bash
set -euo pipefail

: "${AZURE_SUBSCRIPTION_ID:?Missing subscription}"
: "${AZURE_OPERATOR_OBJECT_ID:?Missing operator object ID}"
: "${AZURE_RESOURCE_GROUP:?Missing resource group}"
: "${AZURE_LOCATION:?Missing location}"
: "${RUNNER_TEMP:?This script runs in GitHub Actions}"
: "${GITHUB_RUN_ID:?Missing GitHub run ID}"
: "${GITHUB_RUN_ATTEMPT:?Missing GitHub run attempt}"
: "${OPERATION:?Missing operation}"

case "$OPERATION" in
  preview|deploy) ;;
  *) printf 'Unsupported operation\n' >&2; exit 2 ;;
esac

actual_subscription=$(az account show --query id --output tsv)
if [ "$actual_subscription" != "$AZURE_SUBSCRIPTION_ID" ]; then
  printf 'Unexpected subscription\n' >&2
  exit 1
fi
actual_location=$(az group show --name "$AZURE_RESOURCE_GROUP" --query location --output tsv)
if [ "$actual_location" != "$AZURE_LOCATION" ]; then
  printf 'Unexpected resource-group location\n' >&2
  exit 1
fi

az bicep build --file infra/pilot.bicep --stdout > /dev/null
PARAMETERS_FILE="$RUNNER_TEMP/pilot-parameters.json"
PREVIEW_FILE="$RUNNER_TEMP/pilot-what-if.json"
export PARAMETERS_FILE PREVIEW_FILE
python3 - <<'PY'
import datetime
import json
import os
from pathlib import Path

parameters = {
    "location": os.environ["AZURE_LOCATION"],
    "environmentName": "devis-mngt-dev-ed16",
    "sessionId": "github-" + os.environ["GITHUB_RUN_ID"],
    "deployedBy": "github-actions",
    "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "deployerObjectId": os.environ["AZURE_OPERATOR_OBJECT_ID"],
}
Path(os.environ["PARAMETERS_FILE"]).write_text(json.dumps({
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {key: {"value": value} for key, value in parameters.items()},
}), encoding="utf-8")
PY

az deployment group what-if \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --template-file infra/pilot.bicep \
  --parameters "@$PARAMETERS_FILE" \
  --no-pretty-print --output json > "$PREVIEW_FILE"

python3 - <<'PY'
import json
import os
from collections import Counter
from pathlib import Path

preview = json.loads(Path(os.environ["PREVIEW_FILE"]).read_text())
if preview.get("status") != "Succeeded":
    raise SystemExit("Azure what-if did not succeed")
changes = preview.get("changes", [])
if any(change.get("changeType") == "Delete" for change in changes):
    raise SystemExit("Deployment blocked: deletion detected")
counts = Counter(change.get("changeType", "Unknown") for change in changes)
print("Bicep preview:", ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items())))
PY

if [ "$OPERATION" = deploy ]; then
  DEPLOYMENT_NAME="pilot-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
  az deployment group create \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$DEPLOYMENT_NAME" \
    --template-file infra/pilot.bicep \
    --parameters "@$PARAMETERS_FILE" \
    --only-show-errors --output none
  for account in di-devis-mngt-dev-ed16 aoai-devis-mngt-dev-ed16; do
    state=$(az cognitiveservices account show --resource-group "$AZURE_RESOURCE_GROUP" \
      --name "$account" --query properties.provisioningState --output tsv)
    if [ "$state" != Succeeded ]; then
      printf 'Azure AI resource did not reach Succeeded\n' >&2
      exit 1
    fi
  done
  az cognitiveservices account deployment show --resource-group "$AZURE_RESOURCE_GROUP" \
    --name aoai-devis-mngt-dev-ed16 --deployment-name devis-extraction \
    --query '{state:properties.provisioningState,model:properties.model.name,version:properties.model.version}' \
    --output json
  printf 'Azure AI infrastructure deployed. No quote analysis has been started.\n'
fi
