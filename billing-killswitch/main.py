"""
Billing Kill Switch — Cloud Function (Gen 2)

Triggered by a Pub/Sub budget alert. When actual spend >= budget amount,
it immediately unlinks billing from all projects in the account, stopping
all paid GCP resource consumption.

Deploy once, forget about it. It self-protects your $2K credit.
"""

import base64
import json
import logging
import os

import functions_framework
from google.cloud import billing_v1

log = logging.getLogger(__name__)

BILLING_ACCOUNT_ID = os.environ["BILLING_ACCOUNT_ID"]   # e.g. "01ABCD-123456-XXXXXX"
# Optional: protect specific projects from being unlinked (e.g. prod)
PROTECTED_PROJECTS = set(
    p.strip()
    for p in os.environ.get("PROTECTED_PROJECTS", "").split(",")
    if p.strip()
)


@functions_framework.cloud_event
def billing_killswitch(cloud_event):
    """Entry point — receives Pub/Sub CloudEvent from budget alert."""
    try:
        # Decode the Pub/Sub message
        raw = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        budget_data = json.loads(raw)
        log.info(f"Budget alert received: {budget_data}")

        cost_amount = float(budget_data.get("costAmount", 0))
        budget_amount = float(budget_data.get("budgetAmount", 1))
        cost_interval_start = budget_data.get("costIntervalStart", "unknown")

        log.info(f"Cost: ${cost_amount:.2f} / Budget: ${budget_amount:.2f}")

        # Only kill switch if we've actually hit or exceeded 100%
        if cost_amount < budget_amount:
            log.info("Under budget — no action taken.")
            return

        log.warning(
            f"🚨 BUDGET EXCEEDED: ${cost_amount:.2f} >= ${budget_amount:.2f}. "
            f"Disabling billing on all projects."
        )

        _disable_billing_all_projects()

    except Exception as e:
        log.error(f"Kill switch error: {e}", exc_info=True)
        raise


def _disable_billing_all_projects() -> None:
    """Unlink billing from every project attached to this billing account."""
    client = billing_v1.CloudBillingClient()
    billing_account = f"billingAccounts/{BILLING_ACCOUNT_ID}"

    # List all projects on this billing account
    projects = list(
        client.list_project_billing_info(name=billing_account)
    )

    if not projects:
        log.warning("No projects found on billing account.")
        return

    for project_billing in projects:
        project_id = project_billing.project_id

        if project_id in PROTECTED_PROJECTS:
            log.info(f"Skipping protected project: {project_id}")
            continue

        if not project_billing.billing_enabled:
            log.info(f"Billing already disabled on {project_id}")
            continue

        try:
            # Unlink billing — this immediately stops all paid usage
            client.update_project_billing_info(
                name=f"projects/{project_id}",
                project_billing_info=billing_v1.ProjectBillingInfo(
                    billing_account_name=""   # empty = disable billing
                ),
            )
            log.info(f"✅ Billing disabled on project: {project_id}")
        except Exception as e:
            log.error(f"Failed to disable billing on {project_id}: {e}")
