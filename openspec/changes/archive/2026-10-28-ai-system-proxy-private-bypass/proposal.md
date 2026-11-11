## Why

公司代理能访问公网模型，但会把内网 AI 接口拖超时。v2.2.30 只有「整路使用环境代理」和「整路直连」。同一台电脑需要公网走代理、内网直连时，这两档不够。

## What Changes

- 选择「使用系统 / 环境代理」时，发往本地和私网地址的 AI 请求自动不走环境代理。公网地址仍走环境代理，并继续遵守已有的 `NO_PROXY`。
- 在同一设置旁增加「额外直连域名」。用户填写的域名后缀只在系统代理模式下不走环境代理。
- 「直连（不使用代理）」保持整路不走环境代理。应用更新和模型下载仍不受这组设置影响。
- 不增加「允许访问本地网络」开关，也不内置大厂域名清单。

## Capabilities

### New Capabilities

- `ai-system-proxy-private-bypass`: 系统代理模式下，本地/私网 AI 地址和用户填写的域名后缀直连；直连模式与非匹配公网地址保持原行为。

### Modified Capabilities

- 无。现有 `openspec/specs/` 没有覆盖 AI 代理路由。

## Impact

- 请求：`sayink/text_polisher.py` 的 `http_client_options`、连接测试、润色/纠错；`sayink/minutes_generator.py` 的纪要请求。调用点包括 `sayink/app.py`、`sayink/meeting_session.py`、`sayink/ui/history_window.py`、`sayink/ui/settings_window.py`。
- 配置：`sayink/config.py` 的 `llm` 增加可选后缀字段。缺字段的旧配置按空后缀读取。
- 界面：`sayink/ui/settings_pages/general.py` 的「应用与历史」代理行，以及通用页 Tab 顺序。
- 不改 Windows 系统代理、PAC、TUN，也不改更新器和模型下载。

---

# 系统代理下的内网直连：需求与代码事实简报

## 意图

### 目标与成功标准

- 目标：使用系统 / 环境代理时，本地和私网 AI 地址自动直连，用户还能为自己的公司域名追加直连后缀；公网地址继续走环境代理。
- 可观察的成功结果：
  - 系统代理模式下，`localhost`、`.local`、字面私网/回环/链路本地地址的连接测试、润色/纠错、会议纪要不使用 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。
  - 同一模式下，不匹配的公网地址仍使用这些环境代理。
  - 用户保存的域名后缀只让匹配主机在系统代理模式下直连。
  - 直连模式对所有地址都不使用环境代理。
  - 旧配置没有后缀字段时，行为与后缀为空相同。

### 边界与非目标

- 本次范围：AI 连接测试、文字润色/纠错、历史里的润色、会议纪要。设置位于「设置 → 通用 → 应用与历史」，紧挨现有「AI 服务代理」。
- 非目标：
  - 「允许访问本地网络」权限开关。
  - 内置大厂或 `.corp` / `.internal` / 无点主机名的自动清单。公司域名由用户填写。
  - 先做 DNS 解析再判断是否私网。
  - 读取或绕过 Windows 系统代理、PAC、TUN。
  - 改变应用更新和模型下载的网络路径。
  - 新增第三种代理模式下拉项。
- 禁止修改路径：`dist/`、发布说明以外的打包产物；不改识别模型、热词和会议正文生成规则。

## 代码事实

### 现状摘要

`llm.proxy_mode` 只有 `system` 和 `direct`，默认 `system`。`http_client_options` 在 `direct` 时设置 `trust_env=False`，并保留 `SSL_CERT_FILE` / `SSL_CERT_DIR`；其他值设置 `trust_env=True`。因此系统模式把代理和环境里的 `NO_PROXY` 交给 httpx，直连模式忽略环境代理。该选项进入连接测试、润色、历史润色和会议纪要。设置页文案写明更新不受此设置影响。

`is_local_endpoint` 把 `localhost`、`127.0.0.1`、`::1`、以 `.local` 结尾的主机，以及字面量上的回环、`ipaddress` 私网和链路本地地址视为本地。它决定密钥是否可空、公网明文提示和会议远程确认，不参与选代理。它不解析 DNS。

