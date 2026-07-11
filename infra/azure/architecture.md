# Azure Pipeline Architecture

**Aviation Route Monte Carlo Production Pipeline Design**

This document describes the target Azure architecture for productionising the DXB-BOM route profitability Monte Carlo engine. The Python codebase in this repository runs locally; this design specifies how it would be deployed as a managed enterprise data platform.

> **Scope note:** This is a design artefact. No Azure resources are provisioned by this repository. The architecture is documented in IaC-ready format so a future deployment session can lift it directly into Bicep, Terraform, or ARM templates.

---

## Architecture Overview

### Pattern: Hybrid Lakehouse + Warehouse

The platform combines three Azure services, each chosen for what it does best:

| Layer | Service | Purpose |
|---|---|---|
| **Orchestration** | Azure Data Factory | Schedule, trigger, monitor pipelines |
| **Compute / ML** | Azure Databricks | Spark-based transformation, Prophet training, Monte Carlo execution |
| **Serving** | Synapse Serverless SQL | T-SQL access to curated data for BI, analysts, dashboards |
| **Storage** | ADLS Gen2 | Single source of truth across all zones |
| **Secrets** | Azure Key Vault | API keys, connection strings, service principals |
| **Observability** | Log Analytics + Monitor | Pipeline run metrics, alerts, audit logs |

### Data Flow

### Resource Group Layout

A single resource group `rg-aviation-mc-prod` contains all resources. For multi-environment deployment, this would be templated into `rg-aviation-mc-{dev|uat|prod}`.

---

## Architecture Decision Records

The following ADRs capture the key design decisions. Each follows the standard structure: Context → Decision → Alternatives → Consequences.

---

### ADR-001: Hybrid platform (ADF + Databricks + Synapse)

**Status:** Accepted  
**Date:** 2026-06  

**Context**

The pipeline must serve three distinct user groups: data engineers writing transformation code (PySpark, notebooks), data scientists training and running models (Python, scikit-learn, Prophet, MLflow), and business analysts consuming results (T-SQL, Power BI). Each group has established tooling preferences and skill sets that significantly impact productivity.

The pipeline must also fit within typical enterprise architecture patterns — most large Aviation and Healthcare organisations in the Gulf and Europe already operate hybrid Microsoft stacks with existing Synapse or SQL Server estates and existing BI investments. A pure-play Databricks deployment would require these organisations to abandon working infrastructure.

**Decision**

Use a hybrid architecture with three Azure services, each owning the layer it does best:
- **ADF** for orchestration (scheduling, triggering, monitoring)
- **Databricks** for compute (Spark transformations, ML training, simulation)
- **Synapse Serverless SQL** for serving (T-SQL access to curated data)

ADLS Gen2 acts as the single source of truth that all three services read from and write to.

**Alternatives Considered**

| Option | Why not |
|---|---|
| Pure Databricks (Workflows + SQL Warehouse) | Excellent for ML-first teams but requires analysts to learn Databricks SQL. Misaligned with established enterprise BI patterns. Higher unified cost. |
| Pure Synapse (Dedicated SQL pools + Spark pools) | Synapse Spark lags Databricks for ML workloads (no native MLflow, weaker Delta support, fewer libraries). Forces a single-vendor lock-in to Microsoft. |
| Azure Synapse Link + SQL DB | Suits operational reporting, not analytical Monte Carlo workloads. Wrong tool for the job. |
| AWS equivalent (Glue + EMR + Redshift) | Out of scope — target organisations standardise on Azure. |

**Consequences**

*Positive:*
- Each user group works in familiar tools (no retraining cost)
- Compute and storage scale independently
- Failure isolation: ADF outage doesn't break Databricks notebooks; Synapse outage doesn't break ingestion
- Maps cleanly onto Microsoft enterprise reference architectures

*Negative:*
- Three services to manage, monitor, and bill
- Cross-service authentication requires careful Managed Identity design (see ADR-004)
- Higher cognitive load for the team owning the platform
- Vendor lock-in to Azure (acceptable given the target organisations)

---

### ADR-002: Medallion lake structure (Bronze / Silver / Gold)

**Status:** Accepted  
**Date:** 2026-06  

**Context**

Data lands in the platform in heterogeneous formats from heterogeneous sources: JSON from APIs, CSV files from manual curation, eventually streaming data from IATA feeds. It then needs to be transformed, validated, modelled, and served. Without a layered structure, this becomes a tangle of point-to-point transformations that nobody can reason about after six months.

**Decision**

Adopt the standard medallion architecture as defined by Databricks:

