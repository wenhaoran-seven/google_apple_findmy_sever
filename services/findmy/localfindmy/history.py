"""Accumulate decrypted reports without expiring previously fetched positions."""
import json


NOTE = "累计保存本服务已获取的位置，重复报告去重，空结果不清空历史。坐标为 WGS84；苹果未返回或旧版已覆盖的报告无法凭空恢复。status 按上游原始值显示。"


def identity(point):
    # Timestamp alone is not unique: Apple can return multiple reports in a second.
    return json.dumps(point, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize(saved, timestamp):
    if saved.get("history_version") == 1:
        return saved
    unique = {identity(p): p for p in saved["reports"]}
    return {**saved, "history_version": 1,
            "reports": sorted(unique.values(), key=lambda p: p["timestamp"], reverse=True),
            "count": len(unique), "last_fetch_count": saved["count"], "new_count": 0,
            "empty_count": 0, "consecutive_empty_count": 0, "last_empty_at": None,
            "stats_since": timestamp, "note": NOTE}


def accumulate(saved, device_id, name, points, timestamp):
    previous = normalize(saved, timestamp) if saved else {
        "reports": [], "empty_count": 0, "consecutive_empty_count": 0,
        "last_empty_at": None, "stats_since": timestamp}
    unique = {identity(p): p for p in previous["reports"]}
    old_count = len(unique)
    unique.update((identity(p), p) for p in points)
    empty = not points
    return {"history_version": 1, "device_id": device_id, "device": name,
            "reports": sorted(unique.values(), key=lambda p: p["timestamp"], reverse=True),
            "count": len(unique), "last_fetch_count": len(points),
            "new_count": len(unique) - old_count, "fetched_at": timestamp,
            "empty_count": previous["empty_count"] + int(empty),
            "consecutive_empty_count": previous["consecutive_empty_count"] + 1 if empty else 0,
            "last_empty_at": timestamp if empty else previous["last_empty_at"],
            "stats_since": previous["stats_since"], "note": NOTE}
