"""Summarize Mapbox traffic annotations without inventing missing measurements."""
import math
import re


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def road_key(value):
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"треть(?:е|его|ем|ему)\s+(?:транспортн\w*\s+)?кольц\w*|3[ -]*(?:е|его|ем|го)?\s*кольц\w*", "ттк", text)
    return re.sub(r"[^\w]+", " ", text).strip()


def _worst(levels):
    return next((s for s in ("severe", "heavy", "moderate", "low") if s in levels), "unknown")


def summarize(route, road=None):
    totals = {k: 0.0 for k in ("low", "moderate", "heavy", "severe", "unknown")}
    segments, incidents, closures = [], [], 0
    for leg in route.get("legs") or []:
        ann = leg.get("annotation") or {}
        levels, distances = ann.get("congestion") or [], ann.get("distance") or []
        for i, meters in enumerate(distances):
            if not _number(meters):
                continue
            level = levels[i] if i < len(levels) else "unknown"
            totals[level if level in totals else "unknown"] += meters

        steps = leg.get("steps") or []
        # Step geometries share their boundary vertex. Only associate annotations
        # with road names when every step has geometry and the lengths agree.
        counts = []
        for step in steps:
            geom = step.get("geometry") or {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            counts.append(max(0, len(coords) - 1) if isinstance(coords, list) and coords else None)
        aligned = bool(steps and all(n is not None for n in counts) and
                       sum(counts) == len(distances) == len(levels))
        cursor = 0
        for step, count in zip(steps, counts):
            distance, duration, typical = step.get("distance"), step.get("duration"), step.get("duration_typical")
            if not (_number(distance) and _number(duration)) or distance == 0:
                if aligned:
                    cursor += count
                continue
            step_road = str(step.get("name") or step.get("ref") or "Неназванный участок")[:180]
            step_levels = levels[cursor:cursor + count] if aligned else []
            step_distances = distances[cursor:cursor + count] if aligned else []
            known_step_m = sum(m for level, m in zip(step_levels, step_distances)
                               if level in ("low", "moderate", "heavy", "severe") and _number(m))
            if aligned:
                cursor += count
            item = {"road": step_road, "ref": str(step.get("ref") or "")[:100],
                    "distance_meters": distance, "duration_seconds": duration,
                    "known_distance_meters": min(distance, known_step_m),
                    "typical_seconds": typical if _number(typical) else None,
                    "congestion": _worst(step_levels)}
            if segments and segments[-1]["road"] == step_road and segments[-1]["ref"] == item["ref"]:
                prev = segments[-1]
                prev["distance_meters"] += distance
                prev["duration_seconds"] += duration
                prev["known_distance_meters"] += item["known_distance_meters"]
                prev["typical_seconds"] = (prev["typical_seconds"] + item["typical_seconds"]
                                            if prev["typical_seconds"] is not None and item["typical_seconds"] is not None else None)
                prev["congestion"] = _worst([prev["congestion"], item["congestion"]])
            else:
                segments.append(item)
        closures += len(leg.get("closures") or [])
        for event in (leg.get("incidents") or [])[:20]:
            incidents.append({"type": str(event.get("type") or "unknown")[:80],
                              "description": str(event.get("description") or "")[:300],
                              "road_closed": event.get("closed") is True})

    distance = route.get("distance") if _number(route.get("distance")) else 0
    known = sum(totals[k] for k in ("low", "moderate", "heavy", "severe"))
    unknown = max(totals["unknown"], distance - known)
    congested = totals["heavy"] + totals["severe"]
    status = ("congestion_reported" if congested else "slowdowns_reported" if totals["moderate"] else
              "no_congestion_reported" if known and unknown <= 1 else
              "partial_data" if known else "unknown")
    for segment in segments:
        meters = segment.pop("distance_meters")
        segment["distance_km"] = round(meters / 1000, 2)
        segment["unknown_distance_km"] = round(max(0, meters - segment.pop("known_distance_meters")) / 1000, 2)
        segment["minutes"] = math.ceil(segment["duration_seconds"] / 60)
        baseline = segment.pop("typical_seconds")
        segment["delay_vs_typical_seconds"] = (round(segment["duration_seconds"] - baseline) if baseline is not None else None)
    bottlenecks = [s for s in segments if s["congestion"] in ("moderate", "heavy", "severe") or
                   (s["delay_vs_typical_seconds"] is not None and s["delay_vs_typical_seconds"] >= 60)]
    bottlenecks.sort(key=lambda s: ((s["congestion"] == "severe"), (s["congestion"] == "heavy"),
                                  s["delay_vs_typical_seconds"] or 0), reverse=True)
    duration, typical = route.get("duration"), route.get("duration_typical")
    result = {"status": status, "known_distance_km": round(known / 1000, 2),
            "unknown_distance_km": round(unknown / 1000, 2), "congested_distance_km": round(congested / 1000, 2),
            "slow_distance_km": round(totals["moderate"] / 1000, 2),
            "delay_vs_typical_seconds": round(duration - typical) if _number(duration) and _number(typical) else None,
            "segments": segments[:40], "segments_truncated": len(segments) > 40, "bottlenecks": bottlenecks[:5],
            "incidents": incidents[:10], "closures_count": closures,
            "note": "Разница времени сравнивается с типичной дорожной обстановкой, а не с пустыми дорогами. "
                    "Неизвестные участки не считаются свободными. Сводка относится только к проверенному маршруту."}
    if road:
        result["focus"] = focus({"segments": segments}, road)
    return result


def focus(summary, road):
    key = road_key(road)
    matching = [s for s in summary["segments"] if key and key in road_key(s["road"] + " " + s["ref"])]
    return {"road": str(road)[:150], "found": bool(matching), "segments": matching,
            "note": "Это только участки выбранного маршрута; состояние всего кольца или района не проверялось."}
