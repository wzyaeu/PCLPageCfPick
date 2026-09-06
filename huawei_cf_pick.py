import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request

from huaweicloudsdkcore.auth.credentials import BasicCredentials, GlobalCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkiam.v3 import IamClient, KeystoneListProjectsRequest
from huaweicloudsdkiam.v3.region.iam_region import IamRegion
from huaweicloudsdkdns.v2 import (
    DnsClient,
    ListPublicZonesRequest,
    ListRecordSetsWithLineRequest,
    UpdateRecordSetsRequest,
    UpdateRecordSetsReq,
)
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion

ZONE_NAME = "kaphia.top"
RECORD_NAME = "cf-pick.kaphia.top"

DEFAULT_CF_URL = "https://www.wetest.vip/page/cloudflare/address_v4.html"
CN_LINE = {"电信": "Dianxin", "联通": "Liantong", "移动": "Yidong"}
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Connection": "close",
    "Upgrade-Insecure-Requests": "1",
}


def get_user_env(name):
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            v, _ = winreg.QueryValueEx(key, name)
            return v if v else ""
    except Exception:
        return ""

def get_ak_sk():
    ak = os.environ.get("HUAWEICLOUD_SDK_AK", "").strip()
    sk = os.environ.get("HUAWEICLOUD_SDK_SK", "").strip()
    if not ak:
        ak = get_user_env("HUAWEICLOUD_SDK_AK").strip()
    if not sk:
        sk = get_user_env("HUAWEICLOUD_SDK_SK").strip()
    if not ak or not sk:
        sys.exit("错误: 未找到华为云凭据。")
    return ak, sk

def build_dns_client(ak, sk, project_id, region_id):
    cred = BasicCredentials(ak, sk).with_project_id(project_id)
    builder = DnsClient.new_builder().with_credentials(cred)
    try:
        region = DnsRegion.value_of(region_id)
        if region is not None:
            builder.with_region(region)
        else:
            builder.with_endpoint("https://dns.%s.myhuaweicloud.com" % region_id)
    except Exception:
        builder.with_endpoint("https://dns.%s.myhuaweicloud.com" % region_id)
    return builder.build()


def list_projects(ak, sk):
    cred = GlobalCredentials(ak, sk)
    client = (
        IamClient.new_builder()
        .with_credentials(cred)
        .with_region(IamRegion.CN_NORTH_1)
        .build()
    )
    resp = client.keystone_list_projects(KeystoneListProjectsRequest())
    out = []
    for p in (resp.projects or []):
        rid = (p.name or "").strip()
        if rid and p.id and p.enabled is not False:
            out.append((rid, p.id))
    return out

def find_zone_in_region(client):
    req = ListPublicZonesRequest(
        type="public", name=ZONE_NAME, search_mode="equal", limit=100
    )
    resp = client.list_public_zones(req)
    for z in (resp.zones or []):
        if (getattr(z, "name", "") or "").rstrip(".").lower() == ZONE_NAME.lower():
            return z
    marker = None
    while True:
        r2 = ListPublicZonesRequest(type="public", limit=100)
        if marker:
            r2.marker = marker
        resp2 = client.list_public_zones(r2)
        zones = resp2.zones or []
        if not zones:
            break
        for z in zones:
            if (getattr(z, "name", "") or "").rstrip(".").lower() == ZONE_NAME.lower():
                return z
        marker = zones[-1].id
    return None

def locate_zone(ak, sk, only_region=None):
    projects = list_projects(ak, sk)
    if not projects:
        projects = [
            ("ap-southeast-1", ""), ("cn-north-4", ""), ("cn-north-1", ""),
            ("cn-south-1", ""), ("cn-east-2", ""), ("cn-east-3", ""),
            ("cn-north-9", ""), ("cn-southwest-2", ""),
        ]
    if only_region:
        projects = [p for p in projects if p[0] == only_region]
    for region_id, project_id in projects:
        if not project_id:
            continue
        try:
            client = build_dns_client(ak, sk, project_id, region_id)
            zone = find_zone_in_region(client)
            if zone is not None:
                return client, zone, region_id
        except exceptions.ClientRequestException as e:
            print("[跳过] 区域 %s 查询失败: HTTP %s %s" % (region_id, e.status_code, e.error_msg))
        except Exception as e:
            print("[跳过] 区域 %s 查询异常: %s" % (region_id, e))
    return None, None, None


