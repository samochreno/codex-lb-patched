## ADDED Requirements

### Requirement: Native Codex search is proxied through pooled accounts

The service MUST accept authenticated `POST /backend-api/codex/alpha/search`
requests and forward them to the upstream Codex `alpha/search` endpoint using
the existing Codex control-plane account selection, sticky affinity, credential
refresh, routing, and failover behavior. It MUST preserve the request body,
query parameters, relevant request headers, upstream status, response body, and
relevant response headers. It MUST NOT synthesize results or route the request
to an unrelated search provider.

#### Scenario: Codex client performs a web search

- **WHEN** an authenticated client posts a valid Codex search request
- **THEN** the selected ChatGPT account performs the upstream request
- **AND** the upstream JSON response and status are returned faithfully
