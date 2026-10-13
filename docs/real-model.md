# 0.4：真实模型、真实文档、失败复盘与人审

## 交付内容

这轮接入了真实 Qwen3 模型和 llama.cpp 推理服务，增加模型 RPC 心跳、公开文档留出集、证据选择、拒答检查、人工盲评、reviewer 权限和技术验收签署。

实际用途是帮助维护本项目正在使用的模型服务：查询默认参数、健康状态、并发槽位、鉴权和缓存操作。备件示例仍保留为虚构业务演示；新评测来源为真实开源运行文档，不宣称来自真实客户。

## 模型与源资料

- 最终模型：Qwen3-4B-Q4_K_M，官方 Qwen GGUF，仓库 revision `bc640142c66e1fdd12af0bd68f40445458f3869b`。
- llama.cpp runtime：`b11577`，Windows x64 Vulkan。
- 本机：Ryzen 7 7840H / 15.2 GiB RAM / RTX 4060 Laptop GPU 8 GiB；上下文 4096，推理服务 1 个槽位，temperature=0，seed=42。
- 文档源：`ggml-org/llama.cpp` 的 `b11577/tools/server/README.md`。摘录与源文件 SHA256 记录于 `frontend/ops-bundle.json`。
- 模型、运行时压缩包均与上游 SHA256 校验；安装和运行中的 manifest 分开保存，避免把新下载权重误记成正在运行的旧模型。
- 下载、权重和缓存均在 `D:\CodexWork\cache\sentinellab-model`；没有上传权重到 GitHub。

许可证与来源见 [llama.cpp MIT](licenses/llama.cpp-MIT.txt)、[Qwen Apache 2.0](licenses/Qwen3-Apache-2.0.txt)、[官方模型仓库](https://huggingface.co/Qwen/Qwen3-4B-GGUF) 和 [固定文档源](https://github.com/ggml-org/llama.cpp/blob/b11577/tools/server/README.md)。

## 为什么改为证据选择

开发阶段发现模型能说出部分正确参数，却不能稳定生成字面准确的引用。0.6B、1.7B 和较早 prompt 的失败结果全部保留在 artifacts/real-model-development-*.json。

最终候选采用：

1. BM25 检索上下文，英文形态归一化与停用词减少普通词对排名的干扰。
2. 将上下文拆为稳定编号的证据行。模型只输出答案、evidence_id 和 abstain。
3. JSON grammar 限制证据编号来自当前上下文或 NONE。
4. 服务器将模型选中的编号还原为文档 ID 与原文摘录；无效证据会拒答。

**摘录由服务器从冻结上下文还原，模型负责选择证据。** 没有声称模型能自由生成逐字准确的引用。原始模型 JSON、请求摘要、model/profile、Token 和响应信息写入 generation Span。

选中真实原文仍不能自动证明语义蕴含。模型可能选对资料却答非所问；本次留出集就发现了一个这样的问题。

## 数据集与流程

12 条开发题用于调试，16 条留出题包括 12 条文档参数 / 运维问题与 4 条资料无法回答的问题。全部题目与文档摘录在模型测试之前固定。标准答案、split 和判分要求不会传入模型请求。

开发与留出使用相同文档主题、不同问法，因此主要验证问法变化与拒答，不是新领域泛化。问题数量有限、标签由工程人员整理，不能当作生产总体准确率。

模型选择、prompt 与检索改进只根据开发集作出。最终 4B 配置开发集通过 12 / 12 后，留出集首次运行基础版与证据选择版，再重复一次候选版本。没有看完留出集后改 prompt、标准答案或阈值重新取得绿灯。

预设演示门禁：16 条样本、至少 12 组、规则通过率 ≥ 80%、质量变化下界 ≥ -5 个百分点、最差组下降 ≤ 20 个百分点、P95 ≤ 30 秒，并要求 holdout-only。这是明确的技术演示政策，不是业务 SLA。

## 实测结果（2026-10-13）

| 实验 | 规则通过率 | P95 端到端耗时 | 结果 |
|---|---:|---:|---|
| 基础 RAG | 10 / 16，62.50% | 2383.25 ms | 多个引用 / 原文证据检查失败 |
| 证据选择与拒答 | 15 / 16，93.75% | 2727.75 ms | 认证参数题漏掉正确命令行 flag |
| 候选重复运行 | 15 / 16，93.75% | 1617.75 ms | 判分和答案均无差异 |

首次候选相较基线提高 31.25 个百分点，但门禁 **block**：keyfile 组从基线正确变成候选失败，违反最差组退化阈值。其他检查满足也不能覆盖该项失败。

失败题：如何提供“一行一个认证 key”的文件？候选回答重复了问题，没有明确给出 `--api-key-file`，尽管它选择了正确的证据行。平台把真实失败保存下来，不将“引用存在”当成“答案正确”。

重复试验使用相同 seed / temperature，是固定环境复现测试，不证明随机采样稳健性。候选第二次较快可能受推理缓存、预热和任务竞争影响，**不据此宣称优化提升了推理速度**。

原始记录见 [real-model-holdout.json](../artifacts/real-model-holdout.json)、[development.json](../artifacts/real-model-development.json)、模型 manifest 和 generation Trace。模型输出与结果未人工改写。

## 人工评审与签署

`review.html` 提供按题随机固定左右顺序的对照评审。隐藏模型版本、实验 ID、Trace ID、分数与标准答案；展示问题、答案和相关资料。选择左 / 右 / 相当 / 都不好，并写理由。

专用 reviewer key 仅允许读取盲评任务和投票，不能访问实验、Trace、摘要或签署接口。工程 writer 可以查看汇总；若 writer 自己参与，则它本身有权限查看版本，不能声称对它严格盲化。

投票由 key ID 标识，已提交投票不可修改，同 key 对每题只能一次。自动门禁通过、题目全部被覆盖、人审没有“两者都不好”且整体偏好支持候选时，writer 才能签署技术验收。

**当前真实评审待完成，签署记录为空。** 自动门禁本次被拦截，即使有人想批准也不能绕过。自动测试在人造隔离数据库验证投票与签署流程；浏览器检查只查看页面，不向真实项目提交假投票。

## 长 RPC、取消与计费边界

每次模型 RPC 都有独立心跳线程续租。取消或 fence 失效后，响应即使返回也不能写检查点或发布。模型请求本身可能已经完成；这种丢弃并不取消供应商侧推理，也不保证外部计费恰好一次。

目前 Token 来自真实推理服务 usage。local model 没有供应商账单，Trace 的 provider cost=0，不代表硬件和电力成本为零。配置远程端点但没有对账时，成本标为未知，不能凭空用 0 通过成本门禁。

模型端点由服务器配置，Job 不能提供任意 URL；API key 只保存在服务器配置或被忽略的 token 文件里。独立本地模型服务也有鉴权，后端读取 key，前端不拿到 key。

## 复现

```powershell
cd D:\agent-runtime
.\backend\.venv\Scripts\python.exe .\tooling\setup_local_model.py --size 4B --backend vulkan
.\start-model.ps1
.\start.ps1
.\backend\.venv\Scripts\python.exe .\examples\build_ops_benchmark.py
.\backend\.venv\Scripts\python.exe .\examples\real_model_benchmark.py --stage development
.\backend\.venv\Scripts\python.exe .\examples\real_model_benchmark.py --stage holdout
.\check.ps1
```

没有可用 Vulkan GPU 时，可明确选择 `--backend cpu` 与合适模型大小，成绩和速度不能沿用本机记录。setup script 用现有 Git credential-manager 减少 GitHub 匿名下载 API 的限流；公开权重请求不带 GitHub credential。

页面：交付台加载“真实运维文档 / 留出集”，选择真实模型版本；实测证据页查看失败；人审页逐题评审。也能导入自己的文档题集，之后接入获准使用的实际客户资料。

后续应将本次失败加入开发回归集，并新建独立留出集检验下一版，避免把已经看过的题重复包装成新验证。真实客户指标和业务签收需要实际业务参与，不能通过代码自动生成。
