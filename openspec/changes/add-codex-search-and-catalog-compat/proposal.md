# Add Codex Search and Catalog Compatibility

## Summary

Make CodexLB compatible with Codex clients that require every native model
catalog entry to contain `truncation_policy`, and proxy the native authenticated
`POST /backend-api/codex/alpha/search` endpoint through the pooled account
control-plane path.

## Scope

- Default missing native catalog `truncation_policy` and
  `experimental_supported_tools` fields to Codex's own fallback values while
  preserving upstream values when present.
- Forward `alpha/search` request bytes, query parameters, relevant headers,
  upstream status, response bytes, and response headers through existing sticky
  account selection, authentication refresh, routing, and failover behavior.

## Out of Scope

- Changing OpenAI-compatible model catalog metadata.
- Implementing a separate public search provider or synthesizing search output.
- Changing Responses, image, or WebSocket routes.
