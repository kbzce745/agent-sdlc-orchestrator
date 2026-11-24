# B11 · 可运行 MVP：确定性 FSM 编排与多级质量门禁研发流水线

- **业务场景：** 爱云动赛事影像上传与号码布识别流水线巡检工具（`race_media_inspector`）
- **核心架构：** 纯 Python 自研有限状态机（FSM）编排引擎 + 角色职责分离（`SpecAnalyzer` / `Coder` / `Reporter`）+ Gate 1 静态/安全/防作弊门禁 + Gate 2 只读隔离 `pytest` + Git 特性分支沙箱与失败回滚
- **支持模型后端：**
  1. `deepseek`（**国内网络首选**，直连 `https://api.deepseek.com`，默认官方模型 `deepseek-flash`，可选 `deepseek-v4-pro`，自动按峰谷时段核算人民币消耗）
  2. `openai`（通用 OpenAI-Compatible 接口，支持通义千问 / 硅基流动 / OpenAI 等自定义 `--base-url`）
  3. `ollama`（本地免费部署，默认 `http://localhost:11434` / `llama3:latest`，零 API 费用）
  4. `mock`（零 Key / 离线确定性演示模式，3 秒完整复现“通过 → 失败 → 自动修复 → 熔断回滚上报”全链路）

---

## 一、环境要求与依赖安装

- Python 3.10+（已实测通过 Python 3.14.0）
- Git（已加入系统 `PATH`，用于沙箱仓库初始化、特性分支与自动回滚）

```bash
# 1. 进入项目根目录（或 mvp 目录）安装极简依赖
pip install -r mvp/requirements.txt
```

---

## 二、一条命令运行方式

### 方式 A：零 Key / 离线一键复现（推荐评审快速验收）

无需联网、无需 API Key，3 秒内完整运行 **场景一（自动修复通过）** 与 **场景二（合规冲突触发熔断回滚上报）**：

```bash
python mvp/run_pipeline.py --scenario all --provider mock
```

### 方式 B：接入 DeepSeek API 实测（国内网络直连首选）

1. 复制模板并填入你的 DeepSeek Key（`.env` 已被 `.gitignore` 与打包脚本自动忽略，不会泄露）：

```bash
cp mvp/.env.example mvp/.env
# 编辑 mvp/.env，填入 DEEPSEEK_API_KEY=sk-...
```

或在终端临时设置环境变量（Windows PowerShell）：

```powershell
$env:DEEPSEEK_API_KEY="sk-your-key-here"
python mvp/run_pipeline.py --scenario all --provider deepseek
```

Linux / macOS：

```bash
export DEEPSEEK_API_KEY="sk-your-key-here"
python mvp/run_pipeline.py --scenario all --provider deepseek
```

> [!NOTE]
> **API 成本参考（DeepSeek 官方 `deepseek-flash` 公开费率，按北京时间峰谷计费）：** 高峰时段（工作日 9:00-12:00 / 14:00-18:00）输入未命中 ¥2.00/百万 Tokens、缓存命中 ¥0.04/百万 Tokens、输出 ¥8.00/百万 Tokens；空闲时段统享 5 折。单次完整双场景运行实测成本通常在 **¥0.03 ~ ¥0.12**。每次运行结束后控制台与 `review_report.md` 均会自动输出精确 Token 与人民币消耗。

### 方式 C：本地 Ollama 免费运行（零 API 费用）

```bash
python mvp/run_pipeline.py --scenario all --provider ollama --model llama3:latest
```

### 方式 D：运行编排器自身单元测试

验证 Gate 1 高危 AST 拦截、硬编码密钥检测、只读测试集防作弊拦截、自动修复闭环与熔断回滚逻辑：

```bash
python -m pytest -v mvp/tests/
```

---

## 三、双场景演示说明

| 场景名称 | 初始门禁（Attempt #0） | 流水线响应机制 | 终态与产物 |
| :--- | :--- | :--- | :--- |
| `repair_demo` | Gate 1 PASS；Gate 2 **2/5 PASS, 3/5 FAIL**（基线聚合通过；`opt_out` 下架过滤、手机号/URL Token 脱敏、脏日志容错失败） | 编排器截取结构化 `pytest` 失败回溯注入 `CoderAgent` → 仅修改 `src/race_media_inspector.py` → 自动重跑门禁 **5/5 PASS** | `AWAITING_HUMAN_REVIEW`（输出含 Git Diff 与消耗账单的 `review_report.md`，严禁自动合并主干） |
| `escalation_demo` | Gate 1 PASS；Gate 2 **1/2 PASS, 1/2 FAIL**（历史测试断言索要明文手机号，与需求隐私脱敏红线直接冲突） | `CoderAgent` 坚守隐私红线且物理无权修改只读 `tests/` 目录 → 连续出现相同错误指纹 → 触发 `DUPLICATE_ERROR_FINGERPRINT` 熔断 → **自动 `git reset --hard` 回滚基线** | `ESCALATED_TO_HUMAN`（输出结构化 `EscalationTicket` 人工接管故障单） |

---

## 四、运行产物目录

每次运行都会在 `mvp/runs/<scenario>_<timestamp>/` 下生成完全隔离、可审计的沙箱产物：

- `pipeline.log`：完整状态机流转、门禁与熔断日志
- `review_report.md`：面向 Tech Lead 的 PR 人工 Review 报告（含 Git Diff 或 Escalation 故障单）
- `report.json`：机器可读的结构化审计记录（可直接对接评估看板）
- `src/` 与 `tests/`：沙箱独立 Git 仓库与特性分支 `agent/<scenario>`
