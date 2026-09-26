from __future__ import annotations

import unittest

from core.action_batch import partition_tool_batch


class ToolBatchTests(unittest.TestCase):
    def test_adjacent_read_only_calls_share_a_group_and_writes_keep_order(self) -> None:
        calls = ["cpu", "downloads", "open_chrome", "windows", "gpu", "move_chrome"]
        read_only = {"cpu", "downloads", "windows", "gpu"}

        groups = partition_tool_batch(calls, lambda call: call in read_only)

        self.assertEqual(
            groups,
            [
                (True, ["cpu", "downloads"]),
                (False, ["open_chrome"]),
                (True, ["windows", "gpu"]),
                (False, ["move_chrome"]),
            ],
        )

    def test_empty_and_write_only_batches_do_not_create_parallel_groups(self) -> None:
        self.assertEqual(partition_tool_batch([], lambda _call: True), [])
        self.assertEqual(
            partition_tool_batch(["open", "move"], lambda _call: False),
            [(False, ["open"]), (False, ["move"])],
        )


if __name__ == "__main__":
    unittest.main()
