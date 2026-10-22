# Live Tests

此目录存放需要真实外部 API 的验证用例和手工脚本，默认不参与 `pytest` 和 CI。

- `test_memory_recall_live.py`：需要真实模型配置和当前 active 记忆库。
- `check_sub_agent_stream_tuple.py`：手工脚本，需要 `MAKECODE_TEST_BASE_URL`、`MAKECODE_TEST_API_KEY`、`MAKECODE_TEST_MODEL`。

手动运行示例：

```bash
python -m pytest live_tests/test_memory_recall_live.py -q
python live_tests/check_sub_agent_stream_tuple.py --help
```
