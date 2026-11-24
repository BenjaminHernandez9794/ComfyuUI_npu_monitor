import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("npu_collector", os.path.join(HERE, "..", "npu_collector.py"))
npu_collector = importlib.util.module_from_spec(spec)
sys.modules["npu_collector"] = npu_collector
spec.loader.exec_module(npu_collector)
parse = npu_collector.parse_npu_smi_info

SAMPLE_910B = """
+------------------------------------------------------------------------------------------------+
| npu-smi 23.0.rc3                 Version: 23.0.rc3                                             |
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip                      | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     910B3               | OK            | 93.4        38                0    / 0             |
| 0                         | 0000:C1:00.0  | 27          0    / 0          33780/ 65536         |
+===========================+===============+====================================================+
| 1     910B3               | OK            | 88.1        41                0    / 0             |
| 0                         | 0000:C2:00.0  | 0           0    / 0          3378 / 65536         |
+===========================+===============+====================================================+
+---------------------------+---------------+----------------------------------------------------+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| 0       0                 | 12345         | python                   | 30000                   |
+===========================+===============+====================================================+
"""

SAMPLE_310P_DUO = """
+--------------------------------------------------------------------------------------------------------+
| npu-smi 23.0.0                                   Version: 23.0.0                                       |
+-------------------------------+-----------------+------------------------------------------------------+
| NPU     Name                  | Health          | Power(W)     Temp(C)           Hugepages-Usage(page) |
| Chip    Device                | Bus-Id          | AICore(%)    Memory-Usage(MB)                        |
+===============================+=================+======================================================+
| 1       310P3                 | OK              | NA           48                0     / 0             |
| 0       0                     | 0000:3B:00.0    | 12           1621 / 44280                            |
| 1       1                     | 0000:3C:00.0    | 0            1302 / 43693                            |
+===============================+=================+======================================================+
"""


def test_910b():
    d = parse(SAMPLE_910B)
    assert len(d) == 2
    a = d[0]
    assert a["npu_id"] == 0 and a["name"] == "910B3"
    assert a["power_w"] == 93.4 and a["temp_c"] == 38
    assert a["aicore_pct"] == 27
    assert a["mem_used_mb"] == 33780 and a["mem_total_mb"] == 65536 and a["mem_kind"] == "HBM"
    assert d[1]["npu_id"] == 1 and d[1]["temp_c"] == 41


def test_310p_duo():
    d = parse(SAMPLE_310P_DUO)
    assert len(d) == 2
    assert d[0]["power_w"] is None and d[0]["temp_c"] == 48
    assert d[0]["aicore_pct"] == 12 and d[0]["mem_total_mb"] == 44280 and d[0]["mem_kind"] == "DDR"
    assert d[1]["chip_id"] == 1 and d[1]["npu_id"] == 1 and d[1]["mem_used_mb"] == 1302


if __name__ == "__main__":
    test_910b()
    test_310p_duo()
    print("ok")
