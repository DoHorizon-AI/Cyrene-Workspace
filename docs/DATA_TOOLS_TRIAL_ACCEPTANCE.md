# Catalyst and Echo data-tools trial acceptance

This document describes the fixed synthetic acceptance checks for the first
Catalyst/Echo data-tools trial. It complements the local startup and operator
instructions supplied with `packaging/data_tools_trial.py`.

本文说明 Catalyst/Echo 数据工具首轮试用的固定合成验收。它补充
`packaging/data_tools_trial.py` 提供的本机启动与操作说明。

## Fixed inputs and evidence

`tests/fixtures/data-tools-trial/` contains a one-page text PDF, a DOCX with
Chinese text, emoji and an actual table, 20 synthetic instruction rows in ten
source families, a four-row Echo reference/actual sample, malformed inputs,
and a duplicate Echo sample-ID negative case. The same PDF bytes are uploaded
under two logical source identities with opposing knowledge/training policy.
All content is synthetic and carries no customer data.

夹具包括可抽取文本的单页 PDF、包含中文/emoji/真实表格的 DOCX、按十个来源组组织的
20 条合成指令样本、四行 Echo reference/actual 样本、损坏文件，以及重复 Echo
sampleId 负例。同一 PDF 字节会按两个逻辑来源上传，并设置相反的知识/训练策略。
所有数据都是合成内容，不含客户数据。

The live verifier checks source identity and byte digests, parser sentinels and
DOCX table locators, human edits and review, policy edits, both immutable
DatasetVersion outputs, independent knowledge-package digest/ACL checks, SFT
split membership through `provenance.jsonl`, private answer-field exclusion,
Echo exact-match coverage and answer-free report export, malformed input
rejection, authenticated access when a token is configured, cross-instance
workspace isolation when requested, and published-version staleness after a
later approved revision. Model-backed QA generation stays opt-in.

真实 API 验收覆盖来源身份和字节摘要、解析结果及 DOCX 表格定位、人工修改与审核、策略
修改、DatasetVersion 的两个不可变输出、独立知识包的摘要/ACL 校验、通过
`provenance.jsonl` 检查 SFT 分组、训练行管理字段排除、Echo 精确匹配覆盖和不含答案的
报告、损坏输入拒绝、配置令牌后的鉴权、按需跨实例工作区隔离，以及后续批准新内容后旧
版本的过期状态。基于模型的 QA 生成默认不调用，需显式 opt-in。

## Commands

Run the small deterministic checks from the Workspace task tree:

```bash
python3.12 workspace/scripts/verify-data-tools-trial.py --fixtures-only
python3.12 -m pytest -q workspace/tests/test_data_tools_trial.py
```

The real native launcher smoke starts and stops an isolated local stack, reads
its actual Product health and direct Plugin readiness records, and uses a
temporary state directory:

```bash
python3.12 workspace/scripts/verify-data-tools-trial.py --launcher-smoke
```

This command prepares the pinned local runtime dependencies. Run it only when
the task worktree's Python environment and dependency cache are ready. It does
not run the data workflow itself.

To run the full API acceptance against an already-started loopback instance,
export only the URLs; local loopback mode does not require a bearer token:

```bash
export CYRENE_TRIAL_CATALYST_URL=http://127.0.0.1:8014
export CYRENE_TRIAL_ECHO_URL=http://127.0.0.1:8094
python3.12 workspace/scripts/verify-data-tools-trial.py
```

Remote trial services require `CYRENE_DATA_TOOLS_TOKEN` in the verifier's
environment. Do not put tokens in command arguments. Cross-workspace checks
also require separate secondary Catalyst/Echo instances, a distinct secondary
token, and `--require-isolation`; one Product instance intentionally maps one
token to one workspace identity.

远程试用服务需要在验收进程环境中设置 `CYRENE_DATA_TOOLS_TOKEN`，不要把令牌写入命令
参数。跨工作区检查还需单独启动第二组 Catalyst/Echo 实例、使用不同令牌并指定
`--require-isolation`；每个 Product 实例只将一个令牌映射到一个工作区身份。

## Acceptance status

Fixture and verifier unit checks can run without starting Product services.
The opt-in live API and launcher checks remain `NOT_RUN` until their exact
commands complete successfully against the current task worktree. A passing
fixture check alone is not evidence that Catalyst, Echo, Client or remote
access is usable by a trial organization.

夹具与 verifier 单元检查无需启动 Product 服务。真实 API 和 launcher 检查必须在当前
任务 worktree 上实际运行成功后才能标记为通过。仅夹具检查通过，不能证明 Catalyst、
Echo、Client 或远程访问已可供试用单位使用。
