## ADDED Requirements

### Requirement: Unified UI font family resolution
The system SHALL resolve a single UI font family for the effective desktop session using this priority: prefer `Segoe UI Variable` when available on the host, otherwise `Microsoft YaHei UI` (then any further documented stack fallbacks). That resolved family MUST be applied to the global application stylesheet, settings/history/tray QSS `font-family` usages, and floating-window `QFont` construction. Hard-coded font family strings outside the shared resolver MUST NOT remain on the floating window or other themed surfaces in scope.

#### Scenario: Floating window matches global UI font
- **WHEN** the application applies the current theme and shows the floating window
- **THEN** the floating status/body labels use the same resolved UI font family as `build_global_stylesheet`

#### Scenario: Font probe failure falls back safely
- **WHEN** `Segoe UI Variable` is unavailable on the host
- **THEN** the resolver selects `Microsoft YaHei UI` (or the next documented fallback) without crashing startup, and all themed surfaces use that same resolved family

### Requirement: Typography size scale tokens
The system SHALL define named typography size tokens (for example caption / body / title levels) in `design_tokens` and use those tokens for user-visible `font-size` values on settings, history, floating, tray menu, and model card chrome in scope. Magic-number `font-size` literals for those surfaces MUST be replaced by the tokens (tests may still assert numeric token values).

#### Scenario: Token-driven sizes rebuild with styles
- **WHEN** settings styles are rebuilt via `reload_styles` / theme apply
- **THEN** page titles, footnotes, nav labels, and model card text sizes are expressed from the typography tokens rather than ad-hoc literals in those builders

### Requirement: MASTER typography axis alignment
`design-system/MASTER.md` MUST document the same UI font resolution strategy (Segoe UI Variable preferred, Microsoft YaHei UI fallback) and the same typography size ladder as the `TYPE_*` (or equivalent) tokens in `design_tokens`. Contradictory font priority or size tables between MASTER and runtime tokens are not allowed.

#### Scenario: No contradictory font strategy docs
- **WHEN** a reviewer compares MASTER typography guidance with `design_tokens` / resolver behavior
- **THEN** MASTER states Segoe-first-with-YaHei-fallback and lists the same size ladder values used by the code tokens

#### Scenario: Type scale table is acceptance-visible
- **WHEN** verification checks MASTER against `design_tokens` typography sizes
- **THEN** every in-scope `TYPE_*` size used by settings/history/float/tray/model-card chrome appears in the MASTER typography size table
