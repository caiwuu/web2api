# Type Tab 调度方案（当前实现）

`core/runtime/browser_manager.py` 已经不再维护旧版“动态 page pool / page slot”模型。

当前实现是更直接的 `browser -> type tab` 方案。

## 1. 基本约束

- 一个 `ProxyKey` 对应一个浏览器进程
- 一个浏览器内，一个 `type` 只允许一个 tab
- 一个 tab 绑定一个账号
- 一个 tab 下可以承载多个 session
- 一个 tab 可以并发承载多个请求，但受 `scheduler.tab_max_concurrent` 限制

## 2. 为什么不再使用 page pool

旧 page pool 的问题是：

- 同一个 type 在同一浏览器里会存在多个 page，状态边界不清楚
- 切代理/切账号时容易把同 type 的其他请求一起打断
- session 与 page 的绑定关系不稳定

当前改为“每个 type 一个 tab”之后：

- 账号身份边界更清晰
- session 可以稳定绑定到 tab/account
- 切号只能发生在 drained 后，不会中断正在运行的请求

## 3. Tab 状态

运行时并不靠复杂的 page 池扩缩容，而是直接维护 tab 状态：

- `ready`
  可接新请求
- `busy`
  有活跃请求
- `draining`
  不再接新请求，等待活跃请求结束
- `switching`
  正在当前 page 上切换账号
- `frozen`
  当前账号额度耗尽，等待恢复或切号

## 4. 新请求如何选 tab

### 情况 A：命中已有 session

若请求里带最新的 `session_id`，并且该 session 对应 tab/account 仍有效：

- 直接复用该 tab
- 不回放完整历史

### 情况 B：新建 session

若无法复用旧 session，则按顺序选择：

1. 已打开浏览器里已有该 `type` 的可服务 tab
2. 已打开浏览器里没有该 `type` tab，但该组有可用账号，可直接开新 tab
3. 已打开浏览器里该 `type` tab 已 drained，且同组有备用账号，可原地切号
4. 若都不满足，再新开浏览器并创建该 `type` tab

## 5. 并发模型

当前没有“多个同 type page slot”。

并发由单 tab 的 `active_requests` 控制：

- 每次请求进入时占用该 tab
- 请求结束时释放
- 若达到 `tab_max_concurrent`，该 tab 暂不接新请求
- 新请求会优先被调度到其他浏览器上的同 type tab
- 若没有现成 tab，才会继续开新 tab 或新浏览器

这意味着：

- 同 type 的横向扩展靠“多浏览器”，不是“同浏览器多 page”
- 同浏览器内，同 type 的状态始终只有一份

## 6. 切号规则

tab 绑定账号后，不会在请求中途切号。

只有满足：

- `active_requests == 0`
- tab 已处于 `draining/frozen`

才允许切号。

切号发生在同一个 page 上：

1. 先让旧 tab 停止接新请求
2. 等待活跃请求全部结束
3. 失效旧账号下的 session
4. tab 绑定到新账号并重新回到 `ready`

## 7. 额度耗尽后的行为

当插件报告当前账号额度耗尽时：

1. 当前 tab 被标记为 `frozen`
2. 该 tab 停止接新请求
3. 该 tab 下已有 session 全部失效
4. 当前失败请求会重试，并重新寻找其他资源

之后会有三种结果：

- 原账号恢复可用：tab 恢复
- 同组有其他账号：tab 在 drained 后切号
- 同组无其他账号：关闭该 tab

## 8. 浏览器回收

浏览器回收也不再依赖 page pool 空槽位，而是直接看 tab 的空闲状态。

一个浏览器会被回收，当且仅当：

- 其下所有 tab 都没有活跃请求
- 所有 tab 都超过空闲阈值
- 当前浏览器总数大于 `resident_browser_count`

## 9. 结论

如果后续继续讨论“调度 / 并发 / 切号 / 回收”，请以这套模型为准：

- 当前实现是 **一个浏览器一个代理组，一个 type 一个 tab**
- 不是旧版的 **多 page slot page pool**
