import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.models import init_model_manager, get_current_model_config
from utils import paths
import utils.memory as memory


CASES = [
    {
        "name": "terminal_commands",
        "query": "请给我几条需要手动运行的终端命令，我要复制执行。",
        "expected_any": ["mem_20260713_174250_ba4ff366"],
    },
    {
        "name": "macos_release",
        "query": "发布 macOS ARM64，确认发布包结构和启动方式。",
        "expected_any": [
            "mem_20260713_185834_892e13e3",
            "mem_20260714_113912_f9ca8f4e",
        ],
    },
    {
        "name": "skills_location",
        "query": "修改 MakeCode Skills 的扫描路径、优先级和打包规则。",
        "expected_any": [
            "mem_20260714_113912_772d0fef",
            "mem_20260714_113912_f9ca8f4e",
        ],
    },
    {
        "name": "model_management",
        "query": "我要修改 system/models.py 里的模型删除和默认选择逻辑。",
        "expected_any": [
            "mem_20260714_141028_2748b97f",
            "mem_20260714_141028_52f4dd18",
            "mem_20260714_141028_7ded8278",
            "mem_20260726_161629_8f01f994",
        ],
    },
]


class LiveMemoryRecallTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_model_manager(paths.install_makecode_dir())
        model = get_current_model_config()
        if model is None:
            raise unittest.SkipTest("No model configured for live memory recall test")
        print(f"live model: {model.model_id} ({model.base_url})")
        print(f"active memories: {len(memory.list_long_term_memories())}")

    async def test_live_memory_recall_accuracy(self):
        failures = []
        for case in CASES:
            with self.subTest(case=case["name"]):
                selected_ids = await memory.select_relevant_memory_ids(
                    case["query"],
                    agent_id=f"live_memory_recall_test:{case['name']}",
                )
                print(f"\n[{case['name']}] query={case['query']}")
                print(f"selected_ids={selected_ids}")
                if not any(memory_id in selected_ids for memory_id in case["expected_any"]):
                    failures.append({
                        "case": case["name"],
                        "expected_any": case["expected_any"],
                        "selected_ids": selected_ids,
                    })
        if failures:
            self.fail(f"Live memory recall misses: {failures}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
