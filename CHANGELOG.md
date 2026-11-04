# 变更日志

本文件记录每个版本做了什么。版本号在 `skillforge/__init__.py`，**单一来源**。

## v0.7.0

新能力：

- **CLI 执行域**（`domain: "cli"`）：技能跑在命令行上，动作只有 `run`（命令模板），
  断言打在 `stdout + stderr` 上。复用同一套断言求值、失败分层、幂等门与回归闸。
  - 幂等门在 CLI 域的等价物是 `precheck_command`（返回 0 表示已存在 → 跳过）；
    没有它写操作直接 `blocked`，和浏览器域"没 precheck 就不许写"是同一条底线
  - `skillforge run` 按 `domain` 自动分派 browser(playwright) / cli(子进程)
- **桌面 GUI 明确不支持**（这是决定，不是没做完）：控件树比 DOM 脆一个数量级，
  桌面能拿到的业务证据也太弱（截图像素/窗口标题撑不起判据）。
  硬做只会多一个会骗人的域 —— 正确做法是把那软件的 API/命令行包成 CLI 域。

修复（审计出来的三个真问题）：

- **CLI 域接不进闸**：`run-golden` / `batch` 写死走 playwright，CLI 技能跑批直接报错，
  等于"越用越准"这个核心承诺对 CLI 域失效。现在 `run_golden` / `run_batch` 接受
  `runner`，不需要页面的域也能进闸；两个都没给时明确 `ValueError`，不静默跑空。
- **Windows 命令输出乱码**：原来无条件按 UTF-8 解，Windows 上老工具吐 GBK/CP437
  会被替换成乱码，断言可能因此**误判**（看着跑过了其实判定错）。
  改成先 UTF-8，失败退系统区域编码。
- 补测试：`domain` / `precheck_command` 的序列化往返、CLI 域过闸且能被单点翻转拦住。

## v0.6.1

- **分发包改名 `skillforge-engine`**：PyPI 上 `skillforge` 已被别人占用（v1.2.0）。
  导入名仍是 `skillforge`，命令仍是 `skillforge` —— 跟 `pip install beautifulsoup4` → `import bs4` 同理。
- 新增 `publish.yml`：PyPI trusted publishing（OIDC），**不需要任何 token 或密码**。
  首发用手动触发，PyPI 上配好 Trusted Publisher 后把 tag 触发打开即可。

## v0.6.0

打包与分发：

- 可以 `pip install` 了：`pyproject.toml` 补齐元数据（URLs、classifiers、可选依赖 `browser` / `dev`）
- 版本号改为**单一来源**（`dynamic = ["version"]` 读 `skillforge.__version__`），不再两处各改一次
- 新增 `release.yml`：打 `v*` tag 自动校验版本号 → 打包 → 建 Release → 挂 sdist + wheel
- CI 新增 `package` job：打包 → 检查 wheel 里没有泄漏 tests/examples/docs →
  装进干净 venv → **用装进去的包跑完全套离线测试**

新能力：

- `batch` —— 批量执行：失败隔离、断点续跑、节流、失败按层归类
- `canary` —— 灰度发布：粘性分桶路由、按版本统计、wait/promote/abort 三态判定；
  支持按单据 / **按用户** / **按分群白名单**三种分流维度
- `flow` —— 多技能编排：上一步的产出当下一步的入参，失败默认中止，不做假回滚
- `webui` —— 人审网页（纯标准库）：不仅能确认，还能**删步骤 / 上下移 / 改值**，
  提交时自动重排 `step_id` 并重建幂等键与 precheck id
- `record` —— 一条命令录演示，「看着人干一遍」这第一步终于有路可走
- `signals` —— 技术探针连续失败累积告警（H14），只喊不改
- `review -i` —— 交互式人审，逐条确认而不是背参数

修复：

- 引用正则只认 ASCII 开头，`{{乱写}}` 这种错法**不被识别**，校验放行、运行期才炸
- `StepResult.detail` 在业务断言失败时是空的，下游只能显示 `failed`，看不出原因
- `CanaryState.field` 遮蔽了 `dataclasses.field`，默认值初始化直接 TypeError
- Windows CI：cp1252 控制台打印中文抛 `UnicodeEncodeError`，把「没装浏览器」伪装成测试失败
- `--start-url` 形同虚设：编译产物的起始页是字面量，跑批时改不动

## v0.5.0

- 自动打补丁（`healer/patcher.py`）：改一版 → 跑闸 → 过闸才收，没过闸库里一个字都不改
- 回归基准集自动扩容（`golden/suggest.py`），并纠正"3 条太少因为分辨率低"这个说法：
  真正的问题是**覆盖面**
- `heal` 接成 CLI

## v0.4.0

- 交互式人审之外的完整闭环：录制 → 对齐 → 编译 → 人审 → 跑批 → 过闸 → 回滚
- 单调性闸（通过率不降 + 无单点翻转）
- 真浏览器验证：元素指纹兜底、三层失败定级、幂等门

## v0.3.0 及更早

- schema 冻结、录制器（注入 JS + 表单快照补录）、序列对齐（Needleman–Wunsch）
- 蒸馏器：两遍演示差出变量、自动发现业务单号做断言
- 执行器：幂等门、写操作唯一命中原位、断言分级（business 判死 / technical 只报信）
