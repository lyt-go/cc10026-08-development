# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则（可发布/撤回正式批次） | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务（可重办交付） | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 归并撤回回执与交付重办

正式归并批次发布后会冻结来源与列结构，导出任务只能把列落到同一份“已确认”
批次上。发现批次引用了错误来源时：

1. 在 `data_merge` 的规则上执行 `撤回正式批次`（可带 `remark` 说明原因），
   系统签发撤回回执并写入 `data/receipts/RCPT-xxxx.json`；
2. 回执逐笔列出受影响登记：已经出过文件的转为 `已撤回`，没出文件的转为 `待重办`，
   旧交付不删除，状态改为 `已撤回` 并保留原批次引用，供事后对账；
3. 撤回批次不能再生成新交付；更正来源补齐后执行 `登记更正来源`，再执行
   `发布更正批次`，更正批次通过 `corrected_from` 关联原归并；
4. 在 `export_task` 上执行 `重办导出`，新交付同时记录原归并批次、实际数据源和
   新文件；同一登记的新旧交付各自身份独立，各自引用自己的批次。

相关接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/data_merge/batches?rule_id=` | 正式批次台账与更正关联 |
| `GET /api/data_merge/receipts[/{receipt_no}]` | 撤回回执列表/详情 |
| `GET /api/export_task/registrations` | 导出登记状态（已登记/已交付/已撤回/待重办/已重办） |
| `GET /api/export_task/{id}/deliveries` | 单个登记的全部交付身份 |
| `POST /api/export_task/{id}/actions` `{"action":"重办导出"}` | 按回执重办有效交付 |

交付文件单独存放在 `data/exports/deliveries/<交付编号>.csv|.json`，撤回不会覆盖
旧交付文件。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
