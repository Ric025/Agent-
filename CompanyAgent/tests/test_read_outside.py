"""读当前文件夹以外的文件（批准级别 read_outside）和查看电脑配置（system_info）。

全放行：直接读；自动、每步都问：先问用户，同意了才读；派出去的助手只在全放行时能读；
存密码、密钥的地方和本程序的数据目录任何方式都不读。写文件仍然只在工作区里。"""
from __future__ import annotations
import json, os, shutil, sys, tempfile, unittest
from types import SimpleNamespace
from helpers import Case, wait_until
from agent import policy
from agent.tools import REGISTRY, load_all
from agent.tools import system as systool

load_all()


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class Outside(Case):
    def setUp(self):
        super().setUp()
        self.out = tempfile.mkdtemp(prefix="ca_outside_")
        self.addCleanup(shutil.rmtree, self.out, True)
        with open(os.path.join(self.out, "外面的报告.txt"), "w", encoding="utf-8") as f:
            f.write("第三季度利润 1234 万")
        self.target = os.path.join(self.out, "外面的报告.txt")

    def last_result(self, e, sid):
        return e.results(sid)[-1]

    # ---- 判定 ----
    def test_decide(self):
        self.assertEqual(policy.decide("read_outside", "manual"), "ask")
        self.assertEqual(policy.decide("read_outside", "auto"), "ask")
        self.assertEqual(policy.decide("read_outside", "full"), "allow")
        self.assertEqual(policy.RISK_LABEL["read_outside"], "读文件夹外的文件")

    def test_risk_by_path(self):
        e = self.env([])
        ctx = SimpleNamespace(root=e.root, store=e.rt.store, cfg=e.cfg)
        rf = REGISTRY["read_file"]
        self.assertEqual(rf.risk_of(ctx, {"path": "a.txt"}), "read")
        self.assertEqual(rf.risk_of(ctx, {"path": os.path.join(e.root, "a.txt")}), "read")
        self.assertEqual(rf.risk_of(ctx, {"path": self.target}), "read_outside")
        self.assertEqual(rf.risk_of(ctx, {"path": "../x.txt"}), "read_outside")
        self.assertEqual(rf.risk_of(ctx, {"path": "~/x.txt"}), "read_outside")
        self.assertEqual(REGISTRY["search_files"].risk_of(ctx, {"query": "x"}), "read")
        self.assertEqual(REGISTRY["list_dir"].risk_of(ctx, {"path": self.out}), "read_outside")
        diff = REGISTRY["excel_diff"]
        self.assertEqual(diff.risk_of(ctx, {"path_a": "a.xlsx", "path_b": "b.xlsx", "output_path": "d.xlsx"}), "create")
        self.assertEqual(diff.risk_of(ctx, {"path_a": self.target, "path_b": "b.xlsx", "output_path": "d.xlsx"}), "read_outside")
        self.assertEqual(REGISTRY["write_pptx"].risk_of(ctx, {"path": "a.pptx", "template": self.target}), "read_outside")
        self.assertEqual(REGISTRY["write_pptx"].risk_of(ctx, {"path": "a.pptx"}), "create")
        # 写文件不因为路径在外面就变成"读"：照旧被工作区挡住
        self.assertEqual(REGISTRY["write_file"].risk_of(ctx, {"path": self.target, "content": "x"}), "create")

    def test_secret(self):
        home = os.path.expanduser("~")
        self.assertTrue(policy.is_secret(os.path.join(home, ".ssh", "config")))
        self.assertTrue(policy.is_secret("/some/where/id_ed25519"))
        self.assertTrue(policy.is_secret(os.path.join(home, "Library", "Keychains", "login.keychain-db")))
        self.assertTrue(policy.is_secret("/data/app/settings.json", extra=("/data/app",)))
        self.assertFalse(policy.is_secret(os.path.join(home, "Documents", "报表.xlsx")))
        self.assertFalse(policy.is_secret(self.target))

    # ---- 全放行：直接读 ----
    def test_full_reads_directly(self):
        e = self.env([tc(("read_file", {"path": self.target})), tc(("list_dir", {"path": self.out})),
                      tc(("search_files", {"query": "利润", "path": self.out})), {"content": "利润 1234 万"}], mode="full")
        sid = e.start("看看那份报告")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        res = e.results(sid)
        self.assertTrue(res[0]["ok"], res[0])
        self.assertIn("1234", res[0]["content"])
        self.assertIn(self.target, res[0]["content"])          # 结果里写完整路径
        self.assertIn("外面的报告.txt", res[1]["content"])
        self.assertIn("1234", res[2]["content"])
        self.assertEqual([a["risk"] for a in e.rt.store.actions(sid)], ["read_outside"] * 3)
        self.assertFalse(e.events(sid, ("approval_requested",)))
        sysmsg = e.mock.calls[0][0]["content"]
        self.assertIn("批准方式：全放行", sysmsg)
        self.assertIn("system_info", sysmsg)

    def test_full_still_refuses_secrets_and_own_data(self):
        own = os.path.join(self.env([]).home, "settings.json")
        fake_key = os.path.join(self.out, "id_rsa")
        with open(fake_key, "w") as f:
            f.write("PRIVATE")
        e = self.env([tc(("read_file", {"path": fake_key})), tc(("read_file", {"path": "~/.ssh/config"})),
                      {"content": "读不了"}], mode="full")
        own = os.path.join(e.home, "settings.json")
        e.mock.script.insert(2, tc(("read_file", {"path": own})))
        sid = e.start("读密钥")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        res = e.results(sid)
        self.assertEqual(len(res), 3)
        for r in res:
            self.assertFalse(r["ok"], r)
            self.assertIn("密码、密钥", r["content"])
        self.assertNotIn("PRIVATE", json.dumps(res, ensure_ascii=False))

    def test_full_cannot_write_outside(self):
        e = self.env([tc(("write_file", {"path": os.path.join(self.out, "新.txt"), "content": "x"})), {"content": "写不了"}],
                     mode="full")
        sid = e.start("写到外面")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertFalse(e.results(sid)[0]["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "新.txt")))

    # ---- 自动：先问 ----
    def test_auto_asks_then_reads(self):
        e = self.env([tc(("read_file", {"path": self.target})), {"content": "利润 1234 万"}], mode="auto")
        sid = e.start("看看那份报告")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        req = e.events(sid, ("approval_requested",))[0]["payload"]
        self.assertEqual(req["risk"], "read_outside")
        e.rt.decide(req["action_id"], True)
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertIn("1234", e.results(sid)[0]["content"])
        self.assertIn("批准方式：自动", e.mock.calls[0][0]["content"])

    def test_auto_denied(self):
        e = self.env([tc(("read_file", {"path": self.target})), {"content": "好的，不读了"}], mode="auto")
        sid = e.start("看看那份报告")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        e.rt.decide(e.rt.store.actions(sid, states=("awaiting",))[0]["id"], False)
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertNotIn("1234", json.dumps(e.results(sid), ensure_ascii=False))

    def test_inside_still_free_in_auto(self):
        e = self.env([tc(("read_file", {"path": "a.txt"})), {"content": "好"}], mode="auto")
        e.write("a.txt", "里面的")
        sid = e.start("读")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.rt.store.actions(sid)[0]["risk"], "read")
        self.assertIn("里面的", e.results(sid)[0]["content"])

    # ---- 不经过批准就不读（例如直接调处理函数） ----
    def test_handler_without_approval_refuses(self):
        from agent.kb import resolve_read, ReadPathError
        e = self.env([], mode="full")
        ctx = SimpleNamespace(root=e.root, store=e.rt.store, cfg=e.cfg, action_id=None, is_sub=False)
        with self.assertRaises(ReadPathError) as cm:
            resolve_read(ctx, self.target)
        self.assertIn("全放行", str(cm.exception))

    def test_sub_agent_only_in_full(self):
        from agent.kb import resolve_read, ReadPathError
        for mode, ok in (("auto", False), ("full", True)):
            e = self.env([], mode=mode)
            sid = e.new_session()
            a = e.rt.store.add_action(sid, 0, "c1", "read_file", {"path": self.target}, "read_outside")
            e.rt.store.set_action(a["id"], "running")
            ctx = SimpleNamespace(root=e.root, store=e.rt.store, cfg=e.cfg, action_id=a["id"], is_sub=True)
            if ok:
                self.assertEqual(resolve_read(ctx, self.target), os.path.realpath(self.target))
            else:
                with self.assertRaises(ReadPathError):
                    resolve_read(ctx, self.target)


class SystemInfo(Case):
    def test_collect_here(self):
        info = systool.collect()
        for k in ("系统", "架构", "核心", "磁盘", "主目录", "本程序用的 Python"):
            self.assertIn(k, info)
        txt = json.dumps(info, ensure_ascii=False)
        self.assertNotIn("serial", txt.lower())

    def test_tool_runs_in_loop(self):
        e = self.env([tc(("system_info", {})), {"content": "配置如上"}], mode="manual")
        sid = e.start("我电脑什么配置")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))       # 每步都问：也问
        e.rt.decide(e.rt.store.actions(sid, states=("awaiting",))[0]["id"], True)
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertIn("系统", e.results(sid)[0]["content"])
        e2 = self.env([tc(("system_info", {})), {"content": "好"}], mode="auto")
        sid2 = e2.start("配置")
        self.assertTrue(e2.rt.wait_idle(sid2, 5))
        self.assertEqual(e2.rt.store.actions(sid2)[0]["risk"], "read")

    def test_mac_profiler_parse(self):
        data = {"SPHardwareDataType": [{"_name": "hardware_overview", "chip_type": "Apple M2 Pro", "machine_model": "Mac14,10",
                                        "machine_name": "MacBook Pro", "number_processors": "proc 12:8:4",
                                        "physical_memory": "16 GB", "serial_number": "C02XXXXX", "platform_UUID": "ABCD"}],
                "SPDisplaysDataType": [{"_name": "Apple M2 Pro", "sppci_cores": "19", "sppci_model": "Apple M2 Pro",
                                        "spdisplays_ndrvs": [{"_name": "Color LCD", "_spdisplays_pixels": "3456 x 2234",
                                                              "_spdisplays_resolution": "1728 x 1117 @ 120.00Hz"}]}]}
        out = systool.mac_from_profiler(data)
        self.assertEqual(out["型号"], "MacBook Pro（Mac14,10）")
        self.assertEqual(out["芯片"], "Apple M2 Pro")
        self.assertEqual(out["核心"], "12 核（8 个性能核、4 个能效核）")
        self.assertEqual(out["内存"], "16 GB")
        self.assertEqual(out["显卡"], "Apple M2 Pro（19 核）")
        self.assertIn("内置屏幕：1728 x 1117，120.00Hz", out["屏幕"])
        self.assertNotIn("C02XXXXX", json.dumps(out, ensure_ascii=False))
        intel = {"SPHardwareDataType": [{"machine_name": "MacBook Pro", "cpu_type": "Quad-Core Intel Core i7",
                                         "current_processor_speed": "2.3 GHz", "number_processors": 4, "physical_memory": "32 GB"}],
                 "SPDisplaysDataType": [{"sppci_model": "Intel Iris Plus Graphics", "spdisplays_vram": "1536 MB"}]}
        out = systool.mac_from_profiler(intel)
        self.assertEqual(out["芯片"], "Quad-Core Intel Core i7 2.3 GHz")
        self.assertEqual(out["核心"], "4 核")
        self.assertEqual(out["显卡"], "Intel Iris Plus Graphics（显存 1536 MB）")

    def test_windows_parse(self):
        d = {"maker": "LENOVO", "model": "21HD", "mem": 34359738368, "cpu": ["13th Gen Intel(R) Core(TM) i7-1365U"], "cores": 10,
             "gpu": [{"name": "Intel(R) Iris(R) Xe Graphics", "ram": 2147483648, "res": "2560 x 1600"}],
             "os": "Microsoft Windows 11 专业版", "build": "22631", "free": 16000000}
        out = systool.win_from_ps(d)
        self.assertEqual(out["系统"], "Microsoft Windows 11 专业版（版本 22631）")
        self.assertEqual(out["型号"], "LENOVO 21HD")
        self.assertEqual(out["内存"], "32.0 GB")
        self.assertEqual(out["显卡"], "Intel(R) Iris(R) Xe Graphics（显存 2.0 GB）")
        self.assertEqual(out["屏幕"], "2560 x 1600")
        out = systool.win_from_ps({"cpu": "单个处理器", "gpu": {"name": "某显卡", "ram": 0, "res": " x "}})
        self.assertEqual(out["处理器"], "单个处理器")
        self.assertEqual(out["显卡"], "某显卡")
        self.assertNotIn("屏幕", out)

    def test_prompt_names_this_computer(self):
        from agent import context
        e = self.env([])
        text = "\n".join(context.env_lines(e.rt))
        self.assertIn(os.path.expanduser("~"), text)
        self.assertIn("演示用的假模型", text)            # 测试用的是假模型
        if sys.platform == "darwin":
            self.assertIn("macOS", text)
        elif sys.platform.startswith("linux"):
            self.assertIn("Linux", text)
        self.assertNotIn("你背后的模型", "\n".join(context.env_lines(e.rt, sub=True)))


