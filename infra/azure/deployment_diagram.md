# Azure Deployment Diagram

**Aviation Route Monte Carlo Physical Deployment & Network Topology**

This document is the physical companion to [`architecture.md`](architecture.md). Where architecture explains *why*, deployment explains *how it is laid out*.

Everything below is designed. No resources are provisioned by this repository.

---

## Resource Layout

### Region and Resource Group

| Attribute | Value | Rationale |
|---|---|---|
| Primary region | `uaenorth` (Dubai) | Data residency for UAE-based workloads; low latency for Emirates/Etihad target audience |
| DR region | `westeurope` (Amsterdam) | Geo-redundant backup for ADLS; second region required for GRS storage |
| Resource group | `rg-aviation-mc-prod` | Single RG per environment simplifies RBAC and lifecycle |
| Naming convention | `{service}-{project}-{env}-{region}` | e.g., `adf-aviation-mc-prod-uaenorth` |

### Services Inventory

| # | Service | SKU / Tier | Purpose |
|---|---|---|---|
| 1 | Azure Data Factory | Standard | Pipeline orchestration |
| 2 | Azure Databricks | Premium | ML compute, transformation |
| 3 | ADLS Gen2 | Standard, LRS (Bronze cool, Silver/Gold hot) | Data lake storage |
| 4 | Synapse Analytics | Serverless SQL only | T-SQL serving over Gold |
| 5 | Key Vault | Standard | Secrets management |
| 6 | Log Analytics Workspace | Pay-as-you-go | Centralised logging |
| 7 | Azure Monitor | Included | Alerts, metrics, dashboards |
| 8 | Managed VNet | N/A | Network isolation container |
| 9 | Private Endpoints | Standard | Service-to-service private connectivity |
| 10 | Private DNS Zones | Standard | Name resolution for private endpoints |

---

## Physical Layout Diagram

