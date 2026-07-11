# Key Vault Secrets Pattern

**Aviation Route Monte Carlo — Secrets Management Reference**

This document specifies the secrets management pattern for the pipeline. It is referenced from [`architecture.md`](architecture.md) ADR-004 and [`deployment_diagram.md`](deployment_diagram.md) authentication model.

---

## Principles

The pattern is grounded in five principles, in order of priority:

1. **No credentials in code.** Not in `.py`, not in `.json`, not in `.yaml`, not in notebooks.
2. **No credentials in configuration.** No connection strings with embedded passwords, no `.env` files in production deployments.
3. **Managed Identity for all Azure-internal auth.** No service-to-service passwords.
4. **Key Vault as single source of truth** for the small number of secrets that must exist (external API keys).
5. **Least privilege.** Each identity has the minimum RBAC role required, nothing more.

Local development uses `.env` files for convenience. This is acceptable because `.env` is gitignored and never leaves the developer's machine. Production explicitly does not use `.env`.

---

## Secret Inventory

| Secret name | Type | Owner | Rotation | Consumers |
|---|---|---|---|---|
| `eia-api-key` | External API key | Platform team | Annual | ADF, Databricks |
| `fred-api-key` | External API key | Platform team | Annual | ADF, Databricks |
| `databricks-workspace-token` | Azure token | Platform team | 90 days | ADF |
| `powerbi-service-principal-secret` | Azure secret | Platform team | 90 days | Power BI Gateway |

**Not stored in Key Vault (by design):**
- ADLS storage account keys — replaced by Managed Identity access
- Synapse SQL admin password — replaced by Azure AD auth
- Databricks personal access tokens — replaced by workspace-level Managed Identity
- Any password for a service that supports Managed Identity — replaced by that Managed Identity

The absence of these from Key Vault is the point. **Secrets you don't need to store are secrets that can't leak.**

---

## Access Pattern

### From ADF pipelines

ADF's system-assigned Managed Identity is granted the `Key Vault Secrets User` role on `kv-aviation-mc-prod`. Pipelines reference secrets via linked service definitions that use Key Vault, not inline values.

Example linked service (conceptual):

```json
{
    "type": "HttpServer",
    "typeProperties": {
        "url": "https://api.eia.gov/v2/",
        "authenticationType": "Anonymous",
        "encryptedCredential": null
    },
    "parameters": {
        "apiKey": {
            "type": "SecureString",
            "referenceName": "eia-api-key",
            "referenceType": "AzureKeyVaultSecret"
        }
    }
}
```

At runtime, ADF authenticates to Key Vault using its Managed Identity, retrieves the secret, and injects it into the HTTP call. **The secret never appears in ADF logs, pipeline JSON, or activity output.**

### From Databricks notebooks

Databricks uses an Azure Key Vault-backed secret scope named `aviation-mc-secrets`. The scope is a mapping — Databricks users see it as a namespace, but the actual retrieval hits Key Vault via the workspace Managed Identity.

Notebook code accesses secrets like this:

```python
eia_api_key = dbutils.secrets.get(scope="aviation-mc-secrets", key="eia-api-key")
```

Critical properties:
- `dbutils.secrets.get()` returns a redacted display value if you try to print it (Databricks intercepts the output)
- Secrets do not appear in notebook cell outputs, logs, or command history
- Notebook users can call the function but cannot enumerate what secrets exist unless granted permission

### From Synapse

Synapse Serverless authenticates to ADLS Gold using its workspace Managed Identity — no secrets involved at all. This is why the Synapse entry in the secret inventory is minimal: **the design specifically avoids Synapse needing secrets.**

---

## Rotation Strategy

Automated rotation is preferred where the underlying service supports it. For manual rotation:

**External API keys (EIA, FRED):**
- No automated rotation (providers don't support programmatic key rotation)
- Manually rotated annually on a calendar reminder
- Rotation procedure:
  1. Generate new key at provider portal
  2. Add new key to Key Vault as new version
  3. Existing consumers automatically pick up latest version at next pipeline run
  4. After 48 hours (confirmation of adoption), delete old key at provider portal

**Azure-issued secrets (Databricks tokens, service principal secrets):**
- Rotated every 90 days
- Rotation triggered by Azure Automation runbook
- Follows the same "add new, verify adoption, revoke old" pattern

**Compromise response:**
- If a secret is believed compromised, immediate rotation with revocation of old value
- Key Vault access logs are reviewed to identify unusual access patterns
- Incident is documented per the tenant's security incident response procedure

---

## Access Auditing

Every access to every secret is logged. Log Analytics workspace collects:
- Secret name accessed
- Identity that accessed it
- Timestamp
- IP address / VNet source
- Success or failure

Retention: 1 year for security audit purposes. Alerts are configured for:
- Access from unexpected identities
- High-frequency access outside normal pipeline windows
- Failed access attempts (potential probing)

---

## Local Development

Developers working on the pipeline locally use `.env` files, which are:

- Gitignored (see [`../../.gitignore`](../../.gitignore))
- Never committed
- Individual to each developer
- Contain personal or shared-dev API keys, not production keys

The pattern used in `data/ingestion/eia_client.py` and `fred_client.py` reads from `.env` in local development mode. When deployed, the same code path would read from Key Vault via Managed Identity — a small adaptation to swap the credential source.

This is deliberate: **the same code runs locally and in production, differing only in how it obtains the secret**. This is a strong pattern because it means local development actually exercises the production code path, not a parallel implementation.

---

## What This Pattern Prevents

Documenting explicitly what this pattern prevents makes it easier to explain and defend:

- **Credentials in git history.** Because they're never in code, they can't leak via commit.
- **Credentials in ADF pipeline definitions.** Pipeline JSON contains references, not values.
- **Credentials in Databricks notebooks.** Scope-based retrieval prevents inline embedding.
- **Credentials in shared logs.** All log sinks redact secrets automatically.
- **Credentials in error messages.** Try/except handlers never surface secret values to users.
- **Cross-service credential sharing.** Managed Identity eliminates most inter-service secrets.

---

## Known Limitations

- Rotation is partly manual (external providers don't offer better)
- The `.env` local-development pattern requires developer discipline to not commit
- Managed Identity is Azure-specific; multi-cloud deployment would need equivalent patterns per cloud
- Key Vault costs scale with secret operations; heavy use could add ~$5-15/month