def list_a_records_with_line(client, zone, name=RECORD_NAME):
    req = ListRecordSetsWithLineRequest(zone_id=zone.id, name=name, type="A", limit=100)
    resp = client.list_record_sets_with_line(req)
    return resp.recordsets or []


def dump_line_record(r):
    return {
        "id": r.id,
        "name": r.name,
        "type": r.type,
        "line": getattr(r, "line", None),
        "records": r.records or [],
        "ttl": getattr(r, "ttl", None),
        "weight": getattr(r, "weight", None),
        "status": getattr(r, "status", None),
    }


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()


def _read_url(url):
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


def fetch_cf_page(url=DEFAULT_CF_URL):
    for attempt in range(1, 4):
        try:
            html = _read_url(url)
            if "优选地址" in html and "毫秒" in html:
                return html
            print("提示: 第 %d 次抓取未返回数据(可能被风控/限流), 重试..." % attempt)
        except Exception as e:
            print("提示: 第 %d 次抓取失败: %s" % (attempt, e))
        time.sleep(1)
    sys.exit("错误: 连续多次抓取失败: %s" % url)


def parse_cf_rows(html):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 5:
            continue
        line = strip_tags(tds[0])
        ip = strip_tags(tds[1])
        m = re.search(r"(\d+)\s*(?:毫秒|ms)", strip_tags(tds[4]))
        if line in CN_LINE and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) and m:
            rows.append((line, ip, int(m.group(1))))
    return rows


def collect_all_by_line(rows):
    """按线路收集网页上该线路的全部优选IP, 每条线路内按 (延迟, IP) 升序排列"""
    by_line = {}
    for cn, ip, ms in rows:
        by_line.setdefault(cn, []).append((ms, ip))
    if not by_line:
        sys.exit("错误: 未解析到任何线路数据(页面结构可能已变化)")
    return {cn: sorted(lst) for cn, lst in by_line.items()}


def online_pick_ip_map(url):
    """抓取页面, 收集每个线路在网页上的全部优选IP, 返回 ({line: [IP,...]}, {cn: [(ms,ip)...]})"""
    html = fetch_cf_page(url)
    rows = parse_cf_rows(html)
    if not rows:
        sys.exit("错误: 页面中未解析到有效数据(页面结构可能已变化)")
    print("[抓取] 共解析 %d 条候选行(电信/联通/移动), 按线路收集全部如下:" % len(rows))
    by = collect_all_by_line(rows)
    ip_map = {}
    for cn, items in by.items():
        line = CN_LINE.get(cn, cn)
        ip_map[line] = [ip for _ms, ip in items]
        desc = ", ".join("%s(%dms)" % (ip, ms) for ms, ip in items)
        print("  [%s line=%-7s] 共%d个: %s" % (cn, line, len(items), desc))
    return ip_map, by


def cmd_list(args):
    ak, sk = get_ak_sk()
    client, zone, region_id = locate_zone(ak, sk, only_region=args.region)
    if zone is None:
        sys.exit("未找到公网 zone: %s" % ZONE_NAME)
    recordsets = list_a_records_with_line(client, zone)
    print("\n记录集(%s, type=A) 共 %d 条:" % (RECORD_NAME, len(recordsets)))
    if not recordsets:
        print("(未找到, 可用 --record 指定其它名称或人工在控制台确认)")
    for r in recordsets:
        d = dump_line_record(r)
        print("  line=%-10s records=%-18s ttl=%s weight=%s id=%s" % (
            d["line"], ",".join(d["records"]), d["ttl"], d["weight"], d["id"]))
    print("\nregion=%s zone_id=%s" % (region_id, zone.id))


