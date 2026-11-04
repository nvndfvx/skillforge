"""人审 Web UI：把三道勾做成一个本地点选页面。

只用标准库（`http.server`），不引任何第三方依赖 —— 这是全仓库的约束：
离线功能零依赖，装都装不起来的环境也能跑。

三条设计取舍：
1. **只绑 127.0.0.1**，且只服务一次请求就退出，不长期驻留。
2. **服务端渲染 + 表单 POST**，不用前端框架：断了网也能用，页面还能直接存档。
3. **跳过步骤刻意不做**：删步骤要重排 step_id 和 idempotency_key，让 UI 顺手改会埋坑。
   步骤顺序只让"确认"，要改就回去改 skill.json。
"""
from __future__ import annotations

import html
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .schema.models import Assertion, Skill
from .schema.types import AnchorKind, AssertionKind

DEFAULT_PRECHECK = r"R\-\d{6,}"

_STYLE = """
body{font-family:-apple-system,'Segoe UI',system-ui,sans-serif;max-width:920px;
     margin:0 auto;padding:24px 20px 80px;color:#1f2328;line-height:1.6}
h1{font-size:22px;margin:0 0 4px} .sub{color:#656d76;font-size:13px;margin-bottom:24px}
h2{font-size:16px;margin:28px 0 8px;padding-top:16px;border-top:1px solid #d0d7de}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #eaeef2;vertical-align:top}
th{color:#656d76;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
code{background:#f6f8fa;padding:1px 5px;border-radius:4px;font-size:13px}
.write{color:#9a6700;font-weight:600;font-size:12px}
.row{display:flex;gap:10px;align-items:baseline;padding:8px 0;border-bottom:1px solid #eaeef2}
.row .k{flex:0 0 190px;color:#656d76;font-size:13px}
.row .v{flex:1}
input[type=text]{width:100%;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;font:inherit}
label.opt{margin-right:18px;cursor:pointer}
button{margin-top:28px;background:#1f883d;color:#fff;border:0;border-radius:6px;
       padding:11px 22px;font-size:15px;cursor:pointer}
button:hover{background:#1a7f37}
.ok{color:#1a7f37;font-weight:600} .bad{color:#cf222e;font-weight:600}
ul{margin:8px 0;padding-left:22px} li{margin:4px 0}
"""


def _render_form(skill: Skill) -> str:
    """把技能渲染成表单。所有用户数据都过 html.escape。"""
    esc = html.escape
    rows = []
    for index, step in enumerate(skill.steps):
        target = step.target.name or step.target.css or "-"
        flag = '<span class="write">[写操作]</span>' if step.is_write else ""
        if step.value is None:
            value_cell = '<span class="sub">—</span>'
        else:
            value_cell = (
                f'<input type="text" name="value_{index}" value="{esc(str(step.value))}">'
            )
        op_cell = (
            f'<select name="op_{index}">'
            '<option value="keep">保留</option>'
            '<option value="drop">删除</option>'
            '<option value="up">上移</option>'
            '<option value="down">下移</option>'
            "</select>"
        )
        rows.append(
            f"<tr><td>{step.step_id}</td><td><code>{esc(step.action.value)}</code></td>"
            f"<td>{esc(str(target))}</td><td>{value_cell}</td>"
            f"<td>{flag}</td><td>{op_cell}</td></tr>"
        )

    assertions = []
    for step in skill.steps:
        for kind, assertion in (("business", step.assertion), ("technical", step.technical_assertion)):
            if assertion is None:
                continue
            key = f"a_{step.step_id}_{kind}"
            label = "业务" if assertion.kind is AssertionKind.BUSINESS else "技术"
            assertions.append(
                f'<div class="row"><div class="k">step{step.step_id} [{label}]</div>'
                f'<div class="v"><code>{esc(assertion.operator)}</code> '
                f'<code>{esc(str(assertion.expected))}</code><br>'
                f'<label class="opt"><input type="radio" name="{key}" value="yes" checked> 对</label>'
                f'<label class="opt"><input type="radio" name="{key}" value="no"> 不对</label>'
                f"</div></div>"
            )

    prechecks = []
    for step in skill.steps:
        if not step.is_write or step.precheck is not None:
            continue
        current = step.precheck.expected if step.precheck is not None else DEFAULT_PRECHECK
        prechecks.append(
            f'<div class="row"><div class="k">step{step.step_id} 查重正则</div>'
            f'<div class="v"><input type="text" name="p_{step.step_id}" '
            f'value="{esc(str(current))}" placeholder="{esc(DEFAULT_PRECHECK)}"></div></div>'
        )
    if not prechecks:
        prechecks.append('<p class="sub">这个技能没有需要补 precheck 的写步骤。</p>')

    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>人审 · {esc(skill.skill_id)}</title><style>{_STYLE}</style>