class ModelIdentity(unittest.TestCase):
    """问"你是什么模型"：系统提示里写明背后的模型，免得本地模型自称 Claude。"""

    def rt(self, eps, first=0):
        caps = SimpleNamespace(mock=lambda: False, endpoints=lambda: eps, candidates=lambda **kw: ([first], ""))
        return SimpleNamespace(caps=caps, cfg=SimpleNamespace(get=lambda *k, **kw: None))

    def test_family(self):
        from agent.context import model_family
        self.assertEqual(model_family("nvidia/Qwen3.8-Flash-Next-FP4-Local"), "通义千问系列，阿里巴巴开发；这是 NVIDIA 发布的版本")
        self.assertEqual(model_family("Qwen/Qwen3-32B"), "通义千问系列，阿里巴巴开发")
        self.assertEqual(model_family("zai-org/GLM-4.6"), "GLM 系列，智谱开发")
        self.assertEqual(model_family("openai/gpt-oss-120b"), "gpt-oss，OpenAI 开源的模型")
        self.assertEqual(model_family("my-own-model"), "")

    def test_line(self):
        from agent.context import model_line
        ep = {"name": "GX10-A", "base_url": "http://10.0.0.11:8000/v1/", "model": "nvidia/Qwen3.8-Flash-Next-FP4-Local"}
        line = model_line(self.rt([ep]))
        self.assertIn("nvidia/Qwen3.8-Flash-Next-FP4-Local（通义千问系列，阿里巴巴开发", line)
        self.assertIn("「GX10-A」（http://10.0.0.11:8000/v1）", line)
        self.assertIn("不要说自己是 Claude", line)
        self.assertNotIn("换成", line)
        two = model_line(self.rt([ep, {"name": "看图", "base_url": "http://x/v1", "model": "glm-4.5v"}], first=1))
        self.assertIn("glm-4.5v（GLM 系列，智谱开发）", two)
        self.assertIn("换成设置里的其他模型", two)
        self.assertEqual(model_line(self.rt([])), "")


if __name__ == "__main__":
    unittest.main()
