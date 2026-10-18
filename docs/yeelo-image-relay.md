# 出图中转站：YeeMagine（2026-10-18 已实跑验证）

当前部署 = `D:/project/H3-MotionStudio`。凭据只放在 Windows 用户级环境变量里，仓库内不写 key。

## 配置（注册表优先，进程环境里的旧值会被覆盖）

`H3_BATCH_IMAGE_BASE_URL=https://img.yeelo.fun/v1`、`H3_BATCH_IMAGE_API_KEY`（私有）、
`H3_BATCH_IMAGE_MODEL=gpt-image-2`、`H3_BATCH_IMAGE_MODE=multi`、`H3_BATCH_IMAGE_FIELD=image`、
`H3_BATCH_IMAGE_QUALITY=auto`。这六项在 `settings._REGISTRY_FIRST_KEYS` 里，改注册表后
**重启后端**即生效，不必改环境。

## 接口形态

- 提交：`POST {base}/images/edits`，multipart，字段 `image`（多参考图就重复该字段），
  另带 `async=true`、`response_format=url`；**不收 `input_fidelity`**（发送前剔除）。
- 取结果：`GET {base}/images/tasks/{task_id}`（`Authorization: Bearer <key>`），
  返回 `status` 与图片地址，`result/response/output/data.images/image_url` 等包装都能识别。
- 实测（无计费）：`GET /v1/images/tasks/<不存在>` → 404 `任务不存在`；
  `/v1/models` 是 404（`netproxy` 的连通性探测只看能否连上，不看状态码，因此不受影响）。

## 2026-10-18 实跑结论（1 条作品、3 张图，共约 4 分钟）

用 `040_跳舞_love~e~e~e` 的抽帧 + 固定身份图，在 `data/relay_check_20261018/pub/` 的副本上按
`/images` 单步流程跑完，未改动发布目录任何成品：

| 步骤 | 请求 | 结果 | 用时 |
| --- | --- | --- | --- |
| 人物图 | 双参考（糊脸抽帧 + 身份图）、720x1280、9:16 | 1440x2560（VOSR2 2× 放大） | 102s |
| B站封面 | 单参考（人物图）、1024x768、4:3 | 1448x1086 | 70s |
| 抖音封面 | 单参考（人物图）、768x1024、3:4 | 1086x1448 | 62s |

**已确认**：多参考（两张 `image`）被接受并生效（生成脸与固定身份图一致，不是糊脸抽帧那张）、
异步任务提交/轮询/下载链路正常、1K 档尺寸按请求返回、VOSR2 放大可用、封面按 `TARGETS`
精确裁切、收尾无残留检查点/临时文件。成图见上述目录。

## 成本与失败保护

- 一次「人物图 + 两张封面」= 3 笔出图请求；不自动重试格式、不自动换方案、不并发重复提交
  （`_BILLED_INFLIGHT` 按 `request_key` 去重）。
- 内容政策 400 **默认不自动重发**（新渠道「不计费」这一前提没有验证过）。旧渠道的自动重发
  由 `H3_BATCH_IMAGE_POLICY_RETRY=1` 显式打开。
- 任务记录：`.relay-task.json` 与输出图同目录，记录 `baseUrl/model/taskId/fingerprint`，**不含 key**。
  提交前先落盘再轮询；中断/重启后只会 `GET` 同一条任务或直接取已存结果，**绝不重新提交**。
  图片成功落盘后**立刻删除**记录（否则下一次明确重做会拿到旧结果，或直接报「输入不同」）。
- `.request-pending`（结果未知闸门）与任务记录同时存在时**允许续跑**：续跑只是 GET。
  只有「没有任务记录」的 pending 才需要人工核对后台。
- 换站点只改配置：`H3_BATCH_IMAGE_ASYNC=1|0` 覆盖异步判定，代码不认死主机名。

## 验证与已知边界

`tests/test_yeelo_relay.py`（9 条）覆盖：表单字段、单次计费 POST、续跑只 GET、结果地址刷新、
未知响应 fail closed、轮询失败不误判「未提交」、非 yeelo 主机同样识别 task_id、
落盘后删记录、有任务记录时 `.request-pending` 不阻断续跑。

`tests/test_backend.py` 与 `tests/test_source_face_guard.py` 里另有与本改造无关的既有失败
（例如提示词措辞已改但断言没跟上），不是这次换中转站引入的，未在本轮修复。