Azure Subscription: aviation-mc-prod
                   ═══════════════════════════════════════

                   Resource Group: rg-aviation-mc-prod
                   (Region: uaenorth)

 ┌───────────────────────────────────────────────────────────────────┐
 │                                                                   │
 │   Virtual Network: vnet-aviation-mc-prod (10.10.0.0/16)           │
 │                                                                   │
 │   ┌──────────────────┐   ┌──────────────────┐   ┌────────────┐    │
 │   │ subnet-adf       │   │ subnet-databricks│   │ subnet-syn │    │
 │   │ 10.10.1.0/24     │   │ 10.10.2.0/23     │   │ 10.10.4.0  │    │
 │   │                  │   │  (public + priv) │   │  /24       │    │
 │   │  ADF Runtime     │   │  Databricks      │   │  Synapse   │    │
 │   │  (Managed)       │   │  clusters        │   │  Serverless│    │
 │   └────────┬─────────┘   └─────────┬────────┘   └──────┬─────┘    │
 │            │                       │                   │          │
 │            └───────────────────────┼───────────────────┘          │
 │                                    │                              │
 │            ┌───────────────────────┴───────────────────┐          │
 │            │                                           │          │
 │            ▼                                           ▼          │
 │   ┌────────────────────┐                    ┌──────────────────┐  │
 │   │ Private Endpoint   │                    │ Private Endpoint │  │
 │   │ to ADLS Gen2       │                    │ to Key Vault     │  │
 │   │ (10.10.10.4)       │                    │ (10.10.10.5)     │  │
 │   └────────┬───────────┘                    └────────┬─────────┘  │
 │            │                                         │            │
 └────────────┼─────────────────────────────────────────┼────────────┘
              │                                         │
              ▼                                         ▼
 ┌────────────────────────┐             ┌────────────────────────┐
 │  ADLS Gen2 Storage     │             │  Azure Key Vault       │
 │  stavationmcprod       │             │  kv-aviation-mc-prod   │
 │                        │             │                        │
 │  Containers:           │             │  Secrets:              │
 │  /bronze               │             │  - eia-api-key         │
 │  /silver               │             │  - fred-api-key        │
 │  /gold                 │             │  - synapse-connection  │
 │                        │             │  - databricks-workspace│
 │  Public network: OFF   │             │  Public network: OFF   │
 └────────────────────────┘             └────────────────────────┘

          ▲                                          ▲
          │                                          │
          │            Managed Identity              │
          │            authentication                │
          │       (no keys, no passwords)            │
          │                                          │
 ┌────────┴──────────────────────────────────────────┴──────────┐
 │                                                              │
 │   Managed Identities (system-assigned):                      │
 │                                                              │
 │   - id-adf-aviation-mc-prod       → RBAC on ADLS, KV         │
 │   - id-databricks-workspace-mc    → RBAC on ADLS, KV         │
 │   - id-synapse-workspace-mc       → RBAC on ADLS (Gold RO)   │
 │                                                              │
 └──────────────────────────────────────────────────────────────┘

          ▲
          │
          │  Public internet
          │  (limited egress)
          │
 ┌────────┴─────────────────────────────────────┐
 │                                              │
 │  External APIs (allow-listed):               │
 │                                              │
 │  - api.eia.gov            (jet fuel prices)  │
 │  - api.stlouisfed.org     (Brent crude)      │
 │                                              │
 │  Egress via NAT Gateway with static IP       │
 │  (whitelisted with API providers if req'd)   │
 │                                              │
 └──────────────────────────────────────────────┘

 ---

## Data Flow — End-to-End Trace

The following trace shows how data moves from source to consumer. Each hop is a specific service-to-service call.

### 1. Ingestion (nightly, ADF-triggered)

[EIA API]                            [FRED API]              [Emirates CSV manual]
│                                    │                            │
└────────────┬───────────────────────┘                            │
▼                                                    ▼
┌────────────────────┐                             ┌────────────────────┐
│ ADF Copy Activity  │                             │ ADF Copy Activity  │
│ HTTP → JSON        │                             │ Blob → CSV         │
│                    │                             │                    │
│ Auth: Key Vault    │                             │ Auth: Managed ID   │
│ (API keys)         │                             │                    │
└─────────┬──────────┘                             └─────────┬──────────┘
│                                                  │
▼                                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ADLS Gen2 : /bronze/                                                │
│                                                                     │
│   /bronze/external/eia/YYYY-MM-DD.json                              │
│   /bronze/external/fred/YYYY-MM-DD.json                             │
│   /bronze/emirates/annual_reports/YYYY_FY.csv                       │
│                                                                     │
│ Format: raw JSON / CSV (no transformation)                          │
│ Retention: 7 years (regulatory compliance)                          │
└────────────────────────────┬────────────────────────────────────────┘
│

### 2. Transformation (ADF → Databricks notebooks)

                                ▼
                    ┌─────────────────────────┐
                    │ ADF triggers Databricks │
                    │ notebook activity       │
                    │                         │
                    │ Auth: Managed Identity  │
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │ Databricks cluster      │
                    │ (auto-terminate 30m)    │
                    │                         │
                    │ Reads: Bronze           │
                    │ Cleanses, validates,    │
                    │ deduplicates            │
                    │ Writes: Silver (Delta)  │
                    └────────────┬────────────┘
                                 │
                                 ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │ ADLS Gen2 : /silver/                                                │
    │                                                                     │
    │   /silver/fuel/jet_fuel_prices/         (Delta table)               │
    │   /silver/fuel/brent_crude_prices/      (Delta table)               │
    │   /silver/traffic/monthly_load_factor/  (Delta table)               │
    │                                                                     │
    │ Format: Delta Lake (ACID, time-travel enabled)                      │
    └────────────────────────────┬────────────────────────────────────────┘
                                 │
### 3. Modelling (Databricks: Prophet + Monte Carlo)

                                 ▼
                    ┌─────────────────────────┐
                    │ Databricks: Prophet     │
                    │ notebook fits monthly   │
                    │ load factor forecast    │
                    └────────────┬────────────┘
                                 ▼
                    ┌─────────────────────────┐
                    │ Databricks: Monte Carlo │
                    │ notebook runs 10,000    │
                    │ paths × 6 scenarios     │
                    └────────────┬────────────┘
                                 │
                                 ▼
    ┌─────────────────────────────────────────────────────────────────────┐
    │ ADLS Gen2 : /gold/                                                  │
    │                                                                     │
    │   /gold/forecasts/prophet_load_factor/    (Delta table)             │
    │   /gold/simulations/monte_carlo_results/  (Delta table)             │
    │   /gold/risk_metrics/scenario_summary/    (Delta table)             │
    │                                                                     │
    │ Format: Delta Lake, partitioned by run_date                         │
    │ Read-optimised for serving (Z-ordered on scenario, date)            │
    └────────────────────────────┬────────────────────────────────────────┘
                                 │  
 
### 4. Serving (Synapse + Power BI)

                                 ▼
                ┌────────────────────────────────┐
                │ Synapse Serverless SQL Pool    │
                │                                │
                │ External tables over Gold      │
                │ (OPENROWSET + Delta support)   │
                │                                │
                │ Auth: Synapse Managed Identity │
                │ → RBAC on ADLS Gold RO         │
                └───────────────┬────────────────┘
                                │
                                ▼
                ┌────────────────────────────────┐
                │ Consumers                      │
                │                                │
                │  • Power BI dashboards         │
                │  • Analyst SQL queries         │
                │  • Downstream API consumers    │
                └────────────────────────────────┘
---

## Network Topology

### Public Network Access

**Disabled on all data services.** Specifically:
- ADLS Gen2 : public network access = disabled, only Private Endpoint traffic
- Key Vault : public network access = disabled
- Synapse : public network access = disabled
- Databricks : deployed in customer-managed VNet, no public IPs on compute
- ADF : Managed VNet with private endpoints

### Egress to External APIs

Not everything can be private EIA and FRED APIs live on the public internet. Egress is controlled:

- Databricks and ADF egress via a **NAT Gateway** with a **static public IP**
- The static IP is whitelisted with API providers if they require IP whitelisting (both EIA and FRED are open, but pattern is correct for future paid APIs)
- No inbound public traffic to any resource in the platform

### Private DNS Zones

Each service that uses private endpoints requires a Private DNS Zone to resolve its FQDN to the private IP:

| Service | Private DNS Zone |
|---|---|
| ADLS Gen2 (Blob) | `privatelink.blob.core.windows.net` |
| ADLS Gen2 (DFS) | `privatelink.dfs.core.windows.net` |
| Key Vault | `privatelink.vaultcore.azure.net` |
| Synapse | `privatelink.sql.azuresynapse.net` |

These zones are linked to `vnet-aviation-mc-prod` so all VNet resources resolve private endpoints correctly.

---

## Authentication Model

**No credentials in code. No credentials in config. All service-to-service auth via Managed Identity.**

| Caller | Callee | Auth mechanism |
|---|---|---|
| ADF | ADLS | System-assigned Managed Identity + RBAC (`Storage Blob Data Contributor`) |
| ADF | Key Vault | System-assigned Managed Identity + RBAC (`Key Vault Secrets User`) |
| ADF | Databricks | Managed Identity + Databricks workspace token from Key Vault |
| Databricks | ADLS | Databricks workspace Managed Identity + RBAC (`Storage Blob Data Contributor`) |
| Databricks | Key Vault | Azure Key Vault-backed secret scope |
| Synapse | ADLS Gold | Workspace Managed Identity + RBAC (`Storage Blob Data Reader` — read-only) |
| Power BI | Synapse | Azure AD SSO with Row-Level Security (RLS) |

The only credentials that exist in Key Vault are for **external** systems (EIA, FRED). All Azure-internal auth is passwordless.

---

## Monitoring and Observability

### Log Analytics Workspace

Single workspace collects diagnostic logs from:
- ADF pipeline runs (success, failure, duration, activity-level detail)
- Databricks cluster logs, job runs, notebook errors
- ADLS access logs (who accessed what, when)
- Key Vault access logs (audit trail for secret retrieval)
- Synapse query history

### Alert Rules

| Alert | Condition | Action |
|---|---|---|
| Pipeline failure | ADF pipeline status = Failed | Email + Teams webhook to data platform team |
| Cluster idle waste | Databricks cluster running > 4 hours with < 10% utilisation | Alert + auto-scale-down |
| Secret access anomaly | Unusual access pattern on Key Vault | Alert to security team |
| Storage cost spike | ADLS cost > 120% of 30-day rolling average | Alert to FinOps |
| Data freshness | Gold table `prophet_load_factor` not refreshed in > 26 hours | Alert to on-call |

### Dashboards

Two Power BI dashboards read from Log Analytics:
- **Platform health** — pipeline success rates, cluster utilisation, storage growth
- **Business dashboard** — Monte Carlo scenario summaries, forecast accuracy trending, VaR by route

---

## Deployment Sequence (Reference)

For future deployment sessions, the deploy order is:

1. Resource group, VNet, subnets, NAT gateway
2. Log Analytics workspace, Application Insights
3. Key Vault + Private Endpoint + Private DNS Zone
4. ADLS Gen2 + Private Endpoints (blob + dfs) + Private DNS Zones
5. Managed Identities (system-assigned on each service as it deploys)
6. RBAC assignments (Managed Identity → Key Vault, ADLS)
7. Databricks workspace (customer-managed VNet)
8. Databricks secret scope (Key Vault-backed)
9. Synapse workspace + Private Endpoint
10. ADF instance + Managed VNet + Private Endpoint linked services
11. ADF pipeline JSON deployment
12. Databricks notebook deployment (via Repos or DBFS)
13. Synapse external table definitions
14. Alert rules + dashboards
15. End-to-end smoke test


## Known Deployment Considerations

This design is documented to enterprise standards but has not been deployed. Deployment in a real Azure tenant would surface constraints that design work cannot anticipate. These are documented not as blockers, but as the class of questions that separate design from deployment.

**Environmental constraints**
- The VNet address range (`10.10.0.0/16`) is illustrative. Actual allocation would require coordination with the tenant's existing IP address plan and could not conflict with other spoke VNets or on-premises networks.
- Region selection (`uaenorth`) assumes tenant approval under data sovereignty policy. Some organisations restrict allowed regions for cost, compliance, or DR strategy reasons.
- Naming and tagging conventions (`rg-aviation-mc-prod`) are project-standard. Enterprise tenants typically enforce more prescriptive standards via Azure Policy — actual names would need to conform.

**Identity and governance**
- Managed Identity RBAC assignments (Storage Blob Data Contributor, Key Vault Secrets User) may require change advisory board approval in regulated tenants.
- Service principal creation for CI/CD is often gated by the tenant's identity team, not self-service.
- Cross-subscription Managed Identity access, if required by the target topology, needs explicit configuration and is a known source of complexity.

**Networking**
- The design assumes the platform lives in an isolated VNet. Integration with a corporate hub-and-spoke topology would require VNet peering, hub route configuration, and possibly firewall exception processes for outbound API traffic.
- Private DNS zones may conflict with existing corporate DNS forwarders in hybrid networks — a common source of "why can't Databricks reach Key Vault?" incidents.
- Egress to EIA and FRED APIs assumes outbound HTTPS is permitted; corporate firewalls typically require URL whitelisting.

**Data governance**
- Data classification for each source (public / internal / restricted) would need to be formally assigned before deployment. This design assumes all sources are public — verifiable for EIA/FRED, less so for any future internal data.
- Registration in the tenant's data catalogue (Purview or equivalent) is typically a mandatory step before Gold data can be surfaced to consumers.
- The 7-year Bronze retention is a common regulatory baseline but should be validated against jurisdiction-specific requirements (UAE Data Protection Law, GDPR, HIPAA if applicable).

**Cost governance**
- The $755/month estimate assumes low utilisation, no reserved capacity, and no cross-region data transfer costs.
- Actual costs depend on workload patterns, Azure Enterprise Agreement discounts, and reserved instance commitments — likely 30-50% lower in a mature enterprise, or 2-3x higher under bursty workloads.
- Cost centre attribution and budget approval processes vary widely; the platform would need a designated FinOps owner.

**Deployment mechanics**
- The deployment sequence assumes a single-tenant, single-environment deployment. Multi-environment (dev/UAT/prod) deployments add promotion gates, environment-specific secrets, and rollback procedures not detailed here.
- CI/CD pipeline design (Azure DevOps, GitHub Actions, or equivalent) is out of scope for this document.
- IaC templates (Bicep or Terraform) are planned but not authored in this repository.

**Operations**
- The alerting design assumes a named data platform team exists to receive escalations. In smaller organisations, this responsibility would need to be explicitly assigned.
- Runbooks for the top failure modes are not authored here; production readiness would require them.
- Disaster recovery testing cadence is not specified; production readiness would require a defined schedule and named RTO/RPO targets.

These considerations reflect the reality that enterprise deployment is not a lift-and-shift of a design document. Each item above represents a class of question a senior engineer would raise before or during deployment, and the design would iterate based on the tenant's constraints and policies.



---


## Diagram Notes

The ASCII diagrams above are current and version-controlled. A polished visual version (Excalidraw or draw.io) is planned, see [`../../TODO.md`](../../TODO.md).

 