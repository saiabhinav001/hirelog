# Backend Cutover and Rollback Runbook

Date: 2026-04-04

This runbook describes how to safely cut over traffic between two backend deployments and how to roll back quickly if service-level checks fail.

## Scope

- API and worker run as separate services.
- Health checks gate all traffic movement.
- Traffic shift is progressive, with monitoring at each stage.
- Rollback is automatic on gate failure and can be executed manually at any time.

## Runtime Topology

- API runtime: app.main:app
- Worker runtime: app.worker_main:app
- Queue backend options:
  - memory (single-process/local)
  - firestore (distributed queue)

## Required Health Endpoints

API:
- GET /health/live
- GET /health/ready
- GET /health/deep

Worker:
- GET /health/live
- GET /health/ready
- GET /health/deep

Deep checks include:
- process status
- Firestore connectivity
- queue metrics
- index freshness
- FAISS vector count (API)

## Container Artifacts

- API image: backend/Dockerfile
- Worker image: backend/Dockerfile.worker
- Local split profile: backend/docker-compose.backend.yml

## Required Environment Variables

- SEARCH_INDEX_WORKER_MODE=embedded|external
- SEARCH_INDEX_QUEUE_BACKEND=memory|firestore
- SEARCH_INDEX_FIRESTORE_COLLECTION=search_index_tasks
- SEARCH_INDEX_MAX_ATTEMPTS=5
- SEARCH_INDEX_LEASE_SECONDS=120
- SEARCH_INDEX_POLL_INTERVAL_SECONDS=0.6
- SEARCH_INDEX_FIRESTORE_CLAIM_BATCH=10
- SEARCH_INDEX_DELETE_DONE_TASKS=true
- SEARCH_INDEX_FRESHNESS_WARN_SECONDS=900

## Automation Scripts

### Cutover Script

Path:
- backend/scripts/blue_green_cutover.py

What it does:
1. Validates deep health for both deployments.
2. Runs a pre-cutover load gate on the candidate deployment.
3. Shifts traffic through configured stages.
4. Runs health and load checks at each stage.
5. Rolls back automatically if any stage fails.
6. Writes a run report.

Default report path:
- docs/reports/cutover-report.json

### Rollback Script

Path:
- backend/scripts/blue_green_rollback.py

What it does:
- Moves traffic back to blue (100/0).
- Optionally validates public health.
- Writes a rollback report.

Default report path:
- docs/reports/rollback-report.json

## DNS Hook Contract

The cutover and rollback scripts expect a hook command template with placeholders:
- {blue_weight}
- {green_weight}
- {stage}

Example helper:
- backend/scripts/set_dns_weight_example.ps1

Provider scripts:
- backend/scripts/set_dns_weight_route53.ps1
- backend/scripts/set_dns_weight_cloudflare.ps1

State artifacts:
- docs/reports/dns-weight-state.json
- docs/reports/dns-weight-route53-state.json
- docs/reports/dns-weight-cloudflare-state.json

### Route53 Prerequisites

- AWS CLI installed and authenticated.
- Hosted zone with weighted records for backend public hostname.
- Stable blue and green target hostnames.

Hook template example:

pwsh -File scripts/set_dns_weight_route53.ps1 -BlueWeight {blue_weight} -GreenWeight {green_weight} -Stage {stage} -HostedZoneId Z123456789ABC -RecordName api.example.com -RecordType CNAME -BlueTarget blue-api.example.com -GreenTarget green-api.example.com

### Cloudflare Prerequisites

- API token with Load Balancer edit permission (CF_API_TOKEN or -ApiToken).
- Existing pool containing named blue and green origins.
- Origin names must match exactly.

Hook template example:

pwsh -File scripts/set_dns_weight_cloudflare.ps1 -BlueWeight {blue_weight} -GreenWeight {green_weight} -Stage {stage} -AccountId <account-id> -PoolId <pool-id> -BlueOrigin blue-origin -GreenOrigin green-origin

## Recommended Run Order

1. Deploy blue and green versions.
2. Validate green readiness and deep health.
3. Execute cutover script with production thresholds.
4. Monitor stage reports and service metrics.
5. Execute rollback immediately if needed.
