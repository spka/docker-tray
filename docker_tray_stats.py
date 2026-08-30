"""Pure parsing, formatting, and health calculations for container stats."""

import datetime
import re


def parse_cpu_pct(value):
    try:
        return float(value.strip().rstrip("%"))
    except Exception:
        return 0.0


def parse_mem_bytes(value):
    match = re.match(r"([\d.]+)\s*(B|kB|KiB|MB|MiB|GB|GiB)", value.strip())
    if not match:
        return 0
    multipliers = {
        "B": 1,
        "kB": 1000,
        "KiB": 1024,
        "MB": 1_000_000,
        "MiB": 1024**2,
        "GB": 1_000_000_000,
        "GiB": 1024**3,
    }
    return int(float(match.group(1)) * multipliers[match.group(2)])


def format_bytes(value):
    for unit, threshold in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if value >= threshold:
            return f"{value / threshold:.1f} {unit}"
    return f"{value} B"


def format_uptime(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_ts(timestamp):
    value = datetime.datetime.fromtimestamp(timestamp)
    if value.date() == datetime.date.today():
        return value.strftime("%H:%M")
    return value.strftime("%d %b %H:%M")


def recent_cpu_streak_counts(history, current_samples, warning_pct, critical_pct):
    samples_by_name = {}
    for sample in (*history, *current_samples):
        try:
            name = sample["name"]
            timestamp = float(sample["t"])
            cpu = float(sample["cpu"])
        except (KeyError, TypeError, ValueError):
            continue
        samples_by_name.setdefault(name, {})[timestamp] = cpu

    warning_counts = {}
    critical_counts = {}
    for name, samples_by_time in samples_by_name.items():
        warning_count = 0
        critical_count = 0
        warning_streak_active = True
        critical_streak_active = True
        for _timestamp, cpu in sorted(samples_by_time.items(), reverse=True):
            if warning_streak_active and cpu >= warning_pct:
                warning_count += 1
            else:
                warning_streak_active = False
            if critical_streak_active and cpu >= critical_pct:
                critical_count += 1
            else:
                critical_streak_active = False
            if not warning_streak_active and not critical_streak_active:
                break
        warning_counts[name] = warning_count
        critical_counts[name] = critical_count
    return warning_counts, critical_counts


def compute_health(summary, system_mem_total, sustained_samples, format_memory=format_bytes):
    issues = []
    level = "ok"
    total_cpu = sum(sample["cpu"] for sample in summary)
    total_mem = sum(sample["mem"] for sample in summary)
    mem_pct = (total_mem / system_mem_total * 100) if system_mem_total else 0

    for sample in summary:
        if sample.get("recent_cpu_critical_count", 0) >= sustained_samples:
            level = "critical"
            issues.append(f"⚠ {sample['name']}: sustained CPU {sample['cpu']:.1f}%")
        elif sample.get("recent_cpu_warning_count", 0) >= sustained_samples and level == "ok":
            level = "warning"
            issues.append(f"↑ {sample['name']}: sustained CPU {sample['cpu']:.1f}%")

    if mem_pct >= 85:
        level = "critical"
        issues.append(
            f"⚠ RAM: {format_memory(total_mem)} / "
            f"{format_memory(system_mem_total)} ({mem_pct:.0f}%)"
        )
    elif mem_pct >= 70 and level == "ok":
        level = "warning"
        issues.append(
            f"↑ RAM: {format_memory(total_mem)} / "
            f"{format_memory(system_mem_total)} ({mem_pct:.0f}%)"
        )

    return level, total_cpu, total_mem, system_mem_total, mem_pct, issues
