import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.models import init_model_manager, get_current_model_config
from utils import paths
import utils.memory as memory


CASES = [
    {
        "name": "release_patch",
        "query": "用户要求打包发布，并说最小版本号+1，全部发布流程都要做。",
        "expected_any": ["mem_20260510_043132_4b4a292b"],
    },
    {
        "name": "test_script_location",
        "query": "我需要创建一个最小测试脚本来验证当前功能。",
        "expected_any": ["mem_20260517_213150_20b1506c"],
    },
    {
        "name": "readme_sync",
        "query": "请更新 README 中关于新机制的说明，并同步英文文档。",
        "expected_any": [
            "mem_20260510_044142_e4820028",
            "mem_20260516_004615_3fbc0c29",
        ],
    },
    {
        "name": "model_management",
        "query": "我要修改 system/models.py 里的模型删除和默认选择逻辑。",
        "expected_any": [
            "mem_20260512_155406_2493aef3",
            "mem_20260516_004617_1658c20d",
            "mem_20260516_004617_ae32126c",
        ],
    },
]


class LiveMemoryRecallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_model_manager(paths.install_makecode_dir())
        model = get_current_model_config()
        if model is None:
            raise unittest.SkipTest("No model configured for live memory recall test")
        print(f"live model: {model.model_id} ({model.base_url})")
        print(f"active memories: {len(memory.list_long_term_memories())}")

    def test_live_memory_recall_accuracy(self):
        failures = []
        for case in CASES:
            with self.subTest(case=case["name"]):
                selected_ids = memory.select_relevant_memory_ids(
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
