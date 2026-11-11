## Context

系统代理模式把整个 httpx 客户端设为 `trust_env=True`，直连模式设为 `trust_env=False` 并保留企业 CA。一次 AI 调用只访问一个 API 地址，客户端也是按次创建的。因此可以按这一次的主机决定该客户端是否读取环境代理，而不必改进程环境。

## Goals / Non-Goals

**Goals:**

- 系统代理模式下，现有本地端点直连；用户后缀只匹配对应主机及其子域。
- 其余公网主机继续走环境代理，包括已有 `NO_PROXY`。
- 直连模式、更新检查和模型下载保持原路径。

**Non-Goals:**

- 不改 Windows 系统代理、PAC、TUN。
- 不解析 DNS，不内置公司域名，不增加第三种代理模式。

## Decisions

- 按单次请求选择 `trust_env`，不修改进程的 `NO_PROXY`。改环境变量会影响到使用 `urllib` 的更新下载（`sayink/updater.py`），违反「更新不看这组设置」。
- 直连选项继续复用现在的 CA 逻辑：`SSL_CERT_FILE` 优先于 `SSL_CERT_DIR`。
- 主机分类直接调用现有 `is_local_endpoint`。不另写私网表，也不先做 DNS。
- 后缀存在 `llm.proxy_bypass_suffixes`，默认空字符串。非法词保留在输入框里，由 `SettingsWindow._set_llm_status` 在 AI 状态句后追加说明；会议纪要那条状态标签不追加。
- 纪要请求指纹不加入后缀。后缀只改变网络路径，不改变生成内容。

未选方案：把后缀写进进程 `NO_PROXY`。它会让更新和模型下载跟着绕过代理，而且并发请求会互相覆盖。

## Risks / Trade-offs

- [把公网主机误判为直连] → 只认字面本地端点和带点的后缀；`notexample.com` 不得匹配 `example.com`。测试锁住这两类。
- [Clash 假 IP] → 不解析 DNS，避免 `198.18.0.0/15` 把所有域名判成私网。
- [只配了 TUN/PAC、没有环境变量] → 本次例外看不见那层代理。这是已接受的非目标。

## Migration Plan

- 在 `DEFAULT_CONFIG["llm"]` 增加 `proxy_bypass_suffixes: ""`。`_merge_defaults` 会给旧配置补上这个空值；非字符串会被现有 `_coerce_known_value` 恢复成空字符串。
- 回滚：撤回这次代码。配置里多出来的空字段或用户填过的后缀会被当作未知键留下，旧代码不读取它。不需要数据迁移。
- 部署：随应用发布，无新依赖。

## Open Questions

无。规格闸门已于 2026-10-28 由用户批准。实现闸门仍待批准。

---

# 系统代理下的内网直连：技术实施计划

## 已批准目标与约束

- 目标：系统代理模式下，本地和私网 AI 地址自动直连，用户后缀只让匹配主机直连；公网仍走环境代理。
- 非目标：访问许可开关、内置域名表、DNS 判断、PAC/TUN、更新和模型下载。
- 风险/闸门：High。规格已批准。实现前还要一次放行。

## 已刷新代码事实

| 结论 | 证据 | 新鲜度 |
|---|---|---|
| `http_client_options` 只看模式 | `sayink/text_polisher.py` | 本会话重读，HEAD `ba342b2` |
| 纪要用同一个函数 | `sayink/minutes_generator.py` `MinutesWorker._run_pipeline` | 本会话重读 |
| 设置行在 `build_preferences_section` | `sayink/ui/settings_pages/general.py` | 本会话重读 |
| 缺键合并默认值，字符串类型不符会恢复默认 | `Config._merge_defaults`、`_coerce_known_value` | 本会话重读 |
| 更新走 `urllib.request` | `sayink/updater.py` | 本会话检索 |
| 无其他活跃 OpenSpec change | `openspec list` 仅本 change | 本会话 |

## 技术决策清单

| ID | 待决事项 | 决策归属（Agent / 用户 / 回 Frame） | 实质影响 | 选项与建议 | 状态 | 最终结论与记录 |
|---|---|---|---|---|---|---|
| D1 | 按请求关闭 `trust_env`，还是改进程 `NO_PROXY` | Agent | 改环境变量会波及更新下载 | 按请求。建议不改环境变量 | decided | 按请求设置客户端选项 |
| D2 | 后缀是否进入纪要指纹 | Agent | 只影响缓存失效，不改变正文 | 不加入 | decided | 网络路径不改变生成结果 |
| D3 | 非法后缀提示放在哪 | Agent | 规格要求用现有 AI 状态，且不要对话框 | 只追加到 `_llm_test_status` | decided | `_set_llm_status` 识别这一个标签 |

没有需要用户另决的技术项，也没有要退回规格的行为变化。

## 方案比较

### 方案 A

- 方案形态：`http_client_options(mode, url, suffixes)` 在该 URL 应直连时返回与直连模式相同的选项。
- 收益：公网和内网可以同时工作；更新代码不读这个字段。
- 成本/风险：每个调用点要传 URL 和后缀。
- 可逆性：撤回提交即可。
- 验证方式：用现有 httpx 替身断言 `trust_env` 和 CA。

### 方案 B

