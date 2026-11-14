"""Synthetic motor-insurance claim book with planted fraud gangs.

The generator writes raw policy/claim/survey/shop documents plus a separate
ground-truth file. ID numbers are valid-format 18-digit Chinese IDs (checksum
computed) so they look plausible, but every person and event is fictional.

Gang designs
------------
* collision_ring : core members repeatedly hit each other as third parties.
* shop_burst     : bursts of claims at one repair shop inside 60-day windows.
* identity_swap  : shared phone / payout card and swapped insured / third-party roles.
* mixed          : combination of the above.
Each gang also has 1-2 "weak-only" members that share exactly one *weak*
relation (a residential address) with one core member, have their own claims
elsewhere and never share phone, card or a claim with another member.

Decoy clusters (legitimate but structurally suspicious) are planted as
false-positive pressure: extended families sharing a landline, a small
corporate fleet, loyal single-shop customers, ride-share co-drivers, and a
family business that shares address+phone.
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR, GROUND_TRUTH_PATH, RAW_DIR

BOOK_START = date(2023, 1, 1)
BOOK_END = date(2025, 10, 31)
SURNAMES = list("王李张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文")
GIVEN = [
    "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛",
    "明", "超", "秀英", "霞", "平", "刚", "桂英", "建国", "玉兰", "文", "斌", "辉", "红梅",
    "志强", "海燕", "建华", "晓东", "春梅", "国华", "淑珍", "立新", "振华", "丹", "晨", "宇",
    "浩", "鑫", "欣", "悦", "鹏", "帅", "琳", "雪", "萌", "宁", "涛", "峰", "云", "飞", "龙",
    "子轩", "梓涵", "一鸣", "思远", "雨桐", "嘉怡", "俊杰", "欣怡", "浩然", "诗涵",
]
CITIES = [
    ("广州市", "天河区", ["天河北路", "体育西路", "珠江新城华夏路"]),
    ("广州市", "白云区", ["机场路", "白云大道", "石井大道"]),
    ("深圳市", "南山区", ["科技园南路", "南海大道", "深南大道"]),
    ("深圳市", "龙岗区", ["布吉路", "龙城大道", "坂雪岗大道"]),
    ("东莞市", "南城区", ["鸿福路", "三元路", "东莞大道"]),
    ("佛山市", "禅城区", ["季华路", "岭南大道", "汾江中路"]),
]
COMMUNITIES = [
    "阳光花园", "锦绣家园", "金色领域", "翠湖居", "番禺新村", "碧水湾", "绿景苑", "幸福里",
    "嘉和园", "康城小区", "星河湾", "汇景新城", "东方名苑", "御景湾", "和平里", "尚品居",
    "恒福花园", "丽景花园", "时代倾城", "万科城", "保利花园", "中海金沙湾", "富力城",
    "雅居乐", "骏景花园", "珠江帝景", "岭南新世界", "祈福新村", "华南碧桂园", "金地荔湖城",
]
VEHICLE_MODELS = [
    "丰田卡罗拉", "本田思域", "大众朗逸", "日产轩逸", "别克英朗", "现代伊兰特", "比亚迪秦PLUS",
    "五菱宏光", "哈弗H6", "长安CS75", "吉利帝豪", "宝马320Li", "奔驰C260L", "奥迪A4L",
    "特斯拉Model 3", "埃安AION S", "小鹏P7", "理想ONE", "丰田凯美瑞", "本田雅阁",
    "帕萨特", "天籁", "起亚K3", "雪佛兰科鲁泽", "福特福克斯",
]
PLATE_PROV = list("京沪粤浙苏鲁川鄂湘闽桂")
PLATE_LETTERS = list("ABCDEFGHJKLMNPQRSTUVWXYZ")
SHOP_PREFIX = [
    "顺达", "恒信", "宏达", "众诚", "永丰", "金泰", "华运", "鑫源", "德众", "利民", "安顺",
    "瑞丰", "长兴", "广源", "新感觉", "车之家", "博世", "辉煌", "骏马", "联众", "万里",
    "捷诚", "腾飞", "嘉禾", "裕丰", "锐志", "天诚", "奥通", "快修", "鹏程",
]
SHOP_SUFFIX = ["汽车维修服务有限公司", "汽修厂", "汽车服务中心", "钣喷中心", "汽车养护中心", "快修连锁店"]
SURVEYORS = ["陈查勘", "李查勘", "王查勘", "赵查勘", "刘查勘", "周查勘", "吴查勘", "郑查勘"]
TOW = ["安达拖车", "路友救援", "畅通拖车", "平安拖车"]
ACCIDENT_TYPES = ["双方碰撞", "单车事故", "多车事故", "人伤事故", "停放被撞"]


def _id_checksum(body17: str) -> str:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    return check[sum(int(c) * w for c, w in zip(body17, weights)) % 11]


def _luhn(number: str) -> str:
    digits = [int(c) for c in number]
    for i in range(len(digits) - 2, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    return str((10 - sum(digits) % 10) % 10)


def make_bank_card(rng: random.Random) -> str:
    body = "6222" + "".join(str(rng.randint(0, 9)) for _ in range(11))
    return body + _luhn(body + "0")


@dataclass
class Person:
    pid: int
    id_no: str
    name: str
    phone: str
    address: str
    bank_card: str
    birth: date
    kind: str = "background"       # background | gang | decoy
    gang_id: str | None = None
    weak_only: bool = False
    role: str = "member"
    vehicles: list[str] = field(default_factory=list)


class Generator:
    def __init__(self, seed: int = 20261114):
        self.rng = random.Random(seed)
        self.seed = seed
        self.persons: list[Person] = []
        self.vehicles: dict[str, dict] = {}
        self.policies: list[dict] = []
        self.claims: list[dict] = []
        self.surveys: list[dict] = []
        self.shops: list[dict] = []
        self.truth_gangs: list[dict] = []
        self.decoys: list[dict] = []
        self._claim_seq = 0
        self._policy_seq = 0
        self._survey_seq = 0
        self._used_id: set[str] = set()
        self._used_plate: set[str] = set()
        self._used_phone: set[str] = set()
        self._used_vin: set[str] = set()
        self._parked_plates: dict[str, list[str]] = {}   # person -> extra plates
        self._gang_phone: dict[str, str] = {}
        self._gang_card: dict[str, str] = {}
        self._gang_shops: dict[str, list[str]] = {}
        self._dirty_shops: list[str] = []
        self._clean_shops: list[str] = []
        self._background_people: list[int] = []
        self._background_vehicles: list[str] = []
        self._households: list[list[int]] = []

    # ---------------------------------------------------------------- identities
    def new_id_no(self, birth: date) -> str:
        while True:
            # 6-digit administrative division code (province+city+district)
            area = self.rng.choice([
                "440106", "440111", "440305", "440307", "441900", "440604",
                "430102", "320102", "330106", "440104", "440113", "441302",
            ])
            body = area + birth.strftime("%Y%m%d") + f"{self.rng.randint(1, 999):03d}"
            idno = body + _id_checksum(body)
            if idno not in self._used_id:
                self._used_id.add(idno)
                return idno

    def new_phone(self) -> str:
        while True:
            p = self.rng.choice(["13", "15", "18", "17", "19"]) + "".join(
                str(self.rng.randint(0, 9)) for _ in range(9)
            )
            if p not in self._used_phone:
                self._used_phone.add(p)
                return p

    def new_name(self) -> str:
        return self.rng.choice(SURNAMES) + self.rng.choice(GIVEN) + (
            self.rng.choice(GIVEN) if self.rng.random() < 0.25 else ""
        )

    def new_address(self) -> str:
        if not hasattr(self, "_used_address"):
            self._used_address = set()
        while True:
            city, district, streets = self.rng.choice(CITIES)
            addr = (
                f"{city}{district}{self.rng.choice(streets)}{self.rng.randint(1, 199)}号"
                f"{self.rng.choice(COMMUNITIES)}{self.rng.randint(1, 28)}栋"
                f"{self.rng.randint(1, 32)}{self.rng.choice(['01','02','03','04','05','06'])}室"
            )
            if addr not in self._used_address:
                self._used_address.add(addr)
                return addr

    def new_plate(self) -> str:
        while True:
            prov = self.rng.choice(PLATE_PROV)
            plate = prov + self.rng.choice(PLATE_LETTERS) + "".join(
                self.rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5)
            )
            if plate not in self._used_plate:
                self._used_plate.add(plate)
                return plate

    def new_vin(self) -> str:
        while True:
            vin = "L" + "".join(
                self.rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(16)
            )
            if vin not in self._used_vin:
                self._used_vin.add(vin)
                return vin

    def new_person(self, birth: date | None = None, *, kind="background",
                   gang_id=None, weak_only=False, role="member", seed_person: Person | None = None,
                   address: str | None = None, phone: str | None = None) -> Person:
        birth = birth or date(self.rng.randint(1965, 2001), self.rng.randint(1, 12),
                              self.rng.randint(1, 28))
        pid = len(self.persons)
        p = Person(
            pid=pid,
            id_no=self.new_id_no(birth),
            name=self.new_name(),
            phone=phone or self.new_phone(),
            address=address or (seed_person.address if seed_person else self.new_address()),
            bank_card=make_bank_card(self.rng),
            birth=birth,
            kind=kind,
            gang_id=gang_id,
            weak_only=weak_only,
            role=role,
        )
        self.persons.append(p)
        return p

    # ---------------------------------------------------------------- vehicles / policies
    def new_vehicle(self, owner: Person, model: str | None = None) -> dict:
        plate = self.new_plate()
        v = {
            "plate": plate,
            "vin": self.new_vin(),
            "model": model or self.rng.choice(VEHICLE_MODELS),
            "owner_pid": owner.pid,
        }
        self.vehicles[plate] = v
        owner.vehicles.append(plate)
        return v

    def new_policy(self, owner: Person, vehicle: dict, effective: date, premium: float | None = None):
        self._policy_seq += 1
        policy = {
            "policy_no": f"P{self._policy_seq:09d}",
            "insured_name": owner.name,
            "insured_id_no": owner.id_no,
            "phone": owner.phone,
            "address": owner.address,
            "bank_card": owner.bank_card,
            "plate": vehicle["plate"],
            "vin": vehicle["vin"],
            "vehicle_model": vehicle["model"],
            "effective_date": effective.isoformat(),
            "expiry_date": (effective + timedelta(days=365)).isoformat(),
            "premium": round(premium or self.rng.uniform(2800, 9500), 2),
            "owner_pid": owner.pid,
        }
        self.policies.append(policy)
        return policy

    def register_vehicle(self, owner: Person, vehicle: dict, when: date, premium=None):
        return self.new_policy(owner, vehicle, when, premium)

    # ---------------------------------------------------------------- shops
    def build_shops(self, n: int = 160, dirty: int = 62):
        names = []
        for i in range(n):
            base = self.rng.choice(SHOP_PREFIX) + self.rng.choice(SHOP_SUFFIX)
            names.append(f"{base}（{self.rng.choice(['总店','一分店','二分店','城东店','城西店','旗舰店'])}）")
        # unique-ify
        seen = set()
        out = []
        for n_ in names:
            if n_ in seen:
                n_ = n_.replace("）", f"{len(out)}）")
            seen.add(n_)
            out.append(n_)
        for i, name in enumerate(out):
            sid = f"RS{i + 1:04d}"
            city, district, streets = self.rng.choice(CITIES)
            shop = {
                "shop_id": sid,
                "name": name,
                "address": f"{city}{district}{self.rng.choice(streets)}{self.rng.randint(1, 299)}号",
                "risk_note": "",
                "_tier": "dirty" if i < dirty else "clean",
            }
            self.shops.append(shop)
        self._dirty_shops = [s["shop_id"] for s in self.shops if s["_tier"] == "dirty"]
        self._clean_shops = [s["shop_id"] for s in self.shops if s["_tier"] == "clean"]

    # ---------------------------------------------------------------- claims
    def new_claim(
        self,
        owner: Person,
        vehicle: dict,
        loss_date: date,
        *,
        driver: Person | None = None,
        third: Person | None = None,
        third_vehicle: dict | None = None,
        shop_id: str | None = None,
        accident_type: str | None = None,
        loss_amount: float | None = None,
        payout_ratio: float | None = None,
        contact_phone: str | None = None,
        payout_card: str | None = None,
        scene_address: str | None = None,
        statement: str | None = None,
        policy_no: str | None = None,
    ) -> dict:
        driver = driver or owner
        acc = accident_type or self.rng.choices(
            ACCIDENT_TYPES, weights=[0.48, 0.28, 0.12, 0.08, 0.04]
        )[0]
        if acc in ("双方碰撞", "多车事故", "人伤事故") and third is None:
            third = self._pick_local_third(driver, owner)
        if third is not None and third_vehicle is None:
            third_vehicle = self.vehicles.get(third.vehicles[0]) if third.vehicles else None
            if third_vehicle is None:
                third_vehicle = self._add_vehicle_for(third)
        loss = loss_amount if loss_amount is not None else round(self.rng.lognormvariate(8.2, 0.65), 2)
        loss = float(min(loss, 180000))
        ratio = payout_ratio if payout_ratio is not None else self.rng.uniform(0.62, 0.95)
        payout = round(loss * ratio, 2)
        shop = shop_id or self.rng.choice(self._shop_ref())
        self._claim_seq += 1
        claim_no = f"C{self._claim_seq:08d}"
        report = loss_date + timedelta(days=self.rng.randint(0, 4))
        pol = policy_no or self._policy_for(owner, vehicle)
        claim = {
            "claim_no": claim_no,
            "policy_no": pol,
            "report_date": report.isoformat(),
            "loss_date": loss_date.isoformat(),
            "accident_type": acc,
            "loss_amount": round(loss, 2),
            "payout": payout,
            "status": self.rng.choices(
                ["已赔付", "已结案", "拒赔", "处理中"], weights=[0.78, 0.14, 0.05, 0.03]
            )[0],
            "driver_name": driver.name,
            "driver_id_no": driver.id_no,
            "third_party_plate": third_vehicle["plate"] if third_vehicle else None,
            "third_party_name": third.name if third else None,
            "third_party_id_no": third.id_no if third else None,
            "third_party_phone": third.phone if third else None,
            "third_party_vehicle_vin": third_vehicle["vin"] if third_vehicle else None,
            "repair_shop_id": shop,
            "survey_no": None,
            "_owner_pid": owner.pid,
            "_driver_pid": driver.pid,
            "_third_pid": third.pid if third else None,
        }
        self.claims.append(claim)

        self._survey_seq += 1
        survey_no = f"S{self._survey_seq:08d}"
        claim["survey_no"] = survey_no
        self.surveys.append({
            "survey_no": survey_no,
            "claim_no": claim_no,
            "survey_date": (loss_date + timedelta(days=self.rng.randint(0, 2))).isoformat(),
            "surveyor": self.rng.choice(SURVEYORS),
            "scene_address": scene_address or self.rng.choice([
                owner.address, self.rng.choice(CITIES)[0] + "主干道",
                f"{self.rng.choice(CITIES)[0]}{self.rng.choice(['高速','环城快速'])}路段",
            ]),
            "contact_phone": contact_phone or driver.phone,
            "payout_card": payout_card or owner.bank_card,
            "repair_shop_id": shop,
            "statement": statement or self._statement(acc, driver, third),
            "tow_company": self.rng.choice(TOW) if acc in ("单车事故", "多车事故") else "",
        })
        return claim

    def _statement(self, acc, driver, third) -> str:
        if third is None:
            return self.rng.choice([
                "车辆在停放时被剐蹭，未找到肇事方。",
                "行驶中因避让电动车撞上护栏。",
                "车辆涉水后发动机进水。",
                "倒车时刮擦墙体，自行报案。",
            ])
        return self.rng.choice([
            f"由{driver.name}驾驶，与{third.name}驾驶车辆发生碰撞。",
            f"{driver.name}称对方{third.name}变道导致事故。",
            "双方在路口发生轻微碰撞，现场协商后报保险。",
        ])

    def _policy_for(self, owner: Person, vehicle: dict) -> str:
        for p in self.policies:
            if p["plate"] == vehicle["plate"] and p["insured_id_no"] == owner.id_no:
                return p["policy_no"]
        pol = self.new_policy(owner, vehicle, BOOK_START + timedelta(days=self.rng.randint(0, 900)))
        return pol["policy_no"]

    def _add_vehicle_for(self, person: Person) -> dict:
        v = self.new_vehicle(person)
        self.new_policy(person, v, BOOK_START + timedelta(days=self.rng.randint(0, 700)))
        return v

    def _background_people_ref(self):
        return self._background_people

    def _pick_local_third(self, driver: Person, owner: Person) -> Person | None:
        """Pick a same-district counterparty; strangers from across the province
        would fabricate a dense collision network among honest policyholders."""
        reference = driver or owner
        pool = getattr(self, "_district_pool", {}).get(reference.address[:12])
        if not pool:
            return None
        for _ in range(8):
            cand = self.persons[self.rng.choice(pool)]
            if cand.pid != reference.pid and cand.vehicles:
                return cand
        return None

    def _shop_ref(self):
        return self._clean_shops if self.rng.random() < 0.82 else self._dirty_shops

    # ---------------------------------------------------------------- background population
    def build_background(self, households: int = 850):
        clean_shop = self._clean_shops
        dirty = self._dirty_shops
        for _ in range(households):
            size = self.rng.choices([1, 2, 3, 4], weights=[0.24, 0.42, 0.24, 0.10])[0]
            address = self.new_address()
            members = []
            for _ in range(size):
                p = self.new_person(address=address)
                if members and self.rng.random() < 0.10:
                    # family landline: shared by this household only -> legitimate false tie
                    p.phone = members[0].phone
                members.append(p)
                self._background_people.append(p.pid)
            self._households.append([p.pid for p in members])
            # one or two vehicles per household
            for p in members:
                if self.rng.random() < 0.92:
                    v = self.new_vehicle(p)
                    self._background_vehicles.append(v["plate"])
                    self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 820)))
                    if self.rng.random() < 0.13:
                        v2 = self.new_vehicle(p)
                        self.register_vehicle(p, v2, BOOK_START + timedelta(days=self.rng.randint(300, 900)))

        # occasional shared landline families (6 clusters) -> decoy pressure
        for _ in range(6):
            address = self.new_address()
            phone = self.new_phone()
            base = self.new_person(address=address, phone=phone)
            self._background_people.append(base.pid)
            members = [base]
            for _ in range(self.rng.randint(2, 3)):
                p = self.new_person(address=address, phone=phone)
                self._background_people.append(p.pid)
                members.append(p)
            for p in members:
                v = self.new_vehicle(p)
                self._background_vehicles.append(v["plate"])
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 800)))

        # background claims. Third parties are drawn from the same postal district
        # (first 4 digits of the address block), which keeps the honest population
        # sparse: random strangers do not form a 1,800-person connected blob.
        district_pool: dict[str, list[int]] = {}
        for pid in self._background_people:
            addr = self.persons[pid].address
            district_pool.setdefault(addr[:12], []).append(pid)
        self._district_pool = district_pool
        for pid in self._background_people:
            person = self.persons[pid]
            if not person.vehicles:
                continue
            n = self.rng.choices([0, 1, 2, 3, 4], weights=[0.34, 0.36, 0.20, 0.08, 0.02])[0]
            for _ in range(n):
                vehicle = self.vehicles[person.vehicles[0]]
                loss = BOOK_START + timedelta(days=self.rng.randint(0, (BOOK_END - BOOK_START).days))
                third = None
                if self.rng.random() < 0.55:
                    pool = district_pool.get(person.address[:12]) or self._background_people
                    third = self.persons[self.rng.choice(pool)]
                    if third.pid == pid or not third.vehicles:
                        third = None
                shop = self.rng.choice(clean_shop) if self.rng.random() < 0.9 else self.rng.choice(dirty)
                driver = person
                if self.rng.random() < 0.14 and self._households:
                    hh = [pid_ for pid_ in self._households[self.persons[pid].pid % len(self._households)]]
                    others = [q for q in hh if q != pid]
                    if others:
                        driver = self.persons[self.rng.choice(others)]
                self.new_claim(person, vehicle, loss, driver=driver, third=third,
                               third_vehicle=self.vehicles[third.vehicles[0]] if third and third.vehicles else None,
                               shop_id=shop)

        # decoys
        self._build_decoys()

    # ---------------------------------------------------------------- gangs
    def build_gangs(self, n: int = 45):
        styles = (["collision_ring"] * 15) + (["shop_burst"] * 15) + (["identity_swap"] * 10) + (["mixed"] * 5)
        self.rng.shuffle(styles)
        used_shops = set()
        for gi in range(n):
            style = styles[gi]
            gid = f"GT-{gi + 1:02d}"
            size = self.rng.choices([4, 5, 6, 7, 8, 9], weights=[0.15, 0.24, 0.24, 0.18, 0.12, 0.07])[0]
            n_weak = 2 if self.rng.random() < 0.22 else 1
            n_core = size - n_weak
            base_address = self.new_address()
            members: list[Person] = []
            for i in range(size):
                weak = i >= n_core
                role = "organizer" if i == 0 else ("weak_pendant" if weak else "core")
                p = self.new_person(
                    address=base_address if not weak or i == n_core else self.new_address(),
                    kind="gang", gang_id=gid, weak_only=weak, role=role,
                )
                members.append(p)
            # weak member shares address with one core; use a *different* address for the
            # second weak member to keep each pendant on exactly one weak edge
            for i in range(n_core, size):
                if i == n_core:
                    members[i].address = members[0].address
                else:
                    members[i].address = members[1].address

            gang_phone = self.new_phone()
            gang_card = make_bank_card(self.rng)
            self._gang_phone[gid] = gang_phone
            self._gang_card[gid] = gang_card
            shops = self._assign_gang_shops(gid, used_shops)
            self._gang_shops[gid] = shops

            # vehicles and policies: every member 1 primary + extras; organizer 3 plates.
            plates = []
            for i, p in enumerate(members):
                v = self.new_vehicle(p)
                plates.append(v["plate"])
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 400)))
                if not p.weak_only and self.rng.random() < 0.7:
                    v2 = self.new_vehicle(p)
                    self.register_vehicle(p, v2, BOOK_START + timedelta(days=self.rng.randint(400, 900)))
                if i == 0:
                    v3 = self.new_vehicle(p)
                    self.register_vehicle(p, v3, BOOK_START + timedelta(days=self.rng.randint(200, 700)))
            # plate re-registration inside gang: organizer's third plate later insured by another core
            if len(plates) >= 2 and n_core >= 3:
                donor = members[0]
                recipient = members[1]
                v = self.vehicles[donor.vehicles[-1]]
                self.new_policy(recipient, v, BOOK_START + timedelta(days=self.rng.randint(500, 880)))

            # build claims by style
            if style == "collision_ring":
                self._claims_collision(gid, members, shops, gang_phone, gang_card)
            elif style == "shop_burst":
                self._claims_shop_burst(gid, members, shops, gang_phone, gang_card, base_address)
            elif style == "identity_swap":
                self._claims_identity(gid, members, shops, gang_phone, gang_card)
            else:
                self._claims_mixed(gid, members, shops, gang_phone, gang_card, base_address)

            self.truth_gangs.append({
                "gang_id": gid,
                "style": style,
                "size": size,
                "core_size": n_core,
                "weak_members": [m.id_no for m in members[n_core:]],
                "members": [m.id_no for m in members],
                "member_names": [m.name for m in members],
                "shared_phone": gang_phone,
                "shared_card": gang_card,
                "shops": shops,
            })

    def _assign_gang_shops(self, gid: str, used: set[str]) -> list[str]:
        # 12 dirty shops are shared by two gangs -> repair-shop collusion network
        candidates = [s for s in self._dirty_shops if s not in used]
        if self.rng.random() < 0.25:
            shared = list(used)
            if shared:
                return [self.rng.choice(shared), self.rng.choice(candidates or self._dirty_shops)]
        primary = self.rng.choice(candidates or self._dirty_shops)
        used.add(primary)
        secondary = primary
        if self.rng.random() < 0.55:
            secondary = self.rng.choice(candidates or self._dirty_shops)
            used.add(secondary)
        return [primary] if primary == secondary else [primary, secondary]

    def _gang_dates(self, n: int, bursts: list[tuple[date, int]] | None = None) -> list[date]:
        out = []
        if bursts:
            for start, count in bursts:
                for _ in range(count):
                    # concentrate: 60% inside first 14 days
                    if self.rng.random() < 0.6:
                        d = start + timedelta(days=self.rng.randint(0, 13))
                    else:
                        d = start + timedelta(days=self.rng.randint(14, 50))
                    out.append(d)
        while len(out) < n:
            d = BOOK_START + timedelta(days=self.rng.randint(60, (BOOK_END - BOOK_START).days - 30))
            out.append(d)
        self.rng.shuffle(out)
        return out[:n]

    def _claim_members(self, gid, members, shops, gang_phone, gang_card, style, weak_n=2):
        """weak members only file their own claims at an unrelated clean shop."""
        for m in members:
            if not m.weak_only:
                continue
            shop = self.rng.choice(self._clean_shops)
            v = self.vehicles[m.vehicles[0]]
            for _ in range(weak_n):
                third = self.persons[self.rng.choice(self._background_people)]
                loss = BOOK_START + timedelta(days=self.rng.randint(120, 950))
                self.new_claim(
                    m, v, loss, third=third,
                    third_vehicle=self.vehicles[third.vehicles[0]] if third.vehicles else None,
                    shop_id=shop, loss_amount=self.rng.uniform(18000, 52000),
                    payout_ratio=self.rng.uniform(0.82, 0.95),
                    contact_phone=self._own_phone(m, gang_phone), payout_card=self._own_card(m, gang_card),
                )

    def _own_phone(self, m: Person, gang_phone: str) -> str:
        """Weak pendants only use identifiers they own — never the gang's shared ones."""
        return m.phone if m.weak_only else gang_phone

    def _own_card(self, m: Person, gang_card: str) -> str:
        return m.bank_card if m.weak_only else gang_card

    def _inflated(self):
        return self.rng.uniform(14000, 46000), self.rng.uniform(0.82, 0.96)

    def _claims_collision(self, gid, members, shops, gang_phone, gang_card):
        core = [m for m in members if not m.weak_only]
        shop = shops[0]
        bursts = [(date(2024, 4, 8), 4), (date(2025, 3, 3), 4)]
        dates = self._gang_dates(len(core) * 2 + 3, bursts)
        pairs = []
        for i in range(len(core)):
            a = core[i]
            b = core[(i + 1) % len(core)] if len(core) > 2 else core[(i + 1) % len(core)]
            pairs.append((a, b))
        if len(core) >= 5:
            pairs.append((core[0], core[3]))
            pairs.append((core[1], core[4]))
        di = 0
        for a, b in pairs:
            for _ in range(self.rng.randint(1, 2)):
                d1 = dates[di % len(dates)]; di += 1
                d2 = d1 + timedelta(days=self.rng.randint(4, 25))
                loss, ratio = self._inflated()
                self.new_claim(
                    a, self.vehicles[a.vehicles[0]], d1, third=b,
                    third_vehicle=self.vehicles[b.vehicles[0]], shop_id=shop,
                    accident_type=self.rng.choice(["双方碰撞", "多车事故", "人伤事故"]),
                    loss_amount=loss, payout_ratio=ratio,
                    contact_phone=gang_phone, payout_card=gang_card,
                )
                loss2, ratio2 = self._inflated()
                self.new_claim(
                    b, self.vehicles[b.vehicles[0]], d2, third=a,
                    third_vehicle=self.vehicles[a.vehicles[0]], shop_id=shop,
                    accident_type=self.rng.choice(["双方碰撞", "多车事故"]),
                    loss_amount=loss2, payout_ratio=ratio2,
                    contact_phone=gang_phone, payout_card=gang_card,
                )
        # filler single-vehicle claims at shop (burst / frequency)
        for k in range(3):
            a = core[k % len(core)]
            loss, ratio = self._inflated()
            d = dates[(len(pairs) * 2 + k) % len(dates)]
            self.new_claim(
                a, self.vehicles[a.vehicles[0]], d, shop_id=shop,
                accident_type=self.rng.choice(["单车事故", "停放被撞", "人伤事故"]),
                loss_amount=loss, payout_ratio=ratio,
                contact_phone=gang_phone, payout_card=gang_card,
            )
        self._claim_members(gid, members, shops, gang_phone, gang_card, "collision_ring",
                             weak_n=2)

    def _claims_shop_burst(self, gid, members, shops, gang_phone, gang_card, base_address):
        core = [m for m in members if not m.weak_only]
        shop = shops[0]
        bursts = [(date(2024, 6, 10), 6), (date(2025, 3, 17), 7)]
        dates = self._gang_dates(17, bursts)
        for i, d in enumerate(dates[:13]):
            m = core[i % len(core)]
            # repeat customers: each core appears >=2 times
            third = None
            if self.rng.random() < 0.25:
                third = self.persons[self.rng.choice(self._background_people)]
            loss, ratio = self._inflated()
            self.new_claim(
                m, self.vehicles[m.vehicles[0]], d,
                third=third,
                third_vehicle=self.vehicles[third.vehicles[0]] if third and third.vehicles else None,
                shop_id=shop,
                accident_type=self.rng.choices(["单车事故", "停放被撞", "人伤事故", "双方碰撞"],
                                               weights=[0.42, 0.28, 0.20, 0.10])[0],
                loss_amount=loss, payout_ratio=ratio,
                contact_phone=gang_phone, payout_card=gang_card,
                scene_address=base_address,
            )
        # occasional second-shop claims (rotating repair shops)
        if len(shops) > 1:
            for k in range(2):
                m = core[k % len(core)]
                loss, ratio = self._inflated()
                self.new_claim(
                    m, self.vehicles[m.vehicles[0]],
                    BOOK_START + timedelta(days=self.rng.randint(200, 850)),
                    shop_id=shops[1], accident_type="单车事故",
                    loss_amount=loss, payout_ratio=ratio,
                    contact_phone=gang_phone, payout_card=gang_card,
                )
        self._claim_members(gid, members, shops, gang_phone, gang_card, "shop_burst", weak_n=2)

    def _claims_identity(self, gid, members, shops, gang_phone, gang_card):
        core = [m for m in members if not m.weak_only]
        dates = self._gang_dates(len(core) * 2 + 2)
        for i, d in enumerate(dates):
            a = core[i % len(core)]
            b = core[(i + 1) % len(core)]
            third = b if self.rng.random() < 0.55 else None
            loss, ratio = self._inflated()
            self.new_claim(
                a, self.vehicles[(a.vehicles[1] if len(a.vehicles) > 1 else a.vehicles[0])], d,
                third=third,
                third_vehicle=self.vehicles[third.vehicles[0]] if third else None,
                shop_id=self.rng.choice(shops), accident_type="双方碰撞" if third else "单车事故",
                loss_amount=loss, payout_ratio=ratio,
                contact_phone=gang_phone, payout_card=gang_card,
                scene_address=a.address,
            )
        self._claim_members(gid, members, shops, gang_phone, gang_card, "identity_swap", weak_n=3)

    def _claims_mixed(self, gid, members, shops, gang_phone, gang_card, base_address):
        core = [m for m in members if not m.weak_only]
        shop = shops[0]
        bursts = [(date(2024, 7, 1), 5), (date(2025, 4, 14), 5)]
        dates = self._gang_dates(16, bursts)
        # ring
        for i in range(min(3, len(core) - 1)):
            a, b = core[i], core[i + 1]
            d1 = dates[i * 2]
            loss, ratio = self._inflated()
            self.new_claim(a, self.vehicles[a.vehicles[0]], d1, third=b,
                           third_vehicle=self.vehicles[b.vehicles[0]], shop_id=shop,
                           accident_type="双方碰撞", loss_amount=loss, payout_ratio=ratio,
                           contact_phone=gang_phone, payout_card=gang_card)
            d2 = d1 + timedelta(days=self.rng.randint(5, 20))
            loss, ratio = self._inflated()
            self.new_claim(b, self.vehicles[b.vehicles[0]], d2, third=a,
                           third_vehicle=self.vehicles[a.vehicles[0]], shop_id=shop,
                           accident_type="双方碰撞", loss_amount=loss, payout_ratio=ratio,
                           contact_phone=gang_phone, payout_card=gang_card)
        # burst fillers
        for i, d in enumerate(dates[6:14]):
            m = core[i % len(core)]
            loss, ratio = self._inflated()
            self.new_claim(m, self.vehicles[m.vehicles[0]], d, shop_id=shop,
                           accident_type=self.rng.choice(["单车事故", "停放被撞"]),
                           loss_amount=loss, payout_ratio=ratio,
                           contact_phone=gang_phone, payout_card=gang_card,
                           scene_address=base_address)
        self._claim_members(gid, members, shops, gang_phone, gang_card, "mixed", weak_n=2)

    # ---------------------------------------------------------------- decoys
    def _build_decoys(self):
        # 1) extended family with a shared landline (strong) but ordinary claims
        for _ in range(6):
            address = self.new_address()
            phone = self.new_phone()
            ppl = []
            for _ in range(self.rng.randint(3, 4)):
                p = self.new_person(address=address, phone=phone)
                v = self.new_vehicle(p)
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 700)))
                ppl.append(p)
            for p in ppl:
                for _ in range(self.rng.randint(0, 2)):
                    v = self.vehicles[p.vehicles[0]]
                    self.new_claim(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                                   shop_id=self.rng.choice(self._clean_shops))
            self.decoys.append({"kind": "family_landline", "members": [p.id_no for p in ppl]})
        # 2) corporate fleet: shared driver + fleet shop
        for _ in range(2):
            address = self.new_address()
            ppl = []
            for _ in range(5):
                p = self.new_person(address=address)
                v = self.new_vehicle(p, "大众朗逸")
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 400)))
                ppl.append(p)
            fleet_shop = self.rng.choice(self._clean_shops)
            # company phone used on survey contact
            company_phone = self.new_phone()
            for _ in range(12):
                a, b = self.rng.sample(ppl, 2)
                self.new_claim(a, self.vehicles[a.vehicles[0]],
                               BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                               driver=b, shop_id=fleet_shop, contact_phone=company_phone)
            self.decoys.append({"kind": "corporate_fleet", "members": [p.id_no for p in ppl]})
        # 3) loyal single-shop customers (weak co_repair_shop only)
        for _ in range(3):
            shop = self.rng.choice(self._clean_shops)
            ppl = []
            for _ in range(6):
                p = self.new_person(kind="decoy")
                v = self.new_vehicle(p)
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 700)))
                for _ in range(self.rng.randint(3, 4)):
                    self.new_claim(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                                   shop_id=shop)
                ppl.append(p)
            self.decoys.append({"kind": "shop_loyal", "members": [p.id_no for p in ppl]})
        # 4) ride-share co-drivers sharing one vehicle
        for _ in range(2):
            owner = self.new_person(kind="decoy")
            v = self.new_vehicle(owner)
            self.register_vehicle(owner, v, BOOK_START)
            ppl = [owner]
            for _ in range(3):
                p = self.new_person(kind="decoy", address=owner.address)
                ppl.append(p)
                for _ in range(2):
                    self.new_claim(owner, v, BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                                   driver=p, shop_id=self.rng.choice(self._clean_shops))
            self.decoys.append({"kind": "ride_share", "members": [p.id_no for p in ppl]})
        # 5) family business sharing address + phone + one shop (borderline)
        for _ in range(3):
            address = self.new_address()
            phone = self.new_phone()
            shop = self.rng.choice(self._clean_shops)
            ppl = []
            for _ in range(4):
                p = self.new_person(address=address, phone=phone, kind="decoy")
                v = self.new_vehicle(p)
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 500)))
                for _ in range(self.rng.randint(1, 2)):
                    self.new_claim(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                                   shop_id=shop, contact_phone=phone, payout_card=p.bank_card)
                ppl.append(p)
            self.decoys.append({"kind": "family_business", "members": [p.id_no for p in ppl]})
        # 6) dealership staff sharing company phone, different shops, normal claims
        for _ in range(2):
            phone = self.new_phone()
            ppl = []
            for _ in range(4):
                p = self.new_person(phone=phone, kind="decoy")
                v = self.new_vehicle(p)
                self.register_vehicle(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 600)))
                for _ in range(self.rng.randint(1, 2)):
                    self.new_claim(p, v, BOOK_START + timedelta(days=self.rng.randint(0, 1000)),
                                   shop_id=self.rng.choice(self._clean_shops),
                                   contact_phone=phone)
                ppl.append(p)
            self.decoys.append({"kind": "dealer_staff", "members": [p.id_no for p in ppl]})

    # ---------------------------------------------------------------- export
    def export(self, raw_dir: Path = RAW_DIR, truth_path: Path = GROUND_TRUTH_PATH):
        raw_dir.mkdir(parents=True, exist_ok=True)
        policies = [{k: v for k, v in p.items() if not k.startswith("_") and k != "owner_pid"}
                    for p in self.policies]
        claims = [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.claims]
        shops = [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.shops]
        for name, rows in (("policies", policies), ("claims", claims),
                           ("surveys", self.surveys), ("shops", shops)):
            (raw_dir / f"{name}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            with (raw_dir / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        truth = {
            "seed": self.seed,
            "note": "GROUND TRUTH — evaluation only; never read by the detection pipeline.",
            "book_start": BOOK_START.isoformat(),
            "book_end": BOOK_END.isoformat(),
            "gang_count": len(self.truth_gangs),
            "gangs": self.truth_gangs,
            "decoys": self.decoys,
            "persons": [
                {
                    "id_no": p.id_no, "name": p.name, "kind": p.kind,
                    "gang_id": p.gang_id, "weak_only": p.weak_only, "role": p.role,
                }
                for p in self.persons
            ],
            "totals": {
                "persons": len(self.persons), "policies": len(self.policies),
                "claims": len(self.claims), "surveys": len(self.surveys),
                "shops": len(self.shops),
            },
        }
        truth_path.parent.mkdir(parents=True, exist_ok=True)
        truth_path.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
        return {
            "policies": len(policies), "claims": len(claims),
            "surveys": len(self.surveys), "shops": len(shops),
            "gangs": len(self.truth_gangs), "decoys": len(self.decoys),
            "raw_dir": str(raw_dir), "ground_truth": str(truth_path),
        }


def generate(seed: int = 20261114) -> Generator:
    g = Generator(seed)
    g.build_shops()
    g.build_background(households=850)
    g.build_gangs(45)
    return g


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate synthetic claim book with planted gangs")
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    args = ap.parse_args()
    gen = generate(args.seed)
    result = gen.export(args.out / "raw", args.out / "ground_truth.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("claims:", len(gen.claims), "persons:", len(gen.persons))
    # sanity: weak member edge audit
    print("gangs:", len(gen.truth_gangs), "weak members:",
          sum(len(g["weak_members"]) for g in gen.truth_gangs))
