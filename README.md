# Runbook: Agent Identity Foundations (Lab 2 / APEX009)

This runbook documents the end-to-end steps completed in the Cloud CLI to migrate two enterprise ADK agents (**GitHub Agent** on Vertex AI Agent Runtime and **Salesforce Agent** on Cloud Run) from hardcoded `.env` credentials to **Google Cloud Agent Identity Auth Manager** (`agentidentity.googleapis.com`).

---

## Architecture Summary

| Component | Runtime Target | Auth Provider Type | ADK Integration |
| :--- | :--- | :--- | :--- |
| **`github-agent`** | Vertex AI Agent Runtime (Reasoning Engine) | API Key (`github-mcp-auth-provider`) | `GcpAuthProvider` + `GcpAuthProviderScheme` + `McpToolset` |
| **`salesforce-agent`** | Cloud Run | 2-Legged OAuth / Client Credentials (`salesforce-2lo-auth-provider`) | `GcpAuthProvider` + `GcpAuthProviderScheme` + `AuthenticatedFunctionTool` |

---

## 1. Environment & Prerequisites Setup

### 1.1 Authenticate & Set Project Variables

```bash
gcloud auth login
gcloud auth application-default login

export PROJECT_ID="<YOUR_PROJECT_ID>"
export LOCATION="us-central1"
export REGION="us-central1"

gcloud config set project "${PROJECT_ID}"
export PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
export ORGANIZATION_ID=$(gcloud projects get-ancestors "${PROJECT_ID}" --format=json | jq -r '.[] | select(.type=="organization") | .id')

echo "Project: ${PROJECT_ID} (${PROJECT_NUMBER}), Org: ${ORGANIZATION_ID}, Location: ${LOCATION}, Agents: ${REGION}"
```

### 1.2 Run Lab Prerequisites Script

Download and run the lab prerequisites script to stage `~/agents/github-agent` and `~/agents/salesforce-agent` and install `agents-cli`:

```bash
gcloud storage cp -r "gs://${PROJECT_ID}-bucket/apex_labs" .
export PATH="$HOME/.local/bin:$PATH"
bash apex_labs/lab2_prereqs.sh
```

### 1.3 Enable Required Google Cloud APIs

```bash
gcloud services enable \
  iam.googleapis.com \
  agentidentity.googleapis.com \
  aiplatform.googleapis.com \
  agentidentitycredentials.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

---

## 2. GitHub Agent (API Key Auth Provider + Vertex AI Agent Runtime)

### 2.1 Create the GitHub API Key Auth Provider

Store the GitHub Personal Access Token (PAT) in Agent Identity Auth Manager instead of `.env`:

```bash
# Enter the GitHub PAT securely (input is hidden)
read -rs GITHUB_PAT && echo

gcloud alpha agent-identity auth-providers create github-mcp-auth-provider \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --api-key="${GITHUB_PAT}"

# Verify creation
gcloud alpha agent-identity auth-providers list \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}"

export GITHUB_AUTH_PROVIDER_URI="projects/${PROJECT_ID}/locations/${LOCATION}/authProviders/github-mcp-auth-provider"
echo "${GITHUB_AUTH_PROVIDER_URI}"
```

### 2.2 Configure `github-agent/.env`

Remove `GITHUB_PERSONAL_ACCESS_TOKEN` and point to `GITHUB_AUTH_PROVIDER_URI`:

```bash
cd ~/agents/github-agent

cat << EOF > .env
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=global
GOOGLE_GENAI_USE_VERTEXAI=True
MODEL=gemini-3.5-flash
GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/
GITHUB_AUTH_PROVIDER_URI=projects/${PROJECT_ID}/locations/${LOCATION}/authProviders/github-mcp-auth-provider
EOF
```

### 2.3 Refactor `github-agent/agent/agent.py`

Register `GcpAuthProvider` with `CredentialManager`, configure `GcpAuthProviderScheme`, and pass it into `McpToolset`:

```python
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity import GcpAuthProvider, GcpAuthProviderScheme
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
GITHUB_AUTH_PROVIDER_URI = os.environ["GITHUB_AUTH_PROVIDER_URI"]
GITHUB_TOOL_FILTER = ["search_repositories", "search_issues", "list_issues"]