- 方案形态：系统模式下把后缀和私网写入进程 `NO_PROXY`。
- 收益：调用点改动少。
- 成本/风险：并发请求互相覆盖；`sayink/updater.py` 的 `urlopen` 也会绕过代理。
- 可逆性：同样可撤回，但运行时副作用更大。
- 验证方式：必须再测更新请求没有被污染。

## 最终决策

- 选定方案：方案 A。
- 选择理由：一次 AI 调用已经独占一个客户端和一个 URL，按请求决定不会漏到更新下载。
- 未选方案及原因：方案 B 违反「更新和模型下载不看这组设置」。
- 决策来源 / 批准记录：规格行为已批准；D1–D3 由实现计划决定。

## 集成方式与数据流/控制流

设置保存 `llm.proxy_mode` 与 `llm.proxy_bypass_suffixes`。连接测试、润色、历史润色、会议纪要在创建 httpx 客户端前调用 `http_client_options`。模式为 `direct` 时始终不信任环境代理。模式不是 `direct` 时，主机被 `is_local_endpoint` 认定或匹配有效后缀，则同样不信任环境代理；否则信任环境代理。

模型下载仍在 `sayink/speech_recognizer.py` 使用它自己的地址常量，更新仍在 `sayink/updater.py` 使用 `urlopen`。两边都不调用新函数。

## 接口与状态模型

- `parse_proxy_bypass_suffixes(text) -> tuple[tuple[str, ...], tuple[str, ...]]`：有效后缀、被忽略的原文词。
- `http_client_options(mode, url="", suffixes="") -> dict`：保持原有 CA 行为。
- `MinutesRequest.proxy_bypass_suffixes: str = ""`。
- 配置键 `llm.proxy_bypass_suffixes`，默认 `""`。
- 界面：`QLineEdit`，标签「额外直连域名」，无障碍名相同，放在代理下拉框下方，并插入 `_general_tab_chain` 中 `_llm_proxy_combo` 与 `_theme_combo` 之间。

## 失败处理与可观测性

- 空地址、无点后缀、含 `*` `/` `:` `@` 的词不参与匹配。
- 不新增记录 API Key 或全文地址的日志。
- 非法词出现时，AI 状态句末追加「这些直连后缀不会生效：…」。密钥错误和缺项提示保留，后面仍可追加这一句。

## 兼容、迁移与回滚

- 旧配置缺字段时等于空后缀。本地和私网仍按系统模式直连。
- 字段不是字符串时恢复为 `""`。
- 回滚只撤回代码。已写入的后缀键留在配置中，旧版本忽略。

## 安全与性能

- 直连会让匹配主机的转写文本和 API Key 不经过公司代理。这是用户选择系统代理并填写后缀后的预期路径。
- 不解析 DNS，避免把假 IP 当成内网。
- 不把后缀或模式写进纪要检查点，避免无关缓存失效。
- 每次请求只做字符串判断。

## 验证策略

### 质量画像

| profile | requirement/acceptance | task | validation |
|---|---|---|---|
| functional | 私网直连、公网走代理、后缀和直连模式 | 任务 1–2 | `python -m pytest tests/test_text_polisher.py tests/test_minutes.py -q --tb=short` |
| visual | 不要求视觉基线 | 不适用 | 无 |
| accessibility | 无障碍名与 Tab 顺序 | 任务 3 | `python -m pytest tests/test_settings_general.py tests/test_ux_redesign.py -q --tb=short` |
| performance | 不要求 | 不适用 | 无 |
| compatibility | 旧配置与更新路径 | 任务 1、任务 3 | 同上，外加源码不引用新符号的断言 |

### Visual validation matrix（visual=required 时）

不适用。`visual` 为 `not_required`。

## 需求追溯

| 需求/场景 | 设计要素 | 任务 | 验证 |
|---|---|---|---|
| 私网字面地址不走环境代理 | `http_client_options` + `is_local_endpoint` | 任务 1 | 代理替身看到非 HTTPProxy |
| 公网主机仍走环境代理 | `trust_env=True` | 任务 1 | 替身看到 HTTPProxy |
| DNS 名不因可能的私网解析而直连 | 不做 DNS | 任务 1 | 普通域名在有代理时走代理 |
| 公司后缀只匹配主机和子域 | `parse_proxy_bypass_suffixes` | 任务 1、任务 3 | `llm.example.internal` 直连，`notexample.com` 不直连 |
| 直连模式忽略后缀 | `mode == "direct"` | 任务 1 | `trust_env is False` 且 CA 仍在 |
| 旧配置没有后缀字段 | 默认 `""` | 任务 3 | 重载后输入为空，私网仍直连 |
| 更新和模型下载不读取该字段 | 不调用新函数 | 任务 1 | `updater.py` 与 `speech_recognizer.py` 源码不含 `proxy_bypass_suffixes` |

## 已知风险与非目标

TUN 和 PAC 不在本次行为内。Python 3.10 的 `ipaddress` 私网判断包含字面 `198.18.0.0/15`；只有用户把这种地址直接写进 API URL 时才会直连，不通过 DNS 扩大。

## High 内部核对

- 设计：本文。
- 任务：`tasks.md` 三条纵向切片。
- 回滚：撤回代码；残留配置键被旧版本忽略。
- 安全：不改进程代理环境，不解析 DNS，不记录密钥，直连仍使用企业 CA。
- 验证：任务中的 pytest 命令能在行为缺失时失败。
