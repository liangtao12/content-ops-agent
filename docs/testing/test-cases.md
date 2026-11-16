# 测试用例（15 条）— ContentOps Agent

测试对象：ContentOps Agent｜统计日期：2026-11-16｜范围：功能、权限、异常、幂等、恢复与测试文档

原始执行表格：[`artifacts/ContentOps_测试用例_15条.xlsx`](artifacts/ContentOps_测试用例_15条.xlsx)

## 总览

| ID | 模块 | 用例标题 | 优先级 | 类型 | 状态 | 缺陷 |
|---|---|---|---|:--:|---|:--:|
| CO-01 | 认证 | 首个用户注册后成为管理员 | P0 | 功能/权限 | ✅ 通过 | — |
| CO-02 | 认证 | 非法用户名和弱密码被拒绝 | P1 | 异常输入 | ✅ 通过 | — |
| CO-03 | 任务创建 | 合法内容任务进入人工审批 | P0 | 主流程 | ✅ 通过 | — |
| CO-04 | 任务创建 | 不支持的发布平台被拒绝 | P1 | 异常输入 | ✅ 通过 | — |
| CO-05 | 权限 | 内容运营不能审批，审核员可以审批 | P0 | 角色权限 | ✅ 通过 | — |
| CO-06 | 审批 | 审批通过后生成排期与审批令牌 | P0 | 主流程 | ✅ 通过 | — |
| CO-07 | 审批 | 编辑稿合规失败后重新中断 | P0 | 业务规则 | ✅ 通过 | — |
| CO-08 | 审批 | 审核员拒绝后流程终止 | P1 | 主流程 | ✅ 通过 | — |
| CO-09 | 合规 | 合规失败时人工放行必须填写理由 | P0 | 业务规则 | ✅ 通过 | — |
| CO-10 | 审批令牌 | 内容或平台变更后令牌失效 | P0 | 安全 | ✅ 通过 | — |
| CO-11 | 排期 | 同一幂等键重复提交只生成一条日程 | P0 | 幂等/并发 | ✅ 通过 | — |
| CO-12 | 流式接口 | SSE 推送顺序与中断状态正确 | P1 | 接口 | ✅ 通过 | — |
| CO-13 | 任务队列 | 按状态和关键词筛选任务 | P2 | 界面/查询 | ⏸ 设计待执行 | — |
| CO-14 | 故障恢复 | 服务重启后继续恢复审批 | P0 | 可靠性 | ❌ 失败 | CO-DEP-001 |
| CO-15 | 排期校验 | 非法和过去时间不得创建排期 | P1 | 边界/异常 | ❌ 失败 | CO-FUNC-001 |

统计：15 条｜通过 12｜失败 2｜设计待执行 1｜已执行率 93.3%｜已执行通过率 85.7%

---

## 明细

### CO-01 首个用户注册后成为管理员
- **优先级** P0　**类型** 功能/权限　**执行方式** 自动化
- **前置条件** 用户库为空
- **测试步骤** 调用 bootstrap，注册首个账号，查询 session 角色
- **预期结果** 首个账号角色为 `admin`，可管理成员
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_first_user_is_admin_and_invite_controls_registration`

### CO-02 非法用户名和弱密码被拒绝
- **优先级** P1　**类型** 异常输入　**执行方式** 自动化
- **前置条件** 注册接口可用
- **测试步骤** 提交过短用户名和纯数字密码
- **预期结果** 返回 422，不创建账号
- **实际结果** 自动化测试通过
- **证据** `backend/app/auth.py`、`tests/test_api.py`

### CO-03 合法内容任务进入人工审批
- **优先级** P0　**类型** 主流程　**执行方式** 自动化
- **前置条件** 已登录内容运营账号
- **测试步骤** 填写运营目标、账号、平台和品牌约束后提交
- **预期结果** 任务创建成功，状态为 `awaiting_human`
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_api_human_loop_and_persistence_endpoints`

### CO-04 不支持的发布平台被拒绝
- **优先级** P1　**类型** 异常输入　**执行方式** 自动化
- **前置条件** 已登录内容运营账号
- **测试步骤** 提交不在平台白名单中的平台
- **预期结果** 返回 422，不生成任务
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_api_rejects_unsupported_platform`

### CO-05 内容运营不能审批，审核员可以审批
- **优先级** P0　**类型** 角色权限　**执行方式** 自动化
- **前置条件** 分别登录 `operator` 与 `reviewer`
- **测试步骤** operator 提交审批，reviewer 再提交通过
- **预期结果** operator 返回 403，reviewer 返回 200
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_role_permissions_split_create_and_approval`

