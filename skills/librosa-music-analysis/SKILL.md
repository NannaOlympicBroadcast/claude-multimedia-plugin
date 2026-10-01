---
name: librosa-music-analysis
description: 用 librosa 分析音乐/音频：BPM 与节拍网格、调性(大小调+置信度)、响度/动态范围/峰均比、音色(频谱质心/带宽/滚降/平坦度)、谐波与打击乐能量比、起音密度、段落结构(主歌/副歌式分段+能量高低)、逐秒能量曲线，可输出波形/频谱/色度图。当用户要求分析歌曲、测 BPM、识别调性、找副歌/高潮、给视频配乐选段、卡点剪辑时使用。
---

# librosa 音乐分析

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/music_analyze.py`（下文 `$MA`），依赖 librosa、soundfile、numpy；`--plot` 另需 matplotlib。视频文件会先用 ffmpeg 提取音轨。

```bash
python3 $MA song.mp3                                 # 终端输出 Markdown 报告 + JSON
python3 $MA song.flac --sections 8 -o out/song       # 写 out/song.json + out/song.md
python3 $MA song.wav --plot -o out/song              # 额外输出 out/song.png（波形+节拍/频谱/色度+分段线）
python3 $MA mv.mp4 --start 1:00 --duration 60        # 只分析一段
```

## 结果解读要点

- `tempo_bpm` 为全曲估计；`tempo_range_bpm` 给出动态速度 10–90% 区间，区间宽说明有变速或节奏复杂。半速/倍速误判常见（如 70 vs 140），结合听感或 `beat_times` 说明。
- `key` 基于 Krumhansl-Schmuckler 模板与 chroma 相关，`confidence` < 0.6 时调性不明确（无调性、转调或打击乐为主），需在汇报中注明。
- `sections` 为无监督分段（agglomerative），`energy` = high 的段落通常对应副歌/高潮，可作为配乐选段或卡点剪辑参考；它不是“主歌/副歌”的真值标签。
- `beat_times_first_16` + `beats` 可用于卡点：把节拍时间交给 ffmpeg-video-editing 的 `cut`/`wall`。
- 若需要更深的语义描述（曲风、乐器、情绪、歌词），配合 gemini-media-analysis `--preset audio`。

如实报告数值；分析失败（文件损坏、无音轨）时直接告诉用户原因。