# 1. Register Google Cloud auth provider
CredentialManager.register_auth_provider(GcpAuthProvider())

# 2. Define the auth scheme referencing the Auth Provider resource URI
github_auth_scheme = GcpAuthProviderScheme(name=GITHUB_AUTH_PROVIDER_URI)

def github_mcp_toolset() -> McpToolset:
    toolset = None

    def github_header_provider(readonly_ctx) -> dict[str, str]:
        if not toolset or not toolset.get_auth_config():
            return {}
        auth_config = toolset.get_auth_config()
        cred = None
        if readonly_ctx and hasattr(readonly_ctx, "get_credential"):
            cred = readonly_ctx.get_credential(auth_config.credential_key)
        if not cred:
            cred = auth_config.exchanged_auth_credential
        if not cred or not cred.http:
            return {}
        token = None
        if cred.http.credentials and cred.http.credentials.token:
            token = cred.http.credentials.token
        elif cred.http.additional_headers:
            token = cred.http.additional_headers.get("X-API-Key") or cred.http.additional_headers.get("X-GOOG-API-KEY")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    # 3. Build McpToolset with auth_scheme and header_provider
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=GITHUB_MCP_URL),
        auth_scheme=github_auth_scheme,
        tool_filter=GITHUB_TOOL_FILTER,
        header_provider=github_header_provider,
    )
    return toolset
```

### 2.4 Deploy `github-agent` with Agent Identity & Bind IAM Policy

```bash
cd ~/agents/github-agent
chmod +x ./deploy_with_identity.sh

REGION="${REGION}" \
A2A_APP_NAME=agent \
AUTH_PROVIDER_NAME=github-mcp-auth-provider \
DISPLAY_NAME=github-agent-v2 \
./deploy_with_identity.sh
```

Grant the Reasoning Engine's effective identity access to the auth provider:

```bash
# Use the effective identity output from deploy_with_identity.sh
export GITHUB_AGENT_IDENTITY="principal://<EFFECTIVE_IDENTITY_FROM_DEPLOY>"
export GITHUB_ENGINE_ID="<REASONING_ENGINE_ID_FROM_DEPLOY>"
export GITHUB_AGENT_URL="https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${GITHUB_ENGINE_ID}"

gcloud alpha agent-identity auth-providers add-iam-policy-binding github-mcp-auth-provider \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --role="roles/agentidentity.user" \
  --member="${GITHUB_AGENT_IDENTITY}"
```

---

## 3. Salesforce Agent (2LO Auth Provider + Cloud Run)

### 3.1 Create the Salesforce 2-Legged OAuth (2LO) Auth Provider

```bash
export SF_DOMAIN="<YOUR_SALESFORCE_MY_DOMAIN>"   # e.g., yourorg.my.salesforce.com
export SF_CLIENT_ID="<YOUR_CONSUMER_KEY>"
read -rs SF_CLIENT_SECRET && echo                # Paste Consumer Secret securely

gcloud alpha agent-identity auth-providers create salesforce-2lo-auth-provider \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --two-legged-oauth-client-id="${SF_CLIENT_ID}" \
  --two-legged-oauth-client-secret="${SF_CLIENT_SECRET}" \
  --two-legged-oauth-token-url="https://${SF_DOMAIN}/services/oauth2/token"

# Verify creation
gcloud alpha agent-identity auth-providers list \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}"

