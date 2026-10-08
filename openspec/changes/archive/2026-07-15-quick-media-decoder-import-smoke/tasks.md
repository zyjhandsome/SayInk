# media_decoder 导入烟雾：实施任务清单

## 执行规则

- 权威状态源：`openspec/changes/quick-media-decoder-import-smoke/`
- 风险/闸门：Quick / low；契约 go 已记录
- 禁止范围：修改 `media_decoder` / `app.py` 行为；delta spec；design.md
- 必须执行的最终验证：`python -m pytest tests/test_media_decoder_import_smoke.py -q`

## 任务

- [x] 1.1 新增导入烟雾单测
  - 目标文件/符号：`tests/test_media_decoder_import_smoke.py`；`sayink.media_decoder.resolve_ffmpeg_executable` / `decode_media_to_pcm` / 错误类型
  - 禁止修改：`sayink/media_decoder.py`、`sayink/app.py`、其它产品模块
  - 验证命令/动作：`python -m pytest tests/test_media_decoder_import_smoke.py -q`
  - 预期结果：至少 1 条用例通过；退出码 0