`Config.get` 在缺键时返回调用方给出的默认值。`_load` 用 `_merge_defaults` 把 `DEFAULT_CONFIG` 补进已有配置。新增带默认值的 `llm` 字段不会要求用户迁移旧文件。

### 可复用 / 需扩展 / 冲突

#### 可直接复用

- 现有两种代理模式和直连时保留企业 CA 的做法。
- `is_local_endpoint` 的主机分类，避免另做一套「本地」定义。
- 代理模式变更时清掉连接测试戳记的做法（`SettingsWindow._on_llm_proxy_changed`）。

#### 需要扩展

- `http_client_options` 目前只有模式、没有目标地址，系统模式无法按主机直连。
- 设置页只有下拉框，没有后缀输入。
- 调用点需要把当前 API 地址和后缀传进客户端选项。

#### 需求与现状冲突

- 无。公网地址在系统模式下的现有行为保持不变。直连模式保持不变。

### 挂载点候选

| 优先级 | 路径/符号 | 理由 |
|---|---|---|
| 必选 | `sayink/text_polisher.py` `http_client_options` | 连接测试和润色的唯一客户端选项 |
| 必选 | `sayink/minutes_generator.py` `MinutesWorker._run_pipeline` | 纪要使用同一选项 |
| 必选 | `sayink/ui/settings_pages/general.py` `build_preferences_section` | 现有「AI 服务代理」行 |
| 必选 | `sayink/config.py` `DEFAULT_CONFIG["llm"]` | 后缀的持久默认值 |
| 备选 | 无 | 不另建代理子系统 |

### 波及线索

- 调用方：`sayink/app.py` 润色、`sayink/ui/history_window.py` 历史润色、`sayink/meeting_session.py` 纪要、`sayink/ui/settings_window.py` 连接测试。
- 持久化：新的 `llm` 字符串字段，缺省为空。
- 测试：`tests/test_text_polisher.py` 的代理绕过与 CA；`tests/test_minutes.py` 的 `trust_env`；`tests/test_settings_general.py` 的保存与重载；`tests/test_ux_redesign.py` 的通用页 Tab 顺序（代理控件在 `_theme_combo` 之前）。
- 打包依赖：无。

### 质量画像与验收

| profile | required / not_required | 证据与验收 |
|---|---|---|
| functional | required | 系统模式按主机选择是否使用环境代理；直连模式不变；后缀持久化。用现有 httpx 替身测试断言 `trust_env` 和代理传输。 |
| visual | not_required | 只在现有设置卡片增加一行输入。验收看控件文案、无障碍名和保存结果，不新做视觉基线。 |
| accessibility | required | 后缀输入有无障碍名「额外直连域名」，并进入通用页 Tab 链，位于代理下拉框之后、主题之前。 |
| performance | not_required | 每次请求只做主机字符串判断。 |
| compatibility | required | 无后缀字段的旧配置等于空后缀；不匹配的公网主机仍 `trust_env=True`；直连仍 `trust_env=False` 且保留 CA 环境变量。 |

### 证据表

| 类型 | 结论 | 证据 |
|---|---|---|
| 事实 | 系统模式 `trust_env=True`，直连模式 `trust_env=False` 并保留 CA 环境变量 | `sayink/text_polisher.py` `http_client_options` |
| 事实 | 该选项用于连接测试、润色、历史润色、会议纪要 | `text_polisher.py`、`app.py`、`history_window.py`、`meeting_session.py`、`minutes_generator.py`、`settings_window.py` |
| 事实 | 设置只有「使用系统 / 环境代理」和「直连（不使用代理）」 | `sayink/ui/settings_pages/general.py` `build_preferences_section` |
| 事实 | 本地端点含 localhost、`.local` 和字面回环/私网/链路本地，且不解析 DNS | `sayink/text_polisher.py` `is_local_endpoint` |
| 事实 | 缺键返回默认值；加载时合并 `DEFAULT_CONFIG` | `sayink/config.py` `Config.get`、`Config._load`、`_merge_defaults` |
| 推断 | httpx 的 `trust_env=True` 会遵守环境变量 `NO_PROXY`；应用不读取 Windows 系统代理、PAC 或 TUN | 依据现有客户端只传 `trust_env`，没有 WinHTTP/PAC 调用。待实现测试锁住环境代理行为即可，不把 TUN 纳入本次验收 |
| 决策 | 自动直连使用现有本地端点分类，公司域名只来自用户后缀 | 「开放问题清单」方向行 |

