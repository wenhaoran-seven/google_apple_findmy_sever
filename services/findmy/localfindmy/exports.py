"""Export one summary row per device from locally saved positions."""
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, Field, model_validator


BEIJING = timezone(timedelta(hours=8))
FIELDS = ["设备名称", "广播间隔", "发射功率", "起始时间", "结束时间", "位置数量"]

# These are the user's measured/configured radio settings, not values inferred
# from Find My location reports. Unknown SNs are left blank in the export.
RADIO_GROUPS = {
    (1, 4): "677823142348 676610285935 673809385937 670275852003 "
            "673913026022 673913275332",
    (2, 4): "676732803015 679007580352 678937033000 677131719461 "
            "679354086768 677059735467 660220565138 668941718641 "
            "676130523880 662811275333 669960210994 674568927904 "
            "677493195872 679510945426 679314109033 678863049689 "
            "676517785980 677674121788 678662077891 671968215103 "
            "671964815344 670373358108 671439582455 670497696262 "
            "672023803010 673809683187 671442968549 672122953061 "
            "670373460811 671967998873",
    (1, 10): "676685912867 676470715384",
    (2, 10): "679993640938 677873329571",
}
RADIO_BY_SN = {sn: settings for settings, sns in RADIO_GROUPS.items() for sn in sns.split()}


def radio_settings(item):
    sn = str(item.get("sn") or "")
    if sn in RADIO_BY_SN:
        return RADIO_BY_SN[sn]
    # Older devices were named with their SN before the dedicated SN field existed.
    matches = set(re.findall(r"(?<!\d)\d{12}(?!\d)", item.get("name", "")))
    if len(matches) == 1:
        return RADIO_BY_SN.get(matches.pop(), (None, None))
    return None, None


def spreadsheet_safe(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or
                                   value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def beijing_text(value):
    return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


class ExportInput(BaseModel):
    device_ids: list[str] = Field(min_length=1, max_length=1000)
    start: AwareDatetime
    end: AwareDatetime
    format: Literal["csv", "json"] = "csv"

    @model_validator(mode="after")
    def valid_range(self):
        if self.end < self.start:
            raise ValueError("end precedes start")
        self.device_ids = list(dict.fromkeys(self.device_ids))
        return self


def export_locations(service, body):
    rows = []
    start, end = beijing_text(body.start), beijing_text(body.end)
    for device_id in body.device_ids:
        item, _ = service.device(device_id)
        count = sum(body.start <= datetime.fromisoformat(point["timestamp"]) <= body.end
                    for point in service.location_cache.get(device_id, {}).get("reports", []))
        interval, power = radio_settings(item)
        rows.append({"设备名称": item["name"],
                     "广播间隔": f"{interval} 秒" if interval is not None else "",
                     "发射功率": f"{power} dBm" if power is not None else "",
                     "起始时间": start, "结束时间": end, "位置数量": count})
    total = sum(row["位置数量"] for row in rows)
    if body.format == "json":
        content = json.dumps({"timezone": "Asia/Shanghai", "bounds": "inclusive",
                              "count": total, "devices": rows},
                             ensure_ascii=False, indent=2).encode("utf-8")
        media_type = "application/json"
    else:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            # Device names are arbitrary user input; prevent spreadsheet formulas.
            writer.writerow({k: spreadsheet_safe(v) for k, v in row.items()})
        content = stream.getvalue().encode("utf-8-sig")
        media_type = "text/csv"
    filename = f"findmy-summary-{datetime.now(BEIJING):%Y%m%d-%H%M%S}.{body.format}"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Report-Count": str(total), "X-Device-Count": str(len(rows))})
