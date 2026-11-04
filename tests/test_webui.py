"""离线测试：人审 Web UI（真起 HTTP 服务，真发请求）。

    python tests/test_webui.py
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from _helpers import temp_dir  # noqa: E402

from skillforge.registry import load_skill  # noqa: E402
from skillforge.schema.models import Assertion, Parameter, Precondition, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)
from skillforge.webui import DEFAULT_PRECHECK, build_server, dump_form  # noqa: E402


def make_skill() -> Skill:
    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[Parameter(name="客户名称", type="string")],
        preconditions=[Precondition(kind="on_page", locator_or_path="http://erp.local/new")],
        steps=[
            Step(
                step_id=1,
                action=ActionKind.FILL,
                target=Locator(role="textbox", name="客户名称"),
                value="{{客户名称}}",
            ),
            Step(
                step_id=2,
                action=ActionKind.CLICK,
                target=Locator(role="button", name="提交"),
                write_effect=WriteEffect.CREATE,
                idempotency_key="{{flow_run_id}}::2",
                assertion=Assertion(
                    assertion_id="a-entity",
                    kind=AssertionKind.BUSINESS,
                    anchor=AnchorKind.BUSINESS_ENTITY,
                    operator="entity_exists",
                    expected=r"R\-\d{6,}",
                    confirmed_by="llm_proposed",
                ),
                technical_assertion=Assertion(
                    assertion_id="t-url",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="url_match",
                    expected="/success",
                    confirmed_by="llm_proposed",
                ),
            ),
        ],
    )


class Live:
    """在后台线程里跑真 HTTP 服务。"""

    def __init__(self, skill, out_path):
        self.httpd, self.session = build_server(skill, out_path)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def get(self, path="/"):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def post(self, fields: dict[str, str]):
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.base + "/submit", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")


def test_form_renders_three_sections():
    with temp_dir() as tmp:
        skill = make_skill()
        with Live(skill, tmp / "skill.json") as live:
            status, body = live.get("/")
        assert status == 200
        assert "① 步骤清单" in body
        assert "② 断言" in body
        assert "③ 写操作的 precheck" in body
        # 步骤、断言、写标记都在
        assert "客户名称" in body and "提交" in body
        assert "[写操作]" in body
        assert 'name="a_2_business"' in body
        assert 'name="a_2_technical"' in body
        assert 'name="p_2"' in body
        assert 'name="steps_ok"' in body
        assert "提交人审结果" in body


def test_submit_writes_skill_and_sets_reviewed():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        skill = make_skill()
        with Live(skill, path) as live:
            status, body = live.post(
                {"steps_ok": "1", "a_2_business": "yes", "a_2_technical": "yes", "p_2": ""}
            )
        assert status == 200
        assert "人审结果" in body
        assert "可以 release" in body

        saved = load_skill(path)
        assert saved.state == "reviewed"
        assert all(saved.review_checklist().values()), saved.review_checklist()
        assert saved.steps[-1].precheck.expected == DEFAULT_PRECHECK
        assert saved.steps[-1].assertion.confirmed_by == "human"


def test_unchecked_steps_blocks_release():
    """第①道勾没勾 → 留在 draft，接口也得给出阻断。"""
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_skill(), path) as live:
            status, body = live.post({"a_2_business": "yes", "a_2_technical": "yes", "p_2": "X-1"})
        assert status == 200
        assert "还有发布阻断" in body

        saved = load_skill(path)
        assert saved.state == "draft"
        assert saved.review_checklist()["① 步骤顺序已人审"] is False


def test_rejecting_an_assertion_keeps_it_unconfirmed():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_skill(), path) as live:
            live.post(
                {"steps_ok": "1", "a_2_business": "no", "a_2_technical": "yes", "p_2": ""}
            )
        saved = load_skill(path)
        assert saved.steps[-1].assertion.confirmed_by == "llm_proposed"
        assert not all(saved.review_checklist().values())


def test_custom_precheck_is_kept():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_skill(), path) as live:
            live.post(
                {"steps_ok": "1", "a_2_business": "yes", "a_2_technical": "yes", "p_2": "PO-\\d{8}"}
            )
        assert load_skill(path).steps[-1].precheck.expected == "PO-\\d{8}"


def test_binds_loopback_only():
    """不允许绑到 0.0.0.0 —— 这是本机工具，不该暴露到局域网。"""
    with temp_dir() as tmp:
        httpd, _ = build_server(make_skill(), tmp / "skill.json")
        try:
            assert httpd.server_address[0] == "127.0.0.1", httpd.server_address
        finally:
            httpd.server_close()


def test_html_is_escaped():
    """技能名里的尖括号不能被当成标签执行。"""
    skill = make_skill()
    skill.name = '<script>alert("x")</script>'
    skill.steps[1].target = Locator(role="button", name="提交<b>")
    with temp_dir() as tmp:
        html_text = dump_form(skill, tmp / "form.html").read_text(encoding="utf-8")
    assert "<script>alert" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "&lt;b&gt;" in html_text


def make_three_step_skill() -> Skill:
    skill = make_skill()
    skill.steps.insert(
        1,
        Step(
            step_id=99,
            action=ActionKind.FILL,
            target=Locator(role="textbox", name="金额"),
            value="{{金额}}",
        ),
    )
    skill.renumber_steps()
    return skill


def test_drop_step_renumbers_and_keeps_precheck():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_skill(), path) as live:
            status, body = live.post(
                {"steps_ok": "1", "op_0": "drop",
                 "a_2_business": "yes", "a_2_technical": "yes", "p_2": "X-1"}
            )
        assert status == 200
        assert "删除了 step1" in body

        saved = load_skill(path)
        assert len(saved.steps) == 1
        assert saved.steps[0].step_id == 1, "删完后必须重排 step_id"
        assert saved.steps[0].action.value == "click"
        assert saved.steps[0].precheck.expected == "X-1"
        assert saved.steps[0].precheck.assertion_id == "precheck-1", "precheck id 要跟着新 step_id"


def test_move_down_reorders_and_edits_value():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_three_step_skill(), path) as live:
            status, body = live.post(
                {"steps_ok": "1", "op_0": "down", "value_1": "{{金额}}元",
                 "a_3_business": "yes", "a_3_technical": "yes", "p_3": "X-1"}
            )
        assert status == 200
        assert "step1 下移" in body

        saved = load_skill(path)
        assert [s.target.name for s in saved.steps] == ["金额", "客户名称", "提交"]
        assert [s.step_id for s in saved.steps] == [1, 2, 3]
        assert saved.steps[0].value == "{{金额}}元", "改值要跟着同一个 step 走"


def test_move_up():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_three_step_skill(), path) as live:
            live.post(
                {"steps_ok": "1", "op_2": "up",
                 "a_3_business": "yes", "a_3_technical": "yes", "p_3": "X-1"}
            )
        saved = load_skill(path)
        assert [s.target.name for s in saved.steps] == ["客户名称", "提交", "金额"]


def test_cannot_drop_all_steps():
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_skill(), path) as live:
            status, body = live.post(
                {"steps_ok": "1", "op_0": "drop", "op_1": "drop",
                 "a_2_business": "yes", "a_2_technical": "yes", "p_2": "X-1"}
            )
        assert status == 200
        assert "至少留一步" in body
        assert len(load_skill(path).steps) == 2, "删空必须被拒，且不写坏文件"


def test_assertion_answers_survive_step_moves():
    """步骤换位置之后，断言答案必须跟着原来那个 step，不能错位。"""
    with temp_dir() as tmp:
        path = tmp / "skill.json"
        with Live(make_three_step_skill(), path) as live:
            live.post(
                {"steps_ok": "1", "op_2": "up",
                 "a_3_business": "no", "a_3_technical": "yes", "p_3": "X-1"}
            )
        saved = load_skill(path)
        submit = [s for s in saved.steps if s.action is ActionKind.CLICK][0]
        assert submit.assertion.confirmed_by == "llm_proposed", "答了「不对」就该保持未确认"
        assert submit.technical_assertion.confirmed_by == "human"


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