## 消歧与闸门

### 开放问题清单

| 优先级 | 问题 | 代码事实背景 | 选项与影响（摘要） | 建议 | 状态 | 最终决策 |
|---|---|---|---|---|---|---|
| 必选 | 系统代理下如何让内网直连、公网仍走代理 | 现在只能整路二选一 | 自动本地/私网 + 用户后缀 / 只加 NO_PROXY 预设 / 访问许可开关 | 自动本地/私网 + 用户后缀 | decided | 用户于 2026-10-28 确认该方向 |
| 可选 | 是否把无点主机名和 `.internal` 等收进自动规则 | 用户确认的句子是「本地和私网」加自定义后缀 | 自动扩大会直连用户没点名的域名 | 不自动扩大 | decided | 只复用现有本地端点分类；其余由后缀框承担 |

无剩余 `open` 项。

### 澄清完整性扫描

- 已检查的适用维度：使用者与入口、正常/空后缀/非法后缀、失败路径、配置保存与旧配置兼容、现有调用方、安全路径、可证伪验收。
- 由证据解决的缺失事实：代理选项的调用面、本地端点分类、配置缺键行为、设置挂载点。
- 新增开放问题及处理状态：无新的阻塞产品分叉。后缀写法作为非阻塞默认写入规格：逗号、分号、空白或中文逗号分隔；可带一个前导点；忽略大小写；去掉前导点后必须仍含一个点，否则该词不参与匹配；匹配主机本身或其子域。
- 明确不适用 / 不在范围的维度：权限角色、并发、删除迁移、更新器与模型下载、PAC/TUN、视觉基线。
- 结论：无实质阻塞项。

### 风险定级与闸门建议

- 建议车道/风险：High / `high`。
- 命中的风险特征：改动跨设置、配置、润色和纪要；新增持久化字段；改变携带转写文本和 API Key 的请求走哪条网络。按「多个升级信号」定为高风险。
- 未命中的高风险特征：不改登录、角色、支付、个人信息的存储/导出/日志脱敏/同意撤回，不改外部 HTTP 契约，不删除数据，不做历史回填。旧配置只补空后缀。
- 不确定点：用户机器若只配置了 TUN 或 PAC，而没有 `HTTP_PROXY` 一类环境变量，本次直连例外看不见那层代理。这已写入非目标。
- 闸门建议：先批准本规格，再进入设计与任务分解。实现闸门留在下一阶段。
- 可用验证：现有代理、纪要、设置和 Tab 顺序测试可扩展。
- 缺失验证：还没有「系统模式下私网直连、公网仍走代理」的测试。

### Explore 交接消费

- [x] `chosen_direction` → 已写入「意图」（或用户修订后的目标）
- [x] `non_goals` → 已写入「意图」边界 / 标明 N/A 及理由
- [x] `code_anchors` → 已驱动至少一处「代码事实」/「挂载点候选」检查（路径或符号已引用）
- [x] `risk_signal` → 仅作线索；「风险定级与闸门建议」已按代码事实重算（hit / miss / uncertain）
- [x] `unknowns` → 已写入「开放问题清单」或标明非阻塞丢弃

落点摘要：意图=系统代理下本地/私网自动直连并允许用户后缀；挂载=`http_client_options` 与通用页代理行；Risk=high；开放问题=均已决。

### 状态源与工件位置

- 后端：OpenSpec change
- 路径：`openspec/changes/ai-system-proxy-private-bypass`
- 闸门记录：规格批准状态 = 已批准；批准人 = 用户（本会话）；时间 = 2026-10-28T17:40:00+08:00；附加约束 = 无；接受的警告 = 无。实现闸门 = 待批准。High 内部五面（设计、任务、回滚、安全、验证）在 `design.md` 记录，待实现闸门一并确认。