<h1>人审：{esc(skill.name or skill.skill_id)}</h1>
<div class="sub">skill_id <code>{esc(skill.skill_id)}</code> · 版本 <code>{esc(skill.version)}</code>
 · 当前状态 <code>{esc(skill.state)}</code></div>

<form method="post" action="/submit">
  <h2>① 步骤清单 —— 可以删、可以上下移、可以改值</h2>
  <table><tr><th>#</th><th>动作</th><th>目标</th><th>值（可改）</th><th></th><th>操作</th></tr>
  {''.join(rows)}
  </table>
  <p><label><input type="checkbox" name="steps_ok" value="1"> 这串步骤（含我刚改的）就是我要的流程，顺序也对</label></p>

  <h2>② 断言 —— 逐条确认</h2>
  {''.join(assertions) or '<p class="sub">这个技能没有断言（很不正常，回去看看编译结果）</p>'}

  <h2>③ 写操作的 precheck —— 怎么判断这单已经存在</h2>
  {''.join(prechecks)}

  <button type="submit">提交人审结果</button>
</form>
</html>"""


def _render_result(skill: Skill, blockers: list[str], changes: list[str]) -> str:
    esc = html.escape
    checklist = skill.review_checklist()
    items = "".join(
        f'<li>{"✅" if ok else "❌"} {esc(label)}</li>' for label, ok in checklist.items()
    )
    steps = "".join(
        f"<li>step{s.step_id} <code>{esc(s.action.value)}</code> "
        f"「{esc(str(s.target.name or s.target.css or '-'))}」</li>"
        for s in skill.steps
    )
    change_block = (
        "<h2>你这次改了步骤</h2><ul>"
        + "".join(f"<li>{esc(c)}</li>" for c in changes)
        + "</ul><p class=\"sub\">step_id 已按新顺序重排，幂等键和 precheck id 一起跟上了。</p>"
        if changes
        else ""
    )
    tail = (
        '<p class="ok">→ 三道勾齐了，可以 release。</p>'
        if not blockers
        else '<p class="bad">还有发布阻断：</p><ul>'
        + "".join(f"<li>{esc(b)}</li>" for b in blockers)
        + "</ul>"
    )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>人审结果</title><style>{_STYLE}</style>
<h1>人审结果</h1>
<div class="sub">skill_id <code>{esc(skill.skill_id)}</code> · 状态
 <code>{esc(skill.state)}</code> · 共 {len(skill.steps)} 步</div>
<h2>现在的步骤</h2><ul>{steps}</ul>
{change_block}
<h2>三道勾</h2>
<ul>{items}</ul>
{tail}
<p class="sub">已写回：<code>{esc(str(skill.meta.get("_saved_path", "")))}</code>　可以关掉这个页面了。</p>
</html>"""