export SALESFORCE_AUTH_PROVIDER_URI="projects/${PROJECT_ID}/locations/${LOCATION}/authProviders/salesforce-2lo-auth-provider"
echo "${SALESFORCE_AUTH_PROVIDER_URI}"
```

### 3.2 Add `agent-identity` Extra to `pyproject.toml`

```bash
cd ~/agents/salesforce-agent
sed -i 's/google-adk\[gcp\]/google-adk[gcp,agent-identity]/' pyproject.toml
uv lock
```

### 3.3 Configure `salesforce-agent/.env`

Remove `SALESFORCE_CLIENT_ID` and `SALESFORCE_CLIENT_SECRET` from `.env` and add `SALESFORCE_AUTH_PROVIDER_URI`:

```bash
cat << EOF > ~/agents/salesforce-agent/.env
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=global
GOOGLE_GENAI_USE_VERTEXAI=True
MODEL=gemini-3.5-flash
SALESFORCE_DOMAIN=${SF_DOMAIN}
SALESFORCE_AUTH_PROVIDER_URI=${SALESFORCE_AUTH_PROVIDER_URI}
EOF
```

### 3.4 Refactor `salesforce-agent` Code

1. **`app/salesforce_client.py`**: Remove client credentials and `authenticate()` from `SalesforceClient`. Initialize with only `domain` and `api_version`, and pass `access_token` per method call (`search_files(self, access_token: str, query: str)`).
2. **`app/tools.py`**: Register `GcpAuthProvider`, create `AuthConfig` with `GcpAuthProviderScheme(name=SALESFORCE_AUTH_PROVIDER_URI)`, inject `credential: AuthCredential` into `search_salesforce`, extract the bearer token via `_extract_token(credential)`, and wrap the function in `AuthenticatedFunctionTool`:

```python
SALESFORCE_AUTH_PROVIDER_URI = os.environ["SALESFORCE_AUTH_PROVIDER_URI"]

CredentialManager.register_auth_provider(GcpAuthProvider())
salesforce_auth_config = AuthConfig(
    auth_scheme=GcpAuthProviderScheme(name=SALESFORCE_AUTH_PROVIDER_URI)
)

def build_search_salesforce(client: SalesforceClient):
    def search_salesforce(query: str, credential: AuthCredential) -> dict:
        access_token = _extract_token(credential)
        if not access_token:
            return {
                "status": "error",
                "error_message": "Salesforce auth failed: no token from Auth Manager.",
            }
        try:
            documents = client.search_files(access_token, query)
        except Exception as e:
            return {
                "status": "error",
                "error_message": f"Salesforce search failed: {e}",
            }
        return {
            "status": "success",
            "query": query,
            "data": documents,
        }

    return AuthenticatedFunctionTool(
        func=search_salesforce, auth_config=salesforce_auth_config
    )
```

3. **`app/agent.py`**: Instantiate `SalesforceClient(domain=SALESFORCE_DOMAIN, api_version=os.environ.get("SALESFORCE_API_VERSION"))` without client credentials.

### 3.5 Deploy `salesforce-agent` to Cloud Run

```bash
cd ~/agents/salesforce-agent

agents-cli deploy --project "${PROJECT_ID}" --region "${REGION}" \
  --no-confirm-project --no-wait \
  --update-env-vars "$(grep -v '^#' .env | grep -v '^$' | grep -v '^GOOGLE_CLOUD_PROJECT' | paste -sd,)"

# Check deployment status until complete
agents-cli deploy --status
```

### 3.6 Grant Auth Provider IAM Bindings & Test via A2A

Grant `roles/agentidentity.user` on `salesforce-2lo-auth-provider` to the Cloud Run service account (and user account if testing locally):

```bash
export SF_SA=$(gcloud run services describe salesforce-agent --region "${REGION}" \
  --format='value(spec.template.spec.serviceAccountName)')
echo "${SF_SA}"

gcloud alpha agent-identity auth-providers add-iam-policy-binding salesforce-2lo-auth-provider \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --role="roles/agentidentity.user" \
  --member="serviceAccount:${SF_SA}"

gcloud alpha agent-identity auth-providers add-iam-policy-binding salesforce-2lo-auth-provider \
  --project="${PROJECT_ID}" \
  --location="${LOCATION}" \
  --role="roles/agentidentity.user" \
  --member="user:<YOUR_EMAIL>"
```

Disable IAP on the Cloud Run service and invoke the agent over A2A:

```bash
gcloud run services update salesforce-agent --region="${REGION}" --no-iap

export SF_URL=$(gcloud run services describe salesforce-agent --region "${REGION}" --format='value(status.url)')

agents-cli run --url "${SF_URL}" --mode a2a "Search for any architecture decision records about timeouts."
```
