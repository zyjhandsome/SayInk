# 系统代理下的内网直连：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/ai-system-proxy-private-bypass`
- 风险/闸门：High。规格已批准。未获得实现闸门前不得改产品代码。
- 禁止范围：不改 `dist/`、更新器请求、模型下载、识别结果和纪要正文规则；不新增代理模式下拉项。
- 必须执行的最终验证：`python -m pytest tests/test_text_polisher.py tests/test_minutes.py tests/test_settings_general.py tests/test_ux_redesign.py -q --tb=short`

## 任务

- [x] 任务 1：系统代理下私网 AI 请求直连、公网请求仍走代理
  - 对应需求/场景：私网字面地址不走环境代理；公网主机仍走环境代理；DNS 名不因私网解析而直连；公司后缀只匹配主机和子域；直连模式忽略后缀；更新和模型下载不读取该字段
  - 前置依赖：无
  - 目标文件/符号：`sayink/text_polisher.py` 的 `parse_proxy_bypass_suffixes`、`http_client_options`、`PolishWorker`、`TextPolisher.test_connection`；`sayink/app.py`；`sayink/ui/history_window.py`；`tests/test_text_polisher.py`
  - 允许修改：上述文件中的代理选项、后缀解析和对应测试
  - 禁止修改：设置页控件、纪要生成正文、`sayink/updater.py`、`sayink/speech_recognizer.py`
  - 实施步骤：先写失败测试，覆盖 `10.1.2.3` 不走代理、`api.example.com` 走代理、普通 DNS 名不直连、`.example.internal` 匹配子域、`notexample.com` 不匹配、`bad` 被忽略、直连模式保持 `trust_env=False` 且使用 `SSL_CERT_FILE`。再实现解析和按 URL 选择客户端选项。润色和历史润色把 `llm.proxy_bypass_suffixes` 传进去。最后断言 `updater.py` 与 `speech_recognizer.py` 源码不含 `proxy_bypass_suffixes`。
  - 失败测试或已批准替代验证：先新增断言再改实现；现有 `test_connection_can_bypass_a_gateway_proxy` 必须继续通过
  - 验证命令/动作：`python -m pytest tests/test_text_polisher.py -q --tb=short`
  - 预期结果：该文件全部通过，且新断言在实现前会失败
  - 质量层/证据：functional、compatibility
  - 迁移/回滚：无数据迁移；撤回这些文件即可
  - 完成定义：私网和有效后缀在系统模式下 `trust_env` 为 False，公网为 True，直连模式与 CA 行为不变
  - 负责人/冲突说明：单人顺序实施；本任务独占 `text_polisher.py`

- [x] 任务 2：会议纪要使用同一条直连规则
  - 对应需求/场景：私网字面地址不走环境代理；公网主机仍走环境代理；公司后缀只匹配主机和子域
  - 前置依赖：任务 1 的 `http_client_options` 已接受 URL 和后缀
  - 目标文件/符号：`sayink/minutes_generator.py` 的 `MinutesRequest`、`MinutesWorker._run_pipeline`；`sayink/meeting_session.py`；`tests/test_minutes.py`
  - 允许修改：纪要请求上的后缀传递和对应测试
  - 禁止修改：纪要提示词、检查点指纹、任务 1 已完成的解析规则
  - 实施步骤：先让纪要测试在 `proxy_mode=system`、后缀或私网地址下断言 `AsyncClient` 的 `trust_env`。再给 `MinutesRequest` 增加默认空字符串字段，由 `MeetingController._start` 传入配置。公网且无后缀时保持 `trust_env=True`。
  - 失败测试或已批准替代验证：扩展 `tests/test_minutes.py` 里已有的 `trust_env` 测试，先看它失败
  - 验证命令/动作：`python -m pytest tests/test_minutes.py -q --tb=short`
  - 预期结果：该文件全部通过
  - 质量层/证据：functional
  - 迁移/回滚：旧请求对象不传该字段时等价于空后缀
  - 完成定义：纪要客户端与润色使用同一判断
  - 负责人/冲突说明：不改 `text_polisher.py`，可在任务 1 完成后与任务 3 并行；本会话仍按 1 → 2 → 3 顺序做

