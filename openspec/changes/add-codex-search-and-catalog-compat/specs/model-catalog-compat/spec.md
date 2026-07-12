## MODIFIED Requirements

### Requirement: Codex-native catalog entries are client-decodable

Every entry emitted in the `models` array of `GET /backend-api/codex/models`
MUST contain a valid `truncation_policy`. The service MUST preserve an upstream
policy when present and MUST use Codex's fallback byte policy with limit 10000
when upstream omits the field. Every entry MUST also contain
`experimental_supported_tools`; the service MUST preserve an upstream list and
MUST use an empty list when upstream omits it.

#### Scenario: Upstream model omits truncation policy

- **WHEN** an otherwise eligible upstream model omits `truncation_policy`
- **THEN** its Codex-native catalog entry contains `{"mode":"bytes","limit":10000}`

#### Scenario: Upstream model supplies truncation policy

- **WHEN** an upstream model supplies a valid `truncation_policy`
- **THEN** the Codex-native catalog preserves that policy unchanged
