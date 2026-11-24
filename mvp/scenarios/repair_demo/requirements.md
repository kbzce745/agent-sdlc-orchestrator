# 需求文档：爱云动赛事影像上传与号码布识别巡检工具（v1.1 迭代）

- **所属内部系统：** 影像上传与预处理中台 / 隐私合规与肖像下架审核后台
- **目标代码文件：** `src/race_media_inspector.py`
- **背景：** 周末大型路跑/马拉松赛事期间，现场摄影师通过上传端并发回传数万张照片，云端同步执行号码布（OCR）识别。运营与研发需在赛后快速排查点位上传失败、低置信度待人工复核照片，并严格遵守选手肖像下架申诉（`opt_out`）与个人敏感信息脱敏红线。

## 一、功能要求

实现并完善以下 4 个公开函数（保持函数名与返回结构一致）：

1. `mask_sensitive_text(text: str) -> str`
   - 将文本中的中国大陆 11 位手机号（`1[3-9]xxxxxxxxx`）中间 4 位脱敏为 `****`（如 `13812345678` → `138****5678`）。
   - 将 URL 查询参数中的 `token=`、`sig=`、`signature=`、`access_key=` 参数值脱敏为 `***`（不区分大小写）。
2. `inspect_jsonl_lines(lines: list[str]) -> dict`
   - 逐行解析 JSONL 日志；空行直接忽略。
   - **脏数据容错：** 遇到非法 JSON 或缺少必要字段（`photo_id`, `photographer_id`, `checkpoint`, `upload_status`, `upload_ms`）的行不得崩溃，累加 `malformed_lines` 后跳过。
   - **肖像下架合规过滤（强制）：** 若记录中 `opt_out` 为 `true`，不得计入有效统计，也不得进入 `low_confidence_queue` 明细，仅累加 `opt_out_filtered`。
   - **聚合统计：**
     - `total_valid_records`, `upload_success_count`, `upload_failed_count`, `overall_success_rate`（保留 4 位小数）
     - `photographers[photographer_id]`：`total`, `success`, `failed`, `success_rate`（4 位小数）, `avg_upload_ms`（2 位小数）
     - `checkpoints[checkpoint]`：`total`, `success`, `failed`, `success_rate`（4 位小数）
   - **低置信度人工复核队列（`low_confidence_queue`）：**
     - 收集 `upload_status == "success"` 且 `ocr_confidence < 0.80` 的记录。
     - 每条包含：`photo_id`, `photographer_id`, `checkpoint`, `bib_number`, `ocr_confidence`（4 位小数）, `contact`（对 `contact_phone` 脱敏）, `preview_url`（对 `preview_url` 脱敏）。
3. `inspect_log_file(path: str | Path) -> dict`
   - 读取本地 JSONL 文件并调用 `inspect_jsonl_lines`。
4. `render_markdown_report(result: dict, event_name: str = "赛事影像巡检") -> str`
   - 输出 Markdown 运营巡检报告，包含核心汇总、摄影师统计表、低置信度待人工复核清单（确保报告中无明文手机号或 URL Token 泄露）。

## 二、安全与合规红线

- 严禁使用 `eval`、`exec`、`os.system`、`subprocess(..., shell=True)`。
- 严禁硬编码 API Key 或生产数据库连接串。
- 严禁修改只读验收测试目录 `tests/`。