- [x] 任务 3：设置里保存额外直连域名并说明无效项
  - 对应需求/场景：保存的后缀在重载后仍在；无效词留在输入框里并出现在 AI 状态中；旧配置没有后缀字段；无障碍名和 Tab 顺序
  - 前置依赖：任务 1
  - 目标文件/符号：`sayink/config.py` `DEFAULT_CONFIG`；`sayink/ui/settings_pages/general.py` `build_preferences_section`、`_general_tab_chain`；`sayink/ui/settings_window.py` `_load_settings_fields`、`_set_llm_status`、连接测试 worker；`tests/test_settings_general.py`；`tests/test_ux_redesign.py`
  - 允许修改：上述设置与配置默认值、Tab 顺序预期
  - 禁止修改：主题、历史保留和其他设置行的文案
  - 实施步骤：先写设置测试：输入 `.example.internal, bad` 会保存，重载后文本仍在，状态点名 `bad`，无障碍名是「额外直连域名」，Tab 顺序位于代理下拉框之后、主题之前。旧配置缺键时输入为空。再加默认值和输入框。改字段时清空 `_llm_test_stamp`。连接测试把当前后缀传给 `test_connection`。
  - 失败测试或已批准替代验证：新增设置测试和 Tab 顺序断言，先看失败
  - 验证命令/动作：`python -m pytest tests/test_settings_general.py tests/test_ux_redesign.py -q --tb=short`
  - 预期结果：两个文件全部通过
  - 质量层/证据：functional、accessibility、compatibility
  - 迁移/回滚：默认空字符串；撤回后旧版本忽略该键
  - 完成定义：后缀可保存、可重载，无效词可见且不参与匹配
  - 负责人/冲突说明：不改任务 2 的纪要文件

## 集成顺序

任务 1 → 任务 2 → 任务 3。任务 2 与任务 3 无共享文件，但本会话按顺序做，避免同时改测试夹具。

## 最终验证

| 命令/动作 | 覆盖范围 | 预期结果 |
|---|---|---|
| `python -m pytest tests/test_text_polisher.py tests/test_minutes.py tests/test_settings_general.py tests/test_ux_redesign.py -q --tb=short` | 四条需求的场景 | 全部通过 |
| `openspec validate ai-system-proxy-private-bypass --strict` | change 结构 | 验证通过 |

## 实现就绪审查

## 结论

- 就绪
- 风险等级：high
- 所需批准：实现闸门一次放行。规格闸门已由用户于 2026-10-28T17:40:00+08:00 批准。

## 阻塞项

- 无

## 警告项

- 无

## 建议项

- 无

## 覆盖情况

| 需求/场景 | 技术计划 | 任务 | 验证 | 状态 |
|---|---|---|---|---|
| 私网直连 | 方案 A | 任务 1、任务 2 | pytest 代理替身 | 已覆盖 |
| 公网仍走代理 | 方案 A | 任务 1、任务 2 | pytest 代理替身 | 已覆盖 |
| DNS 名不提前直连 | 不做 DNS | 任务 1 | 普通域名断言 | 已覆盖 |
| 后缀与相似域名 | 解析规则 | 任务 1、任务 3 | 正反例 | 已覆盖 |
| 直连模式忽略后缀 | `mode == direct` | 任务 1 | CA 与 `trust_env` | 已覆盖 |
| 旧配置 | 默认空字符串 | 任务 3 | 重载测试 | 已覆盖 |
| 更新与模型下载 | 不调用新符号 | 任务 1 | 源码断言 | 已覆盖 |
| 无障碍名与 Tab | 输入框插入 Tab 链 | 任务 3 | 设置与 Tab 测试 | 已覆盖 |

## 技术决策就绪度

- 对齐完整性扫描：已检查调用方、空后缀、非法后缀、旧配置、更新路径、回滚和安全路径。证据已确认集成点。性能和视觉基线不适用。无剩余阻塞。
- Agent 自主决定及依据：D1 按请求设置客户端；D2 不改纪要指纹；D3 状态句追加无效后缀。
- 用户决定及记录：规格范围于 2026-10-28 批准。
- 回 Frame 的事项及状态：无。
- 剩余待决阻塞项：无

## 代码事实新鲜度

- 分支/提交：`main` `ba342b2d12f92de19a7c2066324cd45677b6350e`
- 变基/重构检查：规划期间产品代码未改
- 缺失/改名路径：无

## 并行安全

- 独立任务组：任务 2 与任务 3 在任务 1 之后无共享文件
- 共享文件/状态：任务 1 独占 `text_polisher.py`
- 所有权：本会话顺序实施
- 集成顺序：1 → 2 → 3
- **其他 active changes 路径重叠（G8）：** 无。`openspec list` 只有本 change。

## 后端结构验证

- 命令/工具：`openspec validate ai-system-proxy-private-bypass --strict`
- 结果：`openspec validate ai-system-proxy-private-bypass --strict` 通过；`validate_delivery_change.mjs` 通过

## 闸门记录

- 决定：规格已批准；实现已批准
- 批准人/日期：规格 = 用户（本会话）/ 2026-10-28T17:40:00+08:00；实现 = 用户（本会话）/ 2026-10-28T18:10:00+08:00；绑定工件修订 sha256:08a0ddecf194d656e5f67bf81875a104418bfa5ff13d486939d3b5c22b5ddb04
- 附加约束：无
- 可选 `approval_mode`：`live`
