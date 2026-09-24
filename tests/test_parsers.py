import pytest

from oncall.parsers import (
    cpu_percentages,
    parse_cpu_quota,
    parse_cpu_stat,
    parse_process_stat,
    parse_self_cgroup,
)


def stat(values):
    return f"cpu {values}\ncpu0 1 2 3\ncpu1 1 2 3\n"


def test_guest_ticks_are_not_double_counted():
    first = parse_cpu_stat(stat("10 0 10 80 0 0 0 0 5 0"))
    second = parse_cpu_stat(stat("30 0 20 140 5 0 0 5 15 0"))
    assert second.total - first.total == 100
    assert cpu_percentages(first, second) == (30, 5, 5)
    assert first.logical_cpus == 2


def test_reset_and_empty_samples_fail():
    value = parse_cpu_stat(stat("1 0 1 8 0 0 0 0"))
    with pytest.raises(ValueError):
        cpu_percentages(value, value)
    with pytest.raises(ValueError):
        parse_cpu_stat("cpu 1 2")


def test_process_comm_may_contain_spaces_and_parentheses():
    fields = ["S"] + ["0"] * 21
    fields[11], fields[12], fields[19], fields[21] = "30", "10", "200", "12"
    parsed = parse_process_stat("42 (worker (busy)) " + " ".join(fields))
    assert (parsed.pid, parsed.name, parsed.ticks, parsed.start_ticks, parsed.rss_pages) == (
        42,
        "worker (busy)",
        40,
        200,
        12,
    )


def test_quota_is_not_logical_cpu_count():
    assert parse_cpu_quota("50000 100000") == 0.5
    assert parse_cpu_quota("max 100000") is None
    with pytest.raises(ValueError):
        parse_cpu_quota("100 0")


def test_unified_self_cgroup_is_parsed_without_guessing_systemd_paths():
    assert parse_self_cgroup("0::/system.slice/oncall-target.service\n") == (
        "/system.slice/oncall-target.service"
    )
    with pytest.raises(ValueError):
        parse_self_cgroup("2:cpu:/legacy\n")