### CO-06 审批通过后生成排期与审批令牌
- **优先级** P0　**类型** 主流程　**执行方式** 自动化
- **前置条件** 任务处于 `awaiting_human`
- **测试步骤** 审核员选择 `approved`
- **预期结果** 任务 `completed`，生成排期和 `approval_token`
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_api_human_loop_and_persistence_endpoints`

### CO-07 编辑稿合规失败后重新中断
- **优先级** P0　**类型** 业务规则　**执行方式** 自动化
- **前置条件** 任务处于审批状态
- **测试步骤** 编辑正文并提交 `edited`
- **预期结果** 重新执行合规检查；失败时不排期，任务继续等待人工
- **实际结果** 自动化测试通过
- **证据** `tests/test_e2e.py::test_bad_edit_reinterrupts_instead_of_bypassing_review`

### CO-08 审核员拒绝后流程终止
- **优先级** P1　**类型** 主流程　**执行方式** 自动化
- **前置条件** 任务处于审批状态
- **测试步骤** 提交 `rejected`
- **预期结果** 任务状态变为 `rejected`，不生成排期
- **实际结果** 自动化测试通过
- **证据** `tests/test_e2e.py::test_human_reject_stops_pipeline`

### CO-09 合规失败时人工放行必须填写理由
- **优先级** P0　**类型** 业务规则　**执行方式** 自动化
- **前置条件** 任务合规未通过
- **测试步骤** 提交 `approved` 且 `override_reason` 为空
- **预期结果** 返回 400，禁止直接放行
- **实际结果** 自动化测试通过
- **证据** `tests/test_api.py::test_edit_requires_content_and_override_reason`

### CO-10 内容或平台变更后令牌失效
- **优先级** P0　**类型** 安全　**执行方式** 自动化
- **前置条件** 已有有效 `approval_token`
- **测试步骤** 修改标题、正文或平台后再次排期
- **预期结果** 验签失败，不允许排期
- **实际结果** 自动化测试通过
- **证据** `tests/test_reliability.py::test_approval_token_is_bound_to_content_and_platform`

### CO-11 同一幂等键重复提交只生成一条日程
- **优先级** P0　**类型** 幂等/并发　**执行方式** 自动化
- **前置条件** 相同内容和幂等键
- **测试步骤** 并发提交 8 次排期请求
- **预期结果** 只生成一个 `schedule_id`
- **实际结果** 自动化测试通过
- **证据** `tests/test_reliability.py::test_schedule_lock_is_idempotent_across_threads`

### CO-12 SSE 推送顺序与中断状态正确
- **优先级** P1　**类型** 接口　**执行方式** 自动化
- **前置条件** 已登录并创建合法任务
- **测试步骤** 调用 `/api/run/stream` 并读取事件
- **预期结果** 依次出现 `start`、`node`、`awaiting_human`、`done`，且需登录
- **实际结果** 自动化测试通过
- **证据** `tests/test_rag_stream.py::test_stream_emits_start_node_and_done`、`test_stream_requires_login`

### CO-13 按状态和关键词筛选任务
- **优先级** P2　**类型** 界面/查询　**执行方式** 界面回归
- **前置条件** 存在多种状态的任务
- **测试步骤** 筛选待审批并按标题关键词搜索
- **预期结果** 只显示匹配状态和关键词的任务
- **实际结果** —（设计完成，未执行，不计入通过率）
- **证据** `frontend/index.html`、`frontend/app.js`

### CO-14 服务重启后继续恢复审批
- **优先级** P0　**类型** 可靠性　**执行方式** 自动化
- **前置条件** 任务已中断且 checkpoint 已落盘
- **测试步骤** 创建新图实例并恢复同一 `thread_id`
- **预期结果** 能恢复审批并完成流程
- **实际结果** ❌ **失败**：checkpoint 保存时报 `JsonPlusSerializer` 缺少 `dumps`
- **关联缺陷** CO-DEP-001
- **证据** `tests/test_reliability.py::test_checkpoint_survives_new_graph_instance`

### CO-15 非法和过去时间不得创建排期
- **优先级** P1　**类型** 边界/异常　**执行方式** 补充执行
- **前置条件** 排期文件可写
- **测试步骤** 分别写入 `not-a-date` 与 `2020-01-01T00:00`
- **预期结果** 应拒绝非 ISO 格式和过去时间
- **实际结果** ❌ **失败**：实际均返回 `scheduled`，生成 S0001、S0002
- **关联缺陷** CO-FUNC-001
- **证据** `backend/app/tools.py::write_schedule`
