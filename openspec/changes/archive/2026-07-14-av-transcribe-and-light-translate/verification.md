# 验证记录：av-transcribe-and-light-translate

日期：2026-07-13  
执行环境：Python 3.10.11 / pytest 9.0.3（Windows）

## 自动验证

| 命令 | 结果 |
|---|---|
| `pytest tests/test_media_decoder.py tests/test_build_ffmpeg.py tests/test_app_file_transcription.py -q` | **20 passed** |
| `pytest tests/test_readme_features.py tests/test_settings_general.py tests/test_history_window.py tests/test_text_polisher.py tests/test_app.py tests/test_app_file_transcription.py tests/test_media_decoder.py tests/test_build_ffmpeg.py tests/test_config.py tests/test_floating_window.py -q` | **224 passed** |
| 全量 `pytest tests/` | 多次在环境侧出现 `KeyboardInterrupt`（`pyaudiowpatch` / threading），中断前至少 **273 passed**；与本变更无关的既有音频枚举路径易触发。核心相关批已全绿。 |

## 手册项（发布前）

- [ ] 将 `ffmpeg.exe` 放入 `third_party/ffmpeg/` 后执行 `python build.py`，确认 `dist/SayInk/_internal/ffmpeg/` 存在
- [ ] 托盘「导入文件转写…」导入短 mp3/mp4，历史出现 `来源：文件转写`
- [ ] 设置后处理模式=翻译，文件任务出译文；热键听写不翻译
- [ ] Esc 可取消进行中的文件转写；文件任务期间热键听写被拒绝并提示

## 已知限制

- 整段转写，超长文件可能占内存（设计已接受）
- 无 `third_party/ffmpeg` 时构建仅 WARN，不阻断打包（发布构建须自备二进制）
