import unittest
from pathlib import Path
from unittest import mock

import pid_pressure


class PidPressureTests(unittest.TestCase):
    def test_unreadable_cgroup_sheds_closed(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            current, maximum, threshold = pid_pressure.pid_pressure()
            self.assertEqual((current, maximum), (threshold, threshold))
            self.assertTrue(pid_pressure.should_shed())

    def test_malformed_cgroup_sheds_closed(self) -> None:
        with mock.patch.object(Path, "read_text", return_value="not-a-number"):
            self.assertTrue(pid_pressure.should_shed())

    def test_small_cgroup_limit_keeps_headroom(self) -> None:
        self.assertEqual(pid_pressure.effective_shed_threshold(500, 750), 375)
        self.assertEqual(pid_pressure.effective_shed_threshold(1000, 750), 750)
        with mock.patch.object(pid_pressure, "pid_pressure", return_value=(380, 500, 750)):
            self.assertTrue(pid_pressure.should_shed())
        with mock.patch.object(pid_pressure, "pid_pressure", return_value=(374, 500, 750)):
            self.assertFalse(pid_pressure.should_shed())


if __name__ == "__main__":
    unittest.main()
