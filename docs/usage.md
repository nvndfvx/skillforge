# 安装与上手

从零装好、跑通第一个技能，约 15 分钟。

---

## 1. 环境要求

| | |
|---|---|
| Python | **≥ 3.10**（3.12 最稳） |
| 操作系统 | Windows / Linux / macOS |
| 数据库 / 服务 / GPU | **都不需要** |
| 运行时依赖 | 离线功能**零依赖**；控制浏览器才需要 playwright |

---

## 2. 三种安装方式

### 2.1 只想看看 / 跑测试（0 依赖，30 秒）

```bash
git clone https://github.com/nvndfvx/skillforge.git
cd skillforge
python tests/run_all.py
```

期望输出：

```
OK   test_aligner.py          22/22 passed
OK   test_cli.py               7/7 passed
...
全部通过（9 个文件）
```

一个第三方包都不用装。想立刻看效果：

```bash
python examples/demo_loop.py     # 完整闭环
python examples/demo_heal.py     # 自动打补丁 + 基准集扩到 10 条
```

### 2.2 装成命令行工具

```bash
pip install -e .                          # 在当前目录开发安装
# 或者直接从 GitHub 装
pip install git+https://github.com/nvndfvx/skillforge.git
```

装完就有 `skillforge` 命令：

```bash
skillforge --help
skillforge record --help
```

> 没装也能用，把 `skillforge` 换成 `python -m skillforge` 即可。

### 2.3 要真的控制浏览器

录制、真跑、自动修都需要 playwright + 一个 Chromium 内核浏览器。

**方式 A：标准装法**

```bash
pip install -e ".[browser]"
playwright install chromium        # 约 130MB，装在用户目录
```

**方式 B：仓库自带脚本（推荐在受限环境用）**

```bash
python scripts/bootstrap_playwright.py
```

它做的事和 A 等价，但更"老实"：

- 把 playwright 装进仓库的 `vendor/` 目录，**不写系统 Python**
- 浏览器直接用**系统已装的 Chrome/Edge**，不下载 130MB
- 绕过 pip 解压临时文件被杀软/沙箱锁住的问题

装了 `vendor/` 之后，运行真浏览器相关命令前记得让 Python 看到它：

```powershell
# PowerShell
$env:PYTHONPATH = "<repo>\vendor"
```
```bash
# bash
export PYTHONPATH="<repo>/vendor"
```

---

## 3. 验证装好了

| 命令 | 期望 |
|---|---|
| `skillforge --help` | 列出 10 个子命令 |
| `python tests/run_all.py` | `全部通过（9 个文件）` |
| `python tests/test_real_browser.py` | 有浏览器：`27/27 checks passed`；没装：`SKIP` |
| `python -m skillforge status --store ./store --skill-id demo` | 打印一段 JSON，`releases: []` |

`test_real_browser.py` 会 **SKIP 而不是失败**——没装 playwright 是正常的。

---

## 4. 第一次跑通：用仓库自带的示例页面

不需要你自己的系统，仓库里有两个测试页面可以练手。

### 4.0 拿到页面地址

```powershell
# PowerShell
$url = "file:///" + ((Resolve-Path tests\fixtures\form_v1.html).Path -replace '\\','/')
echo $url
```
```bash
# bash
url="file://$(pwd)/tests/fixtures/form_v1.html"
echo $url
```

页面长这样：客户名称、金额、提交按钮。提交后显示"提交成功 单号 R-2024xxxx 已创建"。

### 4.1 录第一遍（会弹出浏览器）

```bash
skillforge record --url "$url" --out demos/a.json --skill-id expense
```

浏览器打开后：

1. 客户名称填 **张三**
2. 金额填 **500**
3. 点 **提交**
4. 回到终端窗口，按 **回车**

终端会打印 `已保存 N 个动作`。

### 4.2 录第二遍 —— **必须换参数**

```bash
skillforge record --url "$url" --out demos/b.json --skill-id expense
```

这次填 **李四 / 800**，其余一样。

> ⚠️ **这是最容易翻车的一步。** 两遍参数一样 → 对齐会判定"没有变量" → 编出一个把所有值写死的假技能。
> 它编译期不报错，第一次换客户才崩。工具会给你一条严重预警，看到就重录。

### 4.3 编译成技能

```bash
skillforge compile --trace-a demos/a.json --trace-b demos/b.json \
    --out skill.json --skill-id expense --name 报销单录入
```

会打印：参数、步骤数、对齐预警、发布阻断。此时 `状态 draft`，还发不出去。

### 4.4 交互式人审（三道勾）

```bash
skillforge review --skill skill.json -i
```

它会逐条问你：

```
=== ① 步骤清单：确认顺序是不是你要的流程 ===
  1. goto   「-」 value='file:///.../form_v1.html'
  2. fill   「客户名称」 value='{{客户名称}}'
  3. fill   「金额」 value='{{金额}}'
  4. click  「提交」 value=None  [写操作]
这串步骤就是你要的流程、顺序也对？ [y/N] y

=== ② 断言：逐条确认 ===
  step4 [业务] entity_exists 'R\-\d{6,}'
    这条断言对不对？ [Y/n] y

=== ③ 写操作的 precheck ===
  step4「提交」查重正则（直接回车用 R\-\d{6,}）>  [直接回车]
```

