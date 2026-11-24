# Contributing to Agent SDLC Orchestrator

感谢你关注并愿意参与 **Agent SDLC Orchestrator** 的开源共建！本项目致力于探索可控、可审计、具备生产级质量门禁的 AI 研发流水线工程实践，欢迎任何形式的 Issue、PR、业务场景案例与门禁规则贡献。

## 一、推荐贡献方向（Good First Issues）

- **新增行业业务场景（`mvp/scenarios/`）**：补充更多高价值的内部工具场景（如财务对账巡检、日志异常告警、配置漂移检测），包含 `requirements.md`、初始基线 `src/` 与只读 `tests/`。
- **扩展质量门禁规则（`mvp/orchestrator/quality_gate.py`）**：接入 `ruff` / `mypy` / `bandit` 或自定义 AST 复杂度与安全规则。
- **扩展模型后端与路由策略（`mvp/orchestrator/llm_client.py`）**：补充更多国内模型平台预设、自动降级路由（如首轮失败自动从 `deepseek-flash` 升级至 `deepseek-v4-pro`）与成本报表导出。
- **CI/CD 集成适配器**：对接 GitHub Actions / GitLab CI Webhook，自动创建 PR 并回写门禁评论。

## 二、开发与提交流程

1. **Fork 本仓库**并创建特性分支（遵循 `feat/<topic>`、`fix/<topic>` 或 `docs/<topic>` 命名）：
   ```bash
   git checkout -b feat/add-ruff-gate
   ```
2. **安装依赖并运行测试**（确保所有单元测试与离线 E2E 全绿）：
   ```bash
   pip install -r mvp/requirements.txt
   python -m pytest -v mvp/tests/
   python mvp/run_pipeline.py --scenario all --provider mock
   ```
3. **遵循代码与安全规范**：
   - Python 代码遵循 PEP 8 与类型注解（Type Hints）。
   - **严禁提交任何 `.env` 文件、API Key 或真实生产敏感数据**。
   - Commit Message 遵循 [Conventional Commits](https://www.conventionalcommits.org/)（如 `feat:`、`fix:`、`docs:`、`test:`、`refactor:`）。
4. **提交 Pull Request**：在 PR 描述中写清**解决的问题、设计取舍、测试验证输出**，等待 Maintainer 人工 Code Review 后合入。

## 三、问题反馈

如遇到 Bug 或有新功能构想，请直接提交 [GitHub Issue](https://github.com/kbzce745/agent-sdlc-orchestrator/issues)，并附上复现命令、Python 版本及脱敏后的运行日志。
