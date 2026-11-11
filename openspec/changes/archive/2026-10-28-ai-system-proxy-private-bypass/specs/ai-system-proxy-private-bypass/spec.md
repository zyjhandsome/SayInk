## ADDED Requirements

### Requirement: System proxy bypasses local and private AI hosts

While AI proxy mode is system, a connection test, polish or correction request, history polish, or meeting-minutes request whose API host is a local endpoint MUST NOT use `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`. The host classification MUST be the same one already used for local endpoints: `localhost`, `127.0.0.1`, `::1`, a host ending in `.local`, or a literal IP that is loopback, private, or link-local. The system MUST NOT resolve DNS before this decision. A bypassed request MUST still use `SSL_CERT_FILE` or `SSL_CERT_DIR` when one of those variables is set. A non-matching host in system mode MUST keep using the environment proxy, including an existing `NO_PROXY` list.

#### Scenario: Private literal address skips the environment proxy

- **WHEN** AI proxy mode is system, `HTTPS_PROXY` points at a gateway, and the API address host is `10.1.2.3`
- **THEN** the connection test, polish or correction, history polish, and meeting-minutes requests do not go through that gateway
- **AND** a configured `SSL_CERT_FILE` is still used to verify TLS

#### Scenario: Public host still uses the environment proxy

- **WHEN** AI proxy mode is system, `HTTPS_PROXY` points at a gateway, and the API address host is `api.example.com`
- **THEN** those same AI requests go through that gateway
- **AND** a host listed in `NO_PROXY` still bypasses the gateway

#### Scenario: DNS name that would resolve privately still uses the proxy

- **WHEN** AI proxy mode is system and the API host is a DNS name that is not a saved suffix and does not end in `.local`
- **THEN** the request uses the environment proxy
- **AND** the system does not connect directly because a lookup might return a private address

### Requirement: User domain suffixes bypass the proxy only in system mode

The settings page MUST show a text field labeled 额外直连域名 beside AI 服务代理, with accessible name 额外直连域名. Its hint MUST say that the field applies only while the system proxy mode is selected, and MUST show that entries are domain suffixes separated by commas or spaces. The field MUST be saved with the other general settings and MUST survive a reload. An empty or missing value MUST add no suffixes. Entries MUST be split on commas, semicolons, whitespace, and Chinese commas. A leading dot MUST be optional. Matching MUST be case-insensitive. After removing one leading dot, an entry without a remaining dot MUST be ignored. A valid suffix MUST match that host and any host under it, and MUST NOT match a different host that merely ends with the same characters. Ignored entries MUST remain visible in the field and MUST be named in the existing AI configuration status without a modal dialog. Changing the field MUST clear a previous connection-test result. Direct proxy mode MUST ignore the field and MUST continue to bypass environment proxies for every host.

#### Scenario: Saved company suffix is direct only for matching hosts

- **WHEN** AI proxy mode is system, the saved suffixes are `.example.internal, bad`, and the API host is `llm.example.internal`
- **THEN** the AI request does not use the environment proxy
- **AND** a request to `api.example.com` still uses the environment proxy
- **AND** the status names `bad` as ignored while the field still shows the typed text

#### Scenario: Suffix does not match a lookalike host

- **WHEN** the only valid saved suffix is `example.com` and the API host is `notexample.com`
- **THEN** system proxy mode sends that request through the environment proxy

#### Scenario: Direct mode ignores suffixes

- **WHEN** AI proxy mode is direct and a suffix is saved
- **THEN** every AI request bypasses environment proxies
- **AND** a configured `SSL_CERT_FILE` is still used

#### Scenario: Old configuration has no suffix field

- **WHEN** a configuration saved before this field exists is loaded
- **THEN** AI proxy mode behaves as if the suffix field were empty
- **AND** local and private hosts still bypass the environment proxy in system mode

### Requirement: Proxy exceptions do not affect updates or model downloads

Application update checks, installer downloads, and speech-model downloads MUST NOT consult AI proxy mode or the suffix field.

#### Scenario: Update path is unchanged

- **WHEN** the user checks for an application update or downloads a model while a private AI host and a suffix are configured
- **THEN** those update and download requests do not use the AI proxy mode or the suffix field
