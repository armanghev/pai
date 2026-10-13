from pai.tools.system import format_health, health, meminfo

MEMINFO = """MemTotal:        4028180 kB
MemFree:          212340 kB
MemAvailable:    3011220 kB
"""


def test_meminfo_parse():
    assert meminfo(MEMINFO) == {"total_mb": 3933, "available_mb": 2940}


def test_health_runs_anywhere(tmp_path):
    h = health(tmp_path)
    assert 0 <= h["disk_used_pct"] <= 100
    assert h["cpus"] >= 1
    text = format_health(h)
    assert "Disk:" in text and "CPU temp:" in text
