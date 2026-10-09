## ADDED Requirements

### Requirement: Factory settings chrome rethemes with effective theme
Settings factory widgets that paint via construction-time stylesheets (including info callouts, usage tip bars, and footnotes created on settings pages) SHALL refresh their colors from the active token axis when the effective theme changes while the settings window remains open.

#### Scenario: Callout follows dark apply
- **WHEN** the user switches appearance to dark while Settings is open and an info callout is visible
- **THEN** the callout background and text colors match the active dark tokens (not the light-axis values captured at construction)

#### Scenario: Tip bar follows light apply
- **WHEN** the effective theme returns to light while Settings is open
- **THEN** usage tip bars and footnotes repaint using the active light tokens

### Requirement: Float chip interaction colors are tokenized
Floating window close-chip hover and pressed backgrounds SHALL use dedicated design tokens (or derived token fields published on the active axis). Implementations MUST NOT synthesize hover/press colors by string-replacing alpha fragments inside `CHIP_BG`.

#### Scenario: Theme apply refreshes chip states without string hacks
- **WHEN** `FloatingWindow.reapply_theme` runs after a light or dark activate
- **THEN** the close button stylesheet references tokenized chip/hover/press colors from `design_tokens`

### Requirement: MASTER color notes match intentional token values
Where runtime tokens intentionally diverge from an older MASTER sample (for example dark-axis success green quieting), MASTER MUST be updated to the runtime values or explicitly document the intentional divergence so the two sources are not contradictory.

#### Scenario: Dark green axis is document-aligned
- **WHEN** a reviewer compares MASTER dark success/toggle green with `design_tokens` dark `GREEN` / `TOGGLE_ON`
- **THEN** the documented value matches the runtime token or states the intentional quieter green with the same hex as code