- **Bronze (`/bronze`)** — raw landing zone. Data lands as-is from source. Append-only. No transformations. Preserves complete history of source data exactly as received.
- **Silver (`/silver`)** — cleansed, validated, conformed. Schema-on-write. Deduplicated. Typed correctly. Joined where natural. The "single version of the truth" layer.
- **Gold (`/gold`)** — business-facing aggregates and models. Forecasts, simulation results, risk metrics. Read-optimised for serving via Synapse.

**Alternatives Considered**

| Option | Why not |
|---|---|
| Raw → Curated (two-layer) | Insufficient separation. Mixes cleansing with business logic. |
| Source → Staging → Datawarehouse | Database paradigm, doesn't fit lake patterns. Loses the time-travel and schema evolution benefits of Delta. |
| Direct write to Gold from ingestion | Violates audit and reprocessing — if a bug is found in transformation logic, we have to re-fetch from the source API. Bronze enables replay without re-fetching. |

**Consequences**

*Positive:*
- Clean separation of concerns: ingest, conform, model
- Replayability: rebuild Silver and Gold from Bronze if logic changes
- Schema evolution at each layer without breaking downstream consumers
- Industry standard — onboarding new engineers is faster

*Negative:*
- 3x storage compared to direct ingestion (mitigated by Delta compression and lifecycle policies)
- Adds latency: source → Bronze → Silver → Gold, vs. source → Gold direct
- Requires discipline: temptation to skip Silver and write directly to Gold is real and must be resisted

---

### ADR-003: Delta Lake as storage format

**Status:** Accepted  
**Date:** 2026-06  

**Context**

Files in ADLS need a format that supports transactional writes (so concurrent jobs don't corrupt each other), schema evolution (so adding a column doesn't break old readers), and time travel (so we can replay historical states for backtesting Monte Carlo scenarios).

**Decision**

Use **Delta Lake** as the storage format across all three lake zones (Bronze, Silver, Gold). Underlying files are Parquet; Delta adds a transaction log (`_delta_log/`) that provides ACID guarantees.

**Alternatives Considered**

| Option | Why not |
|---|---|
| Plain Parquet | No transactions. Concurrent writes corrupt data. No schema evolution. No time travel. Acceptable only for write-once Bronze tables. |
| JSON | Human-readable but enormous storage cost, slow reads, no schema enforcement. Acceptable only as a transient landing format. |
| Apache Iceberg | Equivalent technology to Delta. Better cross-cloud portability. Worse Databricks integration in 2025-26. Revisit in 12 months. |
| Apache Hudi | Strong for upsert-heavy workloads (CDC). Our workload is append-mostly. Wrong tool. |

**Consequences**

*Positive:*
- ACID transactions: ingestion jobs can run concurrently without corruption
- Time travel: replay any historical state of the data for backtesting (e.g., "what would the model have forecast on 2024-06-30?")
- Schema evolution: add columns, change types without breaking downstream
- MERGE / UPDATE / DELETE supported (impossible with plain Parquet)
- Native to Databricks; first-class support in Synapse Serverless via OPENROWSET

*Negative:*
- Slightly higher write latency than plain Parquet (transaction log overhead)
- Requires reader libraries to understand the `_delta_log/` format (mostly transparent — Spark, Synapse, Power BI all support it)
- Vendor concentration risk: Delta is open-source but Databricks-led

---

### ADR-004: Managed Identity + Key Vault for all secrets

**Status:** Accepted  
**Date:** 2026-06  

**Context**

The pipeline needs credentials to access: EIA and FRED APIs, ADLS storage accounts, Synapse SQL pools, and any future external systems. Hard-coding credentials anywhere — in notebooks, in ADF pipeline JSON, in config files — is unacceptable. Storage in `.env` files is acceptable for local development but not production.

**Decision**

All secrets live exclusively in **Azure Key Vault**. Resources access Key Vault using **Managed Identities** (the resource's Azure-assigned identity, which doesn't have a credential at all — Azure handles the authentication transparently).

The pattern:
- ADF has a system-assigned Managed Identity → granted `Key Vault Secrets User` role → reads connection strings and API keys at pipeline runtime
- Databricks uses Azure Key Vault-backed secret scopes → notebooks call `dbutils.secrets.get(scope, key)` → never see the raw secret
- Synapse uses a workspace Managed Identity → accesses ADLS directly without storing storage account keys

**Alternatives Considered**

| Option | Why not |
|---|---|
| `.env` files in each service | Files get committed accidentally. No rotation. No audit trail of access. Production-unsafe. |
| Connection strings in ADF linked services | Connection strings often contain account keys. Visible to anyone with ADF access. No rotation. |
| Storage account keys passed between services | Maximum blast radius — a leaked key gives full storage access. Always rotated manually. |
| Databricks secret scopes (workspace-backed, not Key Vault-backed) | Works but creates a separate secrets store. Splits the source of truth. |

