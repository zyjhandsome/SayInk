## ADDED Requirements

### Requirement: Visible main-window outer border

The frameless main window SHALL draw a theme-aware 1px outer border so users can see the software edge against the desktop.

#### Scenario: 基本行为

- **WHEN** the main window applies its theme chrome
- **THEN** the outermost window draws a 1px solid frame using the active `TEXT_DIM` token so the edge reads like a desktop window outline
- **AND** the root layout keeps a 1px inset so child chrome cannot cover that frame
- **AND** child widgets do not inherit an extra outer-border rule from that window stylesheet