答完：`状态：reviewed`、`→ 可以 release 了`。

### 4.5 建回归基准集

```bash
skillforge golden suggest --store ./store --skill-id expense \
    --skill skill.json --limit 12 --apply
skillforge golden list --store ./store --skill-id expense
```

> `suggest` 生成的用例**都是候选**（`origin=manual_edge`），必须人确认才能当判据。
> 为什么至少 10 条：基准集只能拦住"已经存在的用例"的退化。3 条集里没有的边界，
> 对应的回归**压根不会被发现**。

### 4.6 真浏览器跑批

```bash
skillforge run-golden --store ./store --skill skill.json \
    --start-url "$url" --out scores.json
```

`--start-url` 会覆盖技能里录制的起始页，方便换环境跑。

### 4.7 过闸发布

```bash
skillforge release --store ./store --skill skill.json --scores scores.json
```

全过就发布 v1.0.0；有失败会被**单调性闸拒绝**，并且**库里一个字都不会改**。

### 4.8 看状态 / 回滚

```bash
skillforge status --store ./store --skill-id expense
skillforge rollback --store ./store --skill-id expense        # 退回上一版
skillforge rollback --store ./store --skill-id expense --to 1.0.0
```

### 4.9 制造一次"页面改版"，看它自己修

改一下页面再跑：用 `tests/fixtures/form_v2.html`（按钮文案从「提交」变成「确认保存」）。

```bash
skillforge heal --store ./store --skill skill.json --start-url "$url_v2"
```

它会：跑一次 → 失败 → 三层定级 → 挑算子 → 打补丁 → 跑基准集 → 过闸就收，过不了就转人工。

---

## 5. 数据落在哪

```
./store/<skill_id>/
├── store.json          当前版本指针 + 全部发布记录（含每条用例结果）
├── golden.json         回归基准集（只增不删）
├── signals.json        技术探针的连续失败计数（H14）
└── versions/<v>.json   每个版本的完整技能
```

`skill.json`（草稿/人审产物）是你自己指定路径的文件，不在 store 里。

---

## 6. Windows 注意

真踩过的两个坑。

### 6.1 控制台不是 UTF-8

Windows 默认 cp1252 / cp936，打印中文可能抛 `UnicodeEncodeError`。

- **仓库自带脚本已经处理了**（自动切 UTF-8 stdout）
- **你自己写脚本**要加：

```python
import sys
if hasattr(sys, "stdout") and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
```

或者临时设环境变量：`$env:PYTHONIOENCODING = "utf-8"`

### 6.2 git 不走系统代理

现象：`git push` 报 `Could not resolve host: github.com`，但浏览器能打开 GitHub。

原因：git 用自己的 DNS/连接，不读 Windows 系统代理设置。

```powershell
# 只给当前仓库
git config http.proxy http://127.0.0.1:7890
git config https.proxy http://127.0.0.1:7890

# 确认
git config --get http.proxy

# 取消
git config --unset http.proxy
```

端口换成你代理软件的实际端口（Clash 常见 7890 / 7897，V2Ray 常见 10809）。

`playwright install chromium` 下载慢或失败时，同样可以给进程设代理：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
playwright install chromium
```

### 6.3 pip 装不上（临时目录被锁）

如果 `pip install playwright` 报 `Permission denied ... \Temp\pip-unpack-...`：

```bash
python scripts/bootstrap_playwright.py
```

它直接下 wheel 解压到 `vendor/`，完全绕开 pip 的临时目录。

---

## 7. 常见问题

**Q：编译出来 0 个变量？**
两遍演示的参数没变。重录第二遍，换客户名/金额/日期。

**Q：没报 `fragile` 预警，是不是没生效？**
正常。`fragile` 预警的是"两次演示之间页面就变了"（比如开发正在上线），
两次录制间隔几分钟、页面没变，本来就不该触发。真正防漂的是运行时的指纹兜底。

**Q：运行报 `locator_not_found`？**
页面改版了。先跑 `skillforge heal` 让它试着自己修；修不了说明文字类特征全变了，需要人给新定位器。

**Q：写步骤直接 `blocked`，说缺 precheck？**
这是设计如此——**没有"怎么查这单已存在"就不许写**，避免超时重试造成重复提交。
用 `skillforge review -i` 补上。

**Q：能用来操作桌面软件吗？**
不能。只支持浏览器域。桌面 GUI 的稳定性差一个数量级。

**Q：一天要跑几千次行不行？**
不行。那个量级应该直接反解接口调 API，不是开浏览器点。

**Q：它能替我判断业务规则变了吗？**
不能。这是它的天花板——业务规则变了，它会把错的流程**做得又快又稳**。
所以人审是必需的一道闸，不是可选项。

---

## 8. 卸载 / 清理

```bash
pip uninstall skillforge
rm -rf vendor/ .wheels/ .browsers/ .tmp/ store/ demos/ skill.json scores.json
```

playwright 自己装的浏览器在用户目录，用 `playwright uninstall --all` 清。