class ReviewSession:
    """一次人审会话：渲染表单 → 收表单 → 改技能 → 写盘。"""

    def __init__(self, skill: Skill, out_path: Path | str) -> None:
        self.skill = skill
        self.out_path = Path(out_path)
        self.submitted = False
        self.blockers: list[str] = []
        self.answered = 0
        self.changes: list[str] = []

    # -- 表单 -> 技能 -------------------------------------------------------
    def apply(self, fields: dict[str, list[str]]) -> None:
        from .registry import save_skill

        def get(name: str, default: str = "") -> str:
            values = fields.get(name)
            return (values[0] if values else default).strip()

        original = list(self.skill.steps)
        changes: list[str] = []

        # 1) 先把断言答案按**原始下标**读出来（步骤一动，字段名就对不上了）
        answers: dict[int, dict[str, str]] = {}
        precheck_raw: dict[int, str] = {}
        for index, step in enumerate(original):
            answers[index] = {
                kind: get(f"a_{step.step_id}_{kind}", "yes")
                for kind, assertion in (
                    ("business", step.assertion),
                    ("technical", step.technical_assertion),
                )
                if assertion is not None
            }
            if step.is_write and step.precheck is None:
                precheck_raw[index] = get(f"p_{step.step_id}")

        # 2) 改值（按原始下标）
        for index, step in enumerate(original):
            if step.value is None:
                continue
            new_value = get(f"value_{index}", str(step.value))
            if new_value != str(step.value):
                changes.append(f"step{step.step_id} 的值：{step.value!r} → {new_value!r}")
                step.value = new_value

        # 3) 删除（下标集合）—— 不允许删空
        dropped = {
            index for index in range(len(original))
            if get(f"op_{index}", "keep") == "drop"
        }
        if len(dropped) >= len(original):
            changes.append("想删光所有步骤，已忽略：至少留一步")
            dropped = set()

        steps = [step for index, step in enumerate(original) if index not in dropped]
        for index in sorted(dropped):
            changes.append(f"删除了 step{original[index].step_id}")

        # 4) 上下移动（按表单顺序逐条生效；被删掉的跳过）
        for index in range(len(original)):
            op = get(f"op_{index}", "keep")
            if op not in ("up", "down") or index in dropped:
                continue
            step = original[index]
            pos = steps.index(step)
            if op == "up" and pos > 0:
                steps[pos - 1], steps[pos] = steps[pos], steps[pos - 1]
                changes.append(f"step{step.step_id} 上移")
            elif op == "down" and pos < len(steps) - 1:
                steps[pos], steps[pos + 1] = steps[pos + 1], steps[pos]
                changes.append(f"step{step.step_id} 下移")

        # 5) 按新顺序重排 step_id（幂等键、precheck id 一起跟上）
        self.skill.steps = steps
        self.skill.renumber_steps()
        self.changes = changes

        # 6) 断言与 precheck 按**对象身份**落回，它们跟着步骤走
        for index, step in enumerate(original):
            if index in dropped:
                continue
            for kind, assertion in (
                ("business", step.assertion),
                ("technical", step.technical_assertion),
            ):
                if assertion is None:
                    continue
                answer = answers[index].get(kind, "yes")
                assertion.confirmed_by = "human" if answer == "yes" else "llm_proposed"
                self.answered += 1
            raw = precheck_raw.get(index)
            if raw is not None and step.precheck is None:
                if raw == "-":
                    continue
                step.precheck = Assertion(
                    assertion_id=f"precheck-{step.step_id}",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="entity_exists",
                    expected=raw or DEFAULT_PRECHECK,
                    confirmed_by="human",
                )

        self.skill.meta["steps_reviewed"] = get("steps_ok") == "1"
        checklist = self.skill.review_checklist()
        self.skill.state = "reviewed" if all(checklist.values()) else "draft"
        self.blockers = self.skill.publish_blockers()
        self.skill.meta["_saved_path"] = str(self.out_path)
        save_skill(self.skill, self.out_path)
        self.submitted = True

    def render_form(self) -> str:
        return _render_form(self.skill)

    def render_result(self) -> str:
        return _render_result(self.skill, self.blockers, self.changes)


def build_server(
    skill: Skill, out_path: Path | str, port: int = 0
) -> tuple[ThreadingHTTPServer, ReviewSession]:
    """建好不启动。测试用 port=0 拿随机端口。"""
    session = ReviewSession(skill, out_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            self._send(200, session.render_form())

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/submit":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                session.apply(parse_qs(raw, keep_blank_values=True))
            except Exception as exc:  # noqa: BLE001
                self._send(500, f"<pre>人审结果没写进去：{html.escape(repr(exc))}</pre>")
                return
            self._send(200, session.render_result())

        def log_message(self, *args: Any) -> None:  # 别把请求打到 stdout
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd, session


def serve(
    skill: Skill, out_path: Path | str, port: int = 8765, open_browser: bool = True
) -> ReviewSession:
    """跑一个请求就退。只绑 127.0.0.1。"""
    httpd, session = build_server(skill, out_path, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"人审页面：{url}")
    print("  浏览器里点选完，提交后这个服务会自动退出。")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        while not session.submitted:
            httpd.handle_request()
    except KeyboardInterrupt:
        print("\n已取消，技能文件没有改动。")
    finally:
        httpd.server_close()
    return session


def dump_form(skill: Skill, out_path: Path | str) -> Path:
    """不启服务，直接把表单存成 HTML（可以发给别人离线看）。"""
    path = Path(out_path)
    path.write_text(_render_form(skill), encoding="utf-8")
    return path


__all__ = ["ReviewSession", "build_server", "dump_form", "serve"]
