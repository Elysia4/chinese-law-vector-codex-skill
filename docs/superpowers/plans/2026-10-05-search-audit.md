# 检索审计与回答闸门 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (禁止子代理时由当前代理 inline 执行)。

**Goal:** 为中国法律检索增加可验证的运行审计、语料哈希校验、JSON 审计输出和严格回答闸门。

**Architecture:** 用独立的 `audit_state.py` 负责规范化条文、计算哈希和构造审计状态；`build_vectors.py` 在索引元数据中保存哈希；`rank_search.py` 读取状态并按默认、JSON、严格三种模式输出。现有 BM25/向量排序保持不变。

**Tech Stack:** Python 3 标准库、现有脚本、`unittest`。

---

### Task 1: 哈希和审计状态模块

**Files:**
- Create: `scripts/audit_state.py`
- Test: `tests/test_audit_state.py`

- [ ] 写测试：同一条文输入得到稳定哈希；正文、状态或施行日期变化会改变条文哈希；旧索引缺少哈希时被标记为 `stale`。
- [ ] 运行 `python -m unittest tests.test_audit_state -v`，确认先失败。
- [ ] 实现规范化文本、条文哈希、集合总哈希和索引比较函数。
- [ ] 再运行测试，确认通过。

### Task 2: 向量索引保存哈希

**Files:**
- Modify: `scripts/build_vectors.py`
- Test: `tests/test_audit_state.py`

- [ ] 写测试：构造最小文档集合后，索引元数据包含 `corpus_hash`、`metadata_hash` 和 `clause_hashes`。
- [ ] 运行测试确认失败。
- [ ] 在构建索引前调用审计模块并写入元数据。
- [ ] 运行测试确认通过。

### Task 3: 检索脚本接入审计状态

**Files:**
- Modify: `scripts/rank_search.py`
- Test: `tests/test_rank_audit.py`

- [ ] 写测试：索引哈希一致时报告 `valid`，哈希不一致时报告 `stale` 并选择 BM25；JSON 输出可解析。
- [ ] 运行测试确认失败。
- [ ] 增加 `--audit-json` 和 `--strict` 参数，接入索引比较和运行状态构造，不改变排序逻辑。
- [ ] 在默认文本输出中加入审计状态，在严格模式下对事实不足/索引不可验证返回非零状态。
- [ ] 运行测试确认通过。

### Task 4: 文档和回归验证

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`

- [ ] 写入状态机、哈希字段、命令示例和回答闸门规则。
- [ ] 运行全部测试和关键 CLI 命令：`python scripts/rank_search.py --help`、`python -m unittest discover -v`。
- [ ] 检查 `git diff`，确认只包含本任务文件。