**Consequences**

*Positive:*
- Single source of truth for all secrets
- Audit trail: Key Vault logs every access to every secret
- Automatic rotation possible for some secret types
- No credentials in code or config
- No service-to-service password sharing

*Negative:*
- Setup complexity: each Managed Identity needs the right RBAC role on Key Vault
- Initial debugging when permissions are wrong is non-obvious
- Tighter Azure coupling (mitigated by the fact we're already Azure-committed per ADR-001)

---

### ADR-005: ADF for orchestration (vs Databricks Workflows)

**Status:** Accepted  
**Date:** 2026-06  

**Context**

The pipeline has multiple stages (API ingestion, transformation notebooks, model training, simulation execution, Synapse refresh) that need scheduled, monitored, and dependency-managed execution. Both Azure Data Factory and Databricks Workflows can orchestrate this.

**Decision**

Use **ADF** as the orchestrator. Databricks notebooks are called as activities from ADF pipelines.

**Alternatives Considered**

| Option | Why not |
|---|---|
| Databricks Workflows (Jobs API) | Excellent for Databricks-internal orchestration. Weaker for cross-service orchestration (less native ADLS Copy support, no first-class Synapse activity, weaker integration with on-prem sources for future hybrid scenarios). |
| Azure Synapse Pipelines | Functionally similar to ADF but with weaker Databricks integration. ADF is the strategic Microsoft orchestrator. |
| Apache Airflow (Azure Managed Airflow) | More powerful for complex DAGs. Higher setup cost. Code-as-config approach less aligned with target organisations' preferred Microsoft-native tooling. |
| Logic Apps + Functions | Better for event-driven workloads. Our workload is scheduled, not event-driven. |

**Consequences**

*Positive:*
- Native connectors to 100+ data sources for future expansion
- Visual pipeline canvas familiar to data engineers in enterprise Microsoft shops
- Built-in monitoring, alerting, retry logic
- Tight integration with Key Vault for secrets (ADR-004)
- Cross-service orchestration: a single ADF pipeline can copy from S3, run a Databricks notebook, refresh a Synapse table, send a Power BI alert

*Negative:*
- Pipeline definitions are JSON — version control diffs are noisier than code
- Debugging cross-activity failures requires hopping between ADF and Databricks UIs
- Some functionality requires a Self-Hosted Integration Runtime (not needed for this project but worth noting)

---

## Security Model

See `key_vault_secrets.md` for detailed secrets management pattern.

Summary:
- No credentials in code, JSON, or config files
- All inter-service auth via Managed Identity
- Key Vault is the single source of truth for any human-managed secret (API keys)
- RBAC at the resource level: each Managed Identity has minimum required permissions

---

## Cost Model

Estimated monthly cost for production-grade deployment (USD, mid-2026 pricing):

| Service | Approximate cost | Notes |
|---|---|---|
| ADLS Gen2 (10 TB) | $200 | LRS, cool tier for Bronze, hot for Silver/Gold |
| Databricks (Premium, 2 nodes, 4 hours/day) | $400 | Auto-terminate after 30 min idle |
| ADF | $50 | ~30 activities/day, 100 pipeline runs/month |
| Synapse Serverless SQL | $50 | Pay-per-TB-scanned, lightweight serving workload |
| Key Vault | $5 | Negligible |
| Log Analytics + Monitor | $50 | 30 GB ingestion/month |
| **Total** | **~$755/month** | |

For development/UAT, costs drop ~60% (auto-pause Databricks, smaller cluster, less storage).

---

## Deployment

This architecture is **designed but not deployed** in this repository. To deploy:

1. Provision resources via Bicep/Terraform (templates not included)
2. Configure Managed Identities and Key Vault RBAC
3. Set up Databricks workspace + secret scope
4. Deploy ADF pipeline JSON (see `data_factory_pipeline.json`)
5. Deploy Databricks notebooks (see `databricks_notebooks/`)
6. Configure Synapse Serverless external tables over ADLS Gold
7. Validate end-to-end run on synthetic test data
8. Cutover

---

## Related Documents

- [`deployment_diagram.md`](deployment_diagram.md) — service-level architecture diagram
- [`key_vault_secrets.md`](key_vault_secrets.md) — detailed secrets management pattern
- [`data_factory_pipeline.json`](data_factory_pipeline.json) — ADF pipeline definition (planned)
- [`databricks_notebooks/`](databricks_notebooks/) — Databricks notebook implementations (planned)