def load_ip_map(args):
    """按来源返回 {线路line值: [IP, ...]}: --online 在线抓全量, 或 --map/--map-file 手动指定"""
    if args.online:
        if args.map or args.map_file:
            sys.exit("错误: --online 与 --map/--map-file 不可同时使用")
        print("[来源] 在线抓取微测网 CF 优选地址页: %s" % args.online)
        ip_map, _by = online_pick_ip_map(args.online)
        return ip_map
    try:
        if args.map_file:
            with open(args.map_file, "r", encoding="utf-8") as f:
                ip_map = json.load(f)
        else:
            ip_map = json.loads(args.map)
        if not isinstance(ip_map, dict):
            raise ValueError("映射需为 JSON 对象")
    except Exception as e:
        sys.exit("错误: 无法解析映射(可用 --map-file 指定 JSON 文件): %s" % e)
    # 手动映射兼容 单IP字符串 与 IP列表 两种写法
    return {k: (v if isinstance(v, list) else [v]) for k, v in ip_map.items()}


def cmd_update(args):
    ip_map = load_ip_map(args)
    ak, sk = get_ak_sk()
    client, zone, region_id = locate_zone(ak, sk, only_region=args.region)
    if zone is None:
        sys.exit("未找到公网 zone: %s" % ZONE_NAME)
    recordsets = list_a_records_with_line(client, zone)
    matched = skipped = 0
    for r in recordsets:
        line = getattr(r, "line", None) or ""
        if line not in ip_map:
            continue
        new_ips = ip_map[line]
        old_ips = list(r.records or [])
        new = ",".join(new_ips)
        if old_ips == new_ips:
            print("[无变化] line=%-8s 已是目标值: %s" % (line, new))
            skipped += 1
            continue
        matched += 1
        if args.dry_run:
            print("[预览] line=%-8s %d个 -> %d个: %s (不写入)" % (line, len(old_ips), len(new_ips), new))
        else:
            body = UpdateRecordSetsReq(
                name=r.name,
                description=getattr(r, "description", "") or "",
                type=r.type,
                ttl=getattr(r, "ttl", None),
                records=new_ips,
                weight=getattr(r, "weight", None),
            )
            client.update_record_sets(
                UpdateRecordSetsRequest(zone_id=zone.id, recordset_id=r.id, body=body)
            )
            print("[已更新] line=%-8s %d个 -> %d个: %s" % (line, len(old_ips), len(new_ips), new))
    if matched == 0 and skipped == 0:
        print("警告: 未找到与映射匹配的记录。请先运行 list 查看实际 line 值。")
    print("完成: 更新 %d 条, 无变化跳过 %d 条。" % (matched, skipped))


def main():
    parser = argparse.ArgumentParser(
        description="华为云 DNS cf-pick.kaphia.top 优选IP更新工具",
        epilog="不带子命令直接运行将显示本帮助。",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_list = sub.add_parser("list", help="查看 zone 与 cf-pick 各线路 A 记录")
    p_list.add_argument("--region", default=None, help="只探测指定区域, 如 ap-southeast-1")

    p_up = sub.add_parser("update", help="更新 A 记录: 用 --online 联网优选, 或 --map/--map-file 手动指定(v2.3)")
    p_up.add_argument("--online", nargs="?", const=DEFAULT_CF_URL, default=None,
                      metavar="URL", help="联网抓取微测网 CF 优选地址页, 把该线路在网页上的全部优选 IP 写入记录集"
                                          "(可另给 URL 覆盖默认源)")
    p_up.add_argument("--map", default=None, help='JSON 映射字符串或数组, 例: {"Dianxin":["1.2.3.4","5.6.7.8"]}')
    p_up.add_argument("--map-file", default=None, help="含 JSON 映射的文件路径(推荐, 避免引号转义问题)")
    p_up.add_argument("--region", default=None)
    p_up.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        print()
        print("用法示例:")
        print("  python huawei_cf_pick.py list                            # 查看当前各线路 A 记录值")
        print("  python huawei_cf_pick.py update --online --dry-run       # 联网优选并预览(不写入)")
        print("  python huawei_cf_pick.py update --online                  # 联网优选并更新 DNS")
        print("  python huawei_cf_pick.py update --map-file ip_update_map.json --dry-run  # 手动映射预览")
        print("  python huawei_cf_pick.py update --map '{\"Dianxin\":\"1.2.3.4\"}' -h       # 查看参数")
        sys.exit(0)
    if args.cmd == "list":
        cmd_list(args)
    else:
        cmd_update(args)


if __name__ == "__main__":
    main()
