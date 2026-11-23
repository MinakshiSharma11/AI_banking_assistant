"""Deterministic synthetic data generator (no database access).

`Generator(seed, now).run()` returns a dict of table name -> list of row tuples, in the
column order given by `COLUMNS`. Same seed and same `now` give identical output. Every
name, ID and amount is synthetic.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import uuid
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from faker import Faker

from mocks.seed import reference as ref

Row = tuple[Any, ...]

COLUMNS: dict[str, list[str]] = {
    "core.branch_master": ["branch_code", "branch_name", "city", "region"],
    "core.account_class": ["account_class", "description", "product_family"],
    "core.txn_type": ["txn_code", "description", "txn_group"],
    "core.customer": [
        "customer_no",
        "full_name",
        "national_id",
        "mobile_no",
        "segment",
        "home_branch",
        "created_date",
    ],
    "core.cust_account": [
        "account_no",
        "customer_no",
        "branch_code",
        "account_class",
        "ccy",
        "account_open_date",
        "record_stat",
        "auth_stat",
        "closed_date",
    ],
    "obdx.digx_user": [
        "user_id",
        "customer_no",
        "user_type",
        "status",
        "registered_at",
        "last_login_at",
        "failed_attempts",
    ],
    "obdx.digx_user_session": [
        "session_id",
        "user_id",
        "channel",
        "device_os",
        "login_at",
        "last_activity_at",
        "logout_at",
        "login_status",
        "failure_reason",
    ],
    "obdx.origination_application": [
        "application_id",
        "customer_no",
        "account_no",
        "account_class",
        "channel",
        "status",
        "started_at",
        "completed_at",
    ],
    "core.transaction": [
        "txn_ref",
        "account_no",
        "txn_code",
        "drcr_ind",
        "lcy_amount",
        "txn_date",
        "txn_ts",
        "channel",
        "status",
        "session_id",
    ],
    "atum.wf_definition": ["wf_code", "wf_name", "app_name", "version", "is_active"],
    "atum.wf_stage": [
        "wf_code",
        "stage_code",
        "stage_seq",
        "stage_name",
        "stage_type",
        "sla_hours",
        "assigned_role",
    ],
    "atum.wf_instance": [
        "instance_id",
        "wf_code",
        "wf_version",
        "reference_no",
        "customer_no",
        "branch_code",
        "current_stage",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
        "payload_format",
        "payload_json",
        "payload_blob",
        "payload_raw",
    ],
    "atum.wf_stage_history": [
        "instance_id",
        "stage_code",
        "entered_at",
        "exited_at",
        "action",
        "actor_user",
        "actor_role",
        "sla_breached",
        "event_data",
    ],
    "atum.wf_document": [
        "doc_id",
        "instance_id",
        "doc_type",
        "mime_type",
        "content",
        "size_bytes",
        "uploaded_at",
        "ocr_json",
    ],
}
# Load order respects foreign keys.
LOAD_ORDER = list(COLUMNS)

# Kuwait work week is Sunday-Thursday: Friday and Saturday are quiet (index = weekday()).
WEEKDAY_WEIGHT = [1.0, 1.0, 1.0, 1.0, 0.45, 0.6, 1.0]
HOURS_BRANCH = [0] * 8 + [6, 10, 10, 9, 8, 6] + [0] * 10
HOURS_DIGITAL = [2, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 7, 7, 6, 6, 6, 6, 6, 7, 8, 9, 9, 7, 4]
HOURS_CARD = [1, 0, 0, 0, 0, 0, 1, 2, 4, 5, 6, 7, 8, 7, 6, 6, 7, 8, 9, 10, 10, 9, 6, 3]
WINDOW_DAYS = 730


@dataclass(frozen=True)
class Volumes:
    customers: int = 6000
    accounts: int = 9000
    applications: int = 7500
    digital_users: int = 4500
    sessions: int = 150_000
    transactions: int = 300_000
    loan_instances: int = 5000
    casa_instances: int = 3000
    documents: int = 12_000
    active_sessions: tuple[int, int] = (50, 150)

    def scaled(self, f: float) -> Volumes:
        """Smaller copy with the same ratios, for fast unit tests."""
        lo, hi = self.active_sessions
        return replace(
            self,
            customers=max(50, int(self.customers * f)),
            accounts=max(75, int(self.accounts * f)),
            applications=max(60, int(self.applications * f)),
            digital_users=max(40, int(self.digital_users * f)),
            sessions=max(500, int(self.sessions * f)),
            transactions=max(500, int(self.transactions * f)),
            loan_instances=max(50, int(self.loan_instances * f)),
            casa_instances=max(30, int(self.casa_instances * f)),
            documents=max(60, int(self.documents * f)),
            active_sessions=(max(3, int(lo * f)), max(5, int(hi * f))),
        )


@dataclass
class _Account:
    account_no: str
    cust: int
    branch: str
    cls: str
    open_date: date
    record_stat: str
    auth_stat: str
    closed_date: date | None
    digital: bool = False


@dataclass
class _Session:
    login_at: datetime
    last_activity_at: datetime
    session_id: uuid.UUID
    channel: str


@dataclass
class _Visit:
    stage: str
    dwell_h: float | None  # None = still open (in progress) or END stage
    action: str | None
    breached: bool | None
    event: dict[str, Any] | None = None
    entered: datetime | None = None
    exited: datetime | None = None


@dataclass
class _Case:
    wf: str
    outcome: str  # COMPLETED / REJECTED / CANCELLED / IN_PROGRESS
    visits: list[_Visit]
    cust: int
    branch: str
    created_at: datetime = field(default_factory=lambda: datetime.min)
    account_no: str | None = None
    account_cls: str | None = None


class Generator:
    def __init__(
        self, seed: int, now: datetime, tz: ZoneInfo | None = None, vol: Volumes | None = None
    ) -> None:
        self.tz = tz or ZoneInfo("Asia/Kuwait")
        self.rng = random.Random(seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(seed)
        self.now = now.astimezone(self.tz)
        self.today = self.now.date()
        self.start = self.today - timedelta(days=WINDOW_DAYS)
        self.vol = vol or Volumes()
        self.tables: dict[str, list[Row]] = {t: [] for t in COLUMNS}

    # ---- helpers -----------------------------------------------------------
    def frac(self, d: date) -> float:
        return min(1.0, max(0.0, (d - self.start).days / WINDOW_DAYS))

    def sample_date(self, lo: date, hi: date, growth: float, weekday: bool = True) -> date:
        """Date in [lo, hi], denser later (growth) and on Kuwait working days."""
        lo = max(lo, self.start)
        hi = min(hi, self.today)
        if hi <= lo:
            return hi
        span = (hi - lo).days
        while True:
            d = lo + timedelta(days=self.rng.randint(0, span))
            w = (1 + growth * self.frac(d)) / (1 + growth)
            if weekday:
                w *= WEEKDAY_WEIGHT[d.weekday()]
            if self.rng.random() < w:
                return d

    def at(self, d: date, hours: list[int]) -> datetime:
        """A timestamp on day d, hour drawn from a profile, never later than now."""
        h = self.rng.choices(range(24), weights=hours)[0]
        dt = datetime.combine(
            d, time(h, self.rng.randint(0, 59), self.rng.randint(0, 59)), tzinfo=self.tz
        )
        if dt > self.now:
            dt = self.now - timedelta(minutes=self.rng.uniform(20, 180))
        return dt

    def uuid4(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def pick(self, options: Sequence[str], weights: Sequence[float]) -> str:
        return self.rng.choices(options, weights=weights)[0]

    # ---- reference ---------------------------------------------------------
    def reference(self) -> None:
        self.tables["core.branch_master"] = [b[:4] for b in ref.BRANCHES]
        self.tables["core.account_class"] = list(ref.ACCOUNT_CLASSES)
        self.tables["core.txn_type"] = [t[:3] for t in ref.TXN_TYPES]
        self.tables["atum.wf_definition"] = [(*d, True) for d in ref.WF_DEFINITIONS]
        self.tables["atum.wf_stage"] = list(ref.WF_STAGES)

    # ---- customers and accounts -------------------------------------------
    def customers_and_accounts(self) -> None:
        v, rng = self.vol, self.rng
        codes = [b[0] for b in ref.BRANCHES]
        bw = [b[4] for b in ref.BRANCHES]
        self.segments = rng.choices("RPC", weights=[85, 10, 5], k=v.customers)
        self.home = rng.choices(codes, weights=bw, k=v.customers)
        self.cust_no = [f"{200000001 + i}" for i in range(v.customers)]
        extra_w = [{"R": 1.0, "P": 3.0, "C": 4.0}[s] for s in self.segments]
        owners = list(range(v.customers)) + rng.choices(
            range(v.customers), weights=extra_w, k=v.accounts - v.customers
        )
        first_date: dict[int, date] = {}
        self.accounts: list[_Account] = []
        for n, c in enumerate(owners):
            seg, first = self.segments[c], c not in first_date
            if first:
                d = self.sample_date(self.start, self.today, growth=0.6)
            else:
                d = self.sample_date(first_date[c], self.today, growth=0.6)
            cls = self._pick_class(seg, first)
            branch = self.home[c] if rng.random() < 0.85 else rng.choices(codes, weights=bw)[0]
            auth = "U" if rng.random() < 0.02 else "A"
            if auth == "U":  # pending authorisation: recent
                d = self.sample_date(self.today - timedelta(days=60), self.today, growth=0)
            if first:
                first_date[c] = d
            closed: date | None = None
            if auth == "A" and (self.today - d).days > 31 and rng.random() < 0.064:
                closed = d + timedelta(days=rng.randint(30, (self.today - d).days))
            # Share of ALL new accounts that are digital grows 20% -> ~55%; corporate
            # customers (~9% of accounts) never onboard digitally, so scale the rest up.
            p_digital = min(0.95, (0.20 + 0.37 * self.frac(d)) / 0.91) if seg != "C" else 0.0
            self.accounts.append(
                _Account(
                    account_no=f"{branch}{10_000_001 + n:011d}",
                    cust=c,
                    branch=branch,
                    cls=cls,
                    open_date=d,
                    record_stat="C" if closed else "O",
                    auth_stat=auth,
                    closed_date=closed,
                    digital=rng.random() < p_digital,
                )
            )
        # A customer is created when their first account is opened.
        self.created = [first_date[c] for c in range(v.customers)]
        for c in range(v.customers):
            self.tables["core.customer"].append(
                (
                    self.cust_no[c],
                    self.fake.name(),
                    f"{rng.choice('23')}{rng.randint(0, 10**11 - 1):011d}",
                    f"+965{rng.choice('569')}{rng.randint(0, 9_999_999):07d}",
                    self.segments[c],
                    self.home[c],
                    self.created[c],
                )
            )
        for a in self.accounts:
            self.tables["core.cust_account"].append(
                (
                    a.account_no,
                    self.cust_no[a.cust],
                    a.branch,
                    a.cls,
                    "KWD",
                    a.open_date,
                    a.record_stat,
                    a.auth_stat,
                    a.closed_date,
                )
            )

    def _pick_class(self, seg: str, first: bool) -> str:
        if seg == "C":
            return self.pick(["CURCOR", "TD03M", "TD06M", "TD12M"], [80, 6, 7, 7])
        if first:
            w = [35, 25, 15, 10, 4, 5, 6] if seg == "R" else [20, 15, 20, 25, 6, 6, 8]
            return self.pick(["SAVREG", "SALACC", "CURIND", "SAVPLS", "TD03M", "TD06M", "TD12M"], w)
        return self.pick(
            ["SAVPLS", "SAVREG", "CURIND", "TD03M", "TD06M", "TD12M"], [20, 15, 10, 15, 18, 22]
        )

    # ---- OBDX origination --------------------------------------------------
    def applications(self) -> None:
        v, rng = self.vol, self.rng
        rows: list[list[Any]] = []
        self.app_first: dict[int, tuple[datetime, datetime]] = {}
        digital = [a for a in self.accounts if a.digital]
        for a in digital:
            completed = self.at(a.open_date, HOURS_DIGITAL)
            started = completed - timedelta(hours=rng.uniform(0.2, 72))
            ch = "MOBILE" if rng.random() < 0.55 + 0.25 * self.frac(a.open_date) else "WEB"
            rows.append([a.cust, a.account_no, a.cls, ch, "APPROVED", started, completed])
            prev = self.app_first.get(a.cust)
            if prev is None or started < prev[0]:
                self.app_first[a.cust] = (started, completed)
        # Approved apps are fixed by the digital-account curve; keep "80% of decided are
        # approved" and let the submitted share follow (ADR-0002).
        n_approved = len(rows)
        n_rejected = round(n_approved / 0.80) - n_approved
        n_total = max(v.applications, n_approved + n_rejected)
        n_pending = max(1, n_total // 125)
        n_submitted = min(n_total, n_approved + n_rejected + n_pending)
        n_pending = n_submitted - n_approved - n_rejected
        n_open = n_total - n_submitted
        n_started = min(max(1, n_total // 95), n_open)
        retail = [c for c in range(v.customers) if self.segments[c] != "C"]
        classes = ["SAVREG", "SALACC", "CURIND", "SAVPLS", "TD03M", "TD06M", "TD12M"]
        cls_w = [30, 25, 15, 12, 6, 6, 6]
        plan = (
            ["SUBMITTED"] * n_pending
            + ["REJECTED"] * n_rejected
            + ["STARTED"] * n_started
            + ["ABANDONED"] * (n_open - n_started)
        )
        for status in plan:
            c = rng.choice(retail)
            if status == "STARTED":
                d = self.sample_date(self.today - timedelta(days=3), self.today, 0)
            elif status == "SUBMITTED":
                d = self.sample_date(self.today - timedelta(days=5), self.today, 0)
            else:
                d = self.sample_date(self.start, self.today, growth=0.6)
            started = self.at(d, HOURS_DIGITAL)
            done = None
            if status == "REJECTED":
                done = min(self.now, started + timedelta(hours=rng.uniform(2, 72)))
            ch = "MOBILE" if rng.random() < 0.55 + 0.25 * self.frac(d) else "WEB"
            rows.append([c, None, self.pick(classes, cls_w), ch, status, started, done])
        rows.sort(key=lambda r: r[5])
        self.tables["obdx.origination_application"] = [
            (f"APP{n + 1:09d}", self.cust_no[r[0]], r[1], r[2], r[3], r[4], r[5], r[6])
            for n, r in enumerate(rows)
        ]

    # ---- OBDX users and sessions -------------------------------------------
    def digital_users(self) -> None:
        v, rng = self.vol, self.rng
        chosen = sorted(self.app_first)
        pool = [c for c in range(v.customers) if c not in self.app_first]
        rng.shuffle(pool)
        chosen = (chosen + pool)[: max(v.digital_users, len(chosen))]
        chosen.sort()
        self.users: list[dict[str, Any]] = []
        for c in chosen:
            base = self.at(self.sample_date(self.created[c], self.today, 0.8), HOURS_DIGITAL)
            reg = base
            if c in self.app_first:
                started, completed = self.app_first[c]
                reg = base if base < started else completed + timedelta(minutes=rng.randint(2, 30))
            status = self.pick(["A", "L", "D"], [96, 3, 1])
            self.users.append(
                {
                    "user_id": f"usr{self.cust_no[c]}",
                    "cust": c,
                    "type": "CORPORATE" if self.segments[c] == "C" else "RETAIL",
                    "status": status,
                    "registered_at": min(reg, self.now - timedelta(hours=2)),
                    "failed": rng.randint(3, 5) if status == "L" else rng.choice([0, 0, 0, 1, 2]),
                }
            )

    def sessions(self) -> None:
        v, rng = self.vol, self.rng
        n_active = rng.randint(*v.active_sessions)
        weights = [
            rng.lognormvariate(0, 1.0) * (1.5 if u["type"] == "CORPORATE" else 1.0)
            for u in self.users
        ]
        idx = rng.choices(range(len(self.users)), weights=weights, k=v.sessions - n_active)
        rows: list[Row] = []
        self.cust_sessions: dict[int, list[_Session]] = defaultdict(list)
        last_login: dict[int, datetime] = {}
        cutoff = self.now - timedelta(minutes=20)
        for i in idx:
            u = self.users[i]
            reg: datetime = u["registered_at"]
            d = self.sample_date(reg.date(), self.today, growth=1.0)
            login = self.at(d, HOURS_DIGITAL)
            if login < reg:
                login = reg + timedelta(minutes=rng.uniform(1, 600))
            mobile = rng.random() < 0.55 + 0.25 * self.frac(login.date())
            ch = "MOBILE" if mobile else "WEB"
            os_ = (
                self.pick(["IOS", "ANDROID"], [45, 55])
                if mobile
                else self.pick(["WINDOWS", "MACOS"], [70, 30])
            )
            sid = self.uuid4()
            if rng.random() < 0.06:
                reason = self.pick(["BAD_PASSWORD", "OTP_FAIL", "LOCKED"], [60, 30, 10])
                if login > cutoff:
                    login = cutoff - timedelta(minutes=rng.uniform(1, 60))
                rows.append((sid, u["user_id"], ch, os_, login, login, None, "F", reason))
                continue
            dur = rng.lognormvariate(math.log(6 if mobile else 10), 0.7)
            last = login + timedelta(minutes=dur)
            if last > cutoff:  # keep the "active right now" set exactly the planned size
                shift = last - cutoff + timedelta(minutes=rng.uniform(5, 60))
                login, last = login - shift, last - shift
            logout = last + timedelta(seconds=rng.uniform(5, 60)) if rng.random() < 0.85 else None
            rows.append((sid, u["user_id"], ch, os_, login, last, logout, "S", None))
            self.cust_sessions[u["cust"]].append(_Session(login, last, sid, ch))
            last_login[i] = max(last_login.get(i, login), login)
        active_users = [i for i, u in enumerate(self.users) if u["status"] == "A"]
        for i in rng.sample(active_users, min(n_active, len(active_users))):
            u = self.users[i]
            login = self.now - timedelta(minutes=rng.uniform(1, 90))
            last = max(login, self.now - timedelta(minutes=rng.uniform(0, 14)))
            mobile = rng.random() < 0.8
            ch = "MOBILE" if mobile else "WEB"
            os_ = (
                self.pick(["IOS", "ANDROID"], [45, 55])
                if mobile
                else self.pick(["WINDOWS", "MACOS"], [70, 30])
            )
            sid = self.uuid4()
            rows.append((sid, u["user_id"], ch, os_, login, last, None, "S", None))
            self.cust_sessions[u["cust"]].append(_Session(login, last, sid, ch))
            last_login[i] = max(last_login.get(i, login), login)
        rows.sort(key=lambda r: r[4])
        self.tables["obdx.digx_user_session"] = rows
        for s in self.cust_sessions.values():
            s.sort(key=lambda x: x.login_at)
        self.tables["obdx.digx_user"] = [
            (
                u["user_id"],
                self.cust_no[u["cust"]],
                u["type"],
                u["status"],
                u["registered_at"],
                last_login.get(i),
                u["failed"],
            )
            for i, u in enumerate(self.users)
        ]

    # ---- core transactions -------------------------------------------------
    def transactions(self) -> None:
        v, rng = self.vol, self.rng
        seg_w = {"R": 1.0, "P": 1.5, "C": 4.0}
        weights = []
        for a in self.accounts:
            if a.auth_stat == "U":
                weights.append(0.0)
            elif ref.FAMILY[a.cls] == "TD":
                weights.append(0.05)
            else:
                weights.append(seg_w[self.segments[a.cust]] * rng.lognormvariate(0, 0.8))
        picks = rng.choices(self.accounts, weights=weights, k=v.transactions)
        login_keys = {c: [s.login_at for s in ss] for c, ss in self.cust_sessions.items()}
        codes = {
            "BRANCH": (
                ["CSH1", "CSH2", "TRF1", "TRF2", "TRF3", "TRF4", "CHG1", "CHG2", "SAL1"],
                [25, 25, 8, 8, 7, 4, 8, 5, 10],
            ),
            "OBDX": (["TRF1", "TRF2", "TRF4", "BIL1", "BIL2", "BIL3"], [25, 30, 5, 15, 15, 10]),
            "ATM": (["CSH1", "CSH2"], [85, 15]),
            "POS": (["CRD1", "CRD2"], [95, 5]),
            "TD": (["TRF1", "CSH2", "CHG1"], [50, 40, 10]),
        }
        rows: list[Row] = []
        for n, a in enumerate(picks):
            hi = a.closed_date or self.today
            d = self.sample_date(a.open_date, hi, growth=0.5)
            f = self.frac(d)
            sid: uuid.UUID | None = None
            if ref.FAMILY[a.cls] == "TD":
                channel, bucket = "BRANCH", "TD"
            else:
                bucket = self.pick(
                    ["OBDX", "BRANCH", "ATM", "POS"],
                    [0.27 + 0.21 * f, 0.25 - 0.12 * f, 0.20 - 0.04 * f, 0.28 - 0.05 * f],
                )
                channel = bucket
            ts: datetime | None = None
            if bucket == "OBDX":
                sess = self._session_near(a, d, hi, login_keys)
                if sess is None:
                    channel = bucket = "BRANCH"
                else:
                    span = max(30.0, (sess.last_activity_at - sess.login_at).total_seconds())
                    ts = sess.login_at + timedelta(seconds=rng.uniform(15, span))
                    ts = min(ts, self.now)
                    channel = "OBDX_MOB" if sess.channel == "MOBILE" else "OBDX_WEB"
                    sid = sess.session_id
            if ts is None:
                hours = HOURS_BRANCH if channel == "BRANCH" else HOURS_CARD
                ts = self.at(d, hours)
            code = self.pick(*codes[bucket])
            status = self.pick(["P", "F", "R"], [97, 2, 1])
            rows.append(
                (
                    f"FT{n + 1:014d}",
                    a.account_no,
                    code,
                    ref.TXN_DRCR[code],
                    self._amount(code),
                    ts.astimezone(self.tz).date(),
                    ts,
                    channel,
                    status,
                    sid,
                )
            )
        rows.sort(key=lambda r: r[6])
        self.tables["core.transaction"] = rows

    def _session_near(
        self, a: _Account, d: date, hi: date, keys: dict[int, list[datetime]]
    ) -> _Session | None:
        sess = self.cust_sessions.get(a.cust)
        if not sess:
            return None
        target = datetime.combine(d, time(12), tzinfo=self.tz)
        i = bisect_left(keys[a.cust], target)
        for j in (i, i - 1, i + 1, i - 2):
            if 0 <= j < len(sess):
                day = sess[j].login_at.astimezone(self.tz).date()
                if a.open_date <= day <= hi:
                    return sess[j]
        return None

    def _amount(self, code: str) -> float:
        med = {
            "TRF1": 150,
            "TRF2": 120,
            "TRF3": 200,
            "TRF4": 400,
            "SAL1": 900,
            "BIL1": 25,
            "BIL2": 15,
            "BIL3": 180,
            "CSH1": 60,
            "CSH2": 150,
            "CRD1": 12,
            "CRD2": 20,
            "CHG1": 2,
            "CHG2": 1,
        }[code]
        sigma = 0.15 if code.startswith("CHG") else 0.8
        return round(med * self.rng.lognormvariate(0, sigma), 3)

    # ---- Atumverse ---------------------------------------------------------
    def _dwell(self, stage: str, wf: str) -> tuple[float, bool]:
        sla = ref.STAGE_INFO[(wf, stage)][5]
        if sla is None:  # START stage: no SLA
            return (
                self.rng.uniform(0.1, 6.0) if wf == "LOAN_ORIG" else self.rng.uniform(0.05, 1.0)
            ), False
        if self.rng.random() < ref.SLA_BREACH_P[stage]:
            return sla * (1 + self.rng.expovariate(1 / 0.6)), True
        return sla * self.rng.uniform(0.1, 0.95), False

    def _walk(
        self, wf: str, stop: str | None, stop_action: str | None, end: str | None
    ) -> list[_Visit]:
        """Stage visits up to `stop`. stop_action None = in progress at `stop`."""
        main = ref.LOAN_MAIN if wf == "LOAN_ORIG" else ref.CASA_MAIN
        default = {"START": "SUBMIT", "SYSTEM": "AUTO", "TASK": "SUBMIT", "APPROVAL": "APPROVE"}
        visits: list[_Visit] = []

        def closed(stage: str, action: str, event: dict[str, Any] | None = None) -> None:
            h, b = self._dwell(stage, wf)
            visits.append(_Visit(stage, h, action, b, event))

        for stage in main:
            if stage == "DOC_VERIFY":
                p = 0.08
                while self.rng.random() < p:  # sent back to the customer, re-enters DOC_VERIFY
                    reason = self.pick(
                        ["MISSING_DOC", "ILLEGIBLE_DOC", "SALARY_MISMATCH"], [55, 25, 20]
                    )
                    closed("DOC_VERIFY", "RETURN", {"returnReason": reason})
                    p = 0.04
            if stage == stop:
                if stop_action is None:
                    visits.append(_Visit(stage, None, None, None))
                    return visits
                closed(stage, stop_action)
                break
            stype = ref.STAGE_INFO[(wf, stage)][4]
            closed(stage, default[stype])
        visits.append(_Visit(end or "CLOSED", None, None, None))
        return visits

    def _schedule_forward(self, visits: list[_Visit], t0: datetime) -> None:
        t = t0
        for vis in visits:
            vis.entered = t
            if vis.dwell_h is not None:
                t = t + timedelta(hours=vis.dwell_h)
                vis.exited = t

    def _total(self, visits: list[_Visit]) -> timedelta:
        return timedelta(hours=sum(v.dwell_h or 0.0 for v in visits))

    def _place_terminal(self, case: _Case) -> None:
        total = self._total(case.visits)
        latest = self.now - total - timedelta(hours=1)
        d = self.sample_date(self.start, latest.date(), growth=0.5)
        t0 = self.at(d, HOURS_BRANCH)
        if t0 > latest:
            t0 = latest - timedelta(minutes=self.rng.uniform(5, 600))
        self._schedule_forward(case.visits, t0)
        case.created_at = t0

    def _place_in_progress(self, case: _Case) -> None:
        cur = case.visits[-1]
        sla = ref.STAGE_INFO[(case.wf, cur.stage)][5] or 24
        breach = self.rng.random() < ref.SLA_BREACH_P.get(cur.stage, 0.1)
        sitting = (
            sla * (1 + self.rng.expovariate(1 / 0.8))
            if breach
            else sla * self.rng.uniform(0.05, 0.9)
        )
        t0 = self.now - timedelta(hours=sitting) - self._total(case.visits)
        self._schedule_forward(case.visits, t0)
        case.created_at = t0

    def atumverse(self) -> None:
        v, rng = self.vol, self.rng
        retail = [c for c in range(v.customers) if self.segments[c] != "C"]
        cases: list[_Case] = []
        # LOAN_ORIG: 18% in progress, the rest terminal
        n_prog = round(v.loan_instances * 0.18)
        for i in range(v.loan_instances):
            c = rng.choice(retail)
            branch = self.home[c] if rng.random() < 0.9 else rng.choice(ref.ALL_BRANCHES)
            if i < n_prog:
                stage = self.pick(ref.LOAN_MAIN, [10, 5, 25, 30, 15, 12, 3])
                case = _Case(
                    "LOAN_ORIG",
                    "IN_PROGRESS",
                    self._walk("LOAN_ORIG", stage, None, None),
                    c,
                    branch,
                )
                self._place_in_progress(case)
            else:
                outcome = self.pick(
                    ["COMPLETED", "REJ_KYC", "REJ_APPROVAL", "CXL_DRAFT", "CXL_OFFER"],
                    [62, 3, 22, 5, 8],
                )
                stop, act, end = {
                    "COMPLETED": (None, None, "CLOSED"),
                    "REJ_KYC": ("KYC_CHECK", "REJECT", "REJECTED"),
                    "REJ_APPROVAL": ("APPROVAL", "REJECT", "REJECTED"),
                    "CXL_DRAFT": ("DRAFT", "CANCEL", "CANCELLED"),
                    "CXL_OFFER": ("OFFER_ACCEPT", "CANCEL", "CANCELLED"),
                }[outcome]
                status = {"CLOSED": "COMPLETED", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED"}[
                    end
                ]
                case = _Case(
                    "LOAN_ORIG", status, self._walk("LOAN_ORIG", stop, act, end), c, branch
                )
                self._place_terminal(case)
            cases.append(case)
        # CASA_ONBOARD: completed cases reconcile with real branch-opened CASA accounts
        cands = [
            a
            for a in self.accounts
            if a.auth_stat == "A" and not a.digital and ref.FAMILY[a.cls] == "CASA"
        ]
        n_prog = round(v.casa_instances * 0.10)
        n_rej, n_cxl = round(v.casa_instances * 0.04), round(v.casa_instances * 0.02)
        n_done = min(len(cands), v.casa_instances - n_prog - n_rej - n_cxl)
        for a in rng.sample(cands, n_done):
            case = _Case(
                "CASA_ONBOARD",
                "COMPLETED",
                self._walk("CASA_ONBOARD", None, None, "CLOSED"),
                a.cust,
                a.branch,
                account_no=a.account_no,
                account_cls=a.cls,
            )
            # ACCOUNT_CREATE exits on the core account's open date
            anchor = self.at(a.open_date, HOURS_BRANCH)
            self._schedule_forward(case.visits, anchor - self._total(case.visits))
            case.created_at = case.visits[0].entered or anchor
            cases.append(case)
        for _ in range(v.casa_instances - n_done - n_rej - n_cxl):
            c = rng.choice(range(v.customers))
            stage = self.pick(ref.CASA_MAIN, [25, 10, 15, 45, 5])
            case = _Case(
                "CASA_ONBOARD",
                "IN_PROGRESS",
                self._walk("CASA_ONBOARD", stage, None, None),
                c,
                self.home[c],
            )
            self._place_in_progress(case)
            cases.append(case)
        for k in range(n_rej + n_cxl):
            c = rng.choice(range(v.customers))
            if k < n_rej:
                stop = self.pick(["EKYC", "AML_SCREEN", "CHECKER_APPROVAL"], [30, 30, 40])
                visits = self._walk("CASA_ONBOARD", stop, "REJECT", "REJECTED")
                status = "REJECTED"
            else:
                visits = self._walk("CASA_ONBOARD", "DRAFT", "CANCEL", "CANCELLED")
                status = "CANCELLED"
            case = _Case("CASA_ONBOARD", status, visits, c, self.home[c])
            self._place_terminal(case)
            cases.append(case)

        cases.sort(key=lambda x: x.created_at)
        loans = [x for x in cases if x.wf == "LOAN_ORIG"]
        v1_cut = loans[int(len(loans) * 0.30)].created_at if loans else self.now
        corrupt = set(rng.sample(range(len(cases)), max(1, round(len(cases) * 0.0025))))
        inst_rows: list[Row] = []
        hist_rows: list[Row] = []
        doc_cands: list[tuple[str, str, int, datetime]] = []
        loan_no = casa_no = 0
        for n, case in enumerate(cases):
            iid = f"WI{n + 1:014d}"
            if case.wf == "LOAN_ORIG":
                loan_no += 1
                ref_no = f"LN-{loan_no:06d}"
                version = 1 if case.created_at < v1_cut else 2
                doc = self._loan_payload(case, version)
            else:
                casa_no += 1
                ref_no = f"CA-{casa_no:06d}"
                version = 1
                doc = self._casa_payload(case)
            fmt = self.pick(["JSON", "BLOB_GZIP", "RAW_TEXT"], [60, 25, 15])
            pj, pb, pr = self._encode(doc, fmt, n in corrupt)
            last = case.visits[-1]
            done = last.entered if case.outcome != "IN_PROGRESS" else None
            if last.entered is None:
                raise RuntimeError(f"unscheduled case {iid}")
            inst_rows.append(
                (
                    iid,
                    case.wf,
                    version,
                    ref_no,
                    self.cust_no[case.cust],
                    case.branch,
                    last.stage,
                    case.outcome,
                    case.created_at,
                    last.entered,
                    done,
                    fmt,
                    pj,
                    pb,
                    pr,
                )
            )
            for vis in case.visits:
                role = ref.STAGE_ACTOR_ROLE.get(vis.stage) if vis.exited else None
                user = None
                if role == "SYSTEM":
                    user = "system"
                elif role == "CUSTOMER":
                    user = f"cust_{self.cust_no[case.cust]}"
                elif role:
                    user = f"{role.lower()}_{rng.randint(1, 12):02d}"
                breached = vis.breached if vis.exited else None
                hist_rows.append(
                    (
                        iid,
                        vis.stage,
                        vis.entered,
                        vis.exited,
                        vis.action,
                        user,
                        role,
                        breached,
                        vis.event,
                    )
                )
            past_draft = any(x.stage == "DRAFT" and x.action == "SUBMIT" for x in case.visits)
            if past_draft:
                kinds = (
                    ["CIVIL_ID", "SALARY_CERT", "BANK_STMT"]
                    if case.wf == "LOAN_ORIG"
                    else ["CIVIL_ID"]
                )
                for kind in kinds:
                    doc_cands.append((iid, kind, case.cust, case.created_at))
        self.tables["atum.wf_instance"] = inst_rows
        self.tables["atum.wf_stage_history"] = hist_rows
        self._documents(doc_cands)

    def _loan_payload(self, case: _Case, version: int) -> dict[str, Any]:
        rng = self.rng
        emp = self.pick(["SALARIED", "GOVERNMENT", "SELF_EMP"], [50, 35, 15])
        salary = round(min(8000.0, max(350.0, 1300 * rng.lognormvariate(0, 0.5))), 3)
        product = "PL_GOV" if emp == "GOVERNMENT" else self.pick(["PL_STD", "PL_PREM"], [80, 20])
        purpose = self.pick(
            ["CAR", "HOME_RENO", "EDUCATION", "TRAVEL", "MEDICAL", "DEBT_CONSOL", "WEDDING"],
            [30, 20, 10, 10, 8, 12, 10],
        )
        requested = float(min(25000, max(1000, round(salary * rng.uniform(5, 20) / 500) * 500)))
        tenor = rng.choice([12, 24, 36, 48, 60, 72])
        assessed = any(v.stage == "CREDIT_ASSESS" and v.action == "SUBMIT" for v in case.visits)
        decision = next(
            (
                v.action
                for v in case.visits
                if v.stage == "APPROVAL" and v.action in ("APPROVE", "REJECT")
            ),
            None,
        )
        score = rng.randint(620, 820) if decision != "REJECT" else rng.randint(520, 700)
        dbr = round(rng.uniform(0.15, 0.45) if decision != "REJECT" else rng.uniform(0.35, 0.6), 2)
        approved = float(round(requested * rng.choice([1, 1, 1, 0.9, 0.8]) / 500) * 500)
        rate = round(rng.uniform(4.5, 7.0), 2)
        if version == 1:  # flat shape
            doc: dict[str, Any] = {
                "employmentType": emp,
                "monthlySalary": salary,
                "product": product,
                "loanAmount": requested,
                "tenorMonths": tenor,
                "purpose": purpose,
            }
            if assessed:
                doc.update({"creditScore": score, "dbr": dbr})
            if decision:
                doc["decision"] = decision
            if decision == "APPROVE":
                doc.update({"amountApproved": approved, "ratePct": rate})
            return doc
        doc = {
            "applicant": {"employmentType": emp, "monthlySalary": salary},
            "loan": {
                "product": product,
                "amountRequested": requested,
                "tenorMonths": tenor,
                "purpose": purpose,
            },
        }
        if assessed:
            doc["credit"] = {"score": score, "dbr": dbr}
            if decision:
                doc["credit"]["decision"] = decision
        if decision == "APPROVE":
            doc["offer"] = {"amountApproved": approved, "ratePct": rate}
        return doc

    def _casa_payload(self, case: _Case) -> dict[str, Any]:
        rng = self.rng
        emp = self.pick(["SALARIED", "GOVERNMENT", "SELF_EMP", "STUDENT"], [45, 35, 12, 8])
        salary = round(min(8000.0, max(0.0, 1100 * rng.lognormvariate(0, 0.6))), 3)
        cls = case.account_cls or self.pick(
            ["SAVREG", "SALACC", "CURIND", "SAVPLS"], [40, 30, 20, 10]
        )
        stages = {v.stage: v.action for v in case.visits if v.exited}
        return {
            "applicant": {"employmentType": emp, "monthlySalary": salary},
            "account": {"product": cls, "currency": "KWD", "accountNo": case.account_no},
            "screening": {
                "ekyc": "FAIL"
                if stages.get("EKYC") == "REJECT"
                else ("PASS" if "EKYC" in stages else None),
                "aml": "HIT"
                if stages.get("AML_SCREEN") == "REJECT"
                else ("CLEAR" if "AML_SCREEN" in stages else None),
            },
        }

    def _encode(
        self, doc: dict[str, Any], fmt: str, corrupt: bool
    ) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
        text = json.dumps(doc, separators=(",", ":"))
        if fmt == "JSON":
            if corrupt:  # valid JSON, wrong shape (schema drift)
                return {"applicant": "<<unavailable>>", "loan": [], "account": 0}, None, None
            return doc, None, None
        if fmt == "BLOB_GZIP":
            if corrupt:  # gzip of a truncated document, or not gzip at all
                if self.rng.random() < 0.5:
                    return None, gzip.compress(text[: len(text) // 2].encode(), mtime=0), None
                return None, b"\x00BAD" + self.rng.randbytes(24), None
            return None, gzip.compress(text.encode(), mtime=0), None
        if corrupt:
            return None, None, text[: len(text) // 2]
        return None, None, text

    def _documents(self, cands: list[tuple[str, str, int, datetime]]) -> None:
        rng = self.rng
        if len(cands) > self.vol.documents:
            keep = sorted(rng.sample(range(len(cands)), self.vol.documents))
            cands = [cands[i] for i in keep]
        rows: list[Row] = []
        customers = self.tables["core.customer"]
        for n, (iid, kind, c, created) in enumerate(cands):
            doc_id = f"DOC{n + 1:013d}"
            content = f"%PDF-1.4\n% synthetic placeholder {doc_id}\n%%EOF\n".encode()
            mime = "image/jpeg" if kind == "CIVIL_ID" and rng.random() < 0.3 else "application/pdf"
            ocr: dict[str, Any] | None = None
            if rng.random() < 0.70:
                cust = customers[c]
                if kind == "CIVIL_ID":
                    ocr = {
                        "documentType": kind,
                        "civilIdNumber": cust[2],
                        "nameEn": cust[1],
                        "expiryDate": str(self.today + timedelta(days=rng.randint(30, 1800))),
                    }
                elif kind == "SALARY_CERT":
                    ocr = {
                        "documentType": kind,
                        "employer": self.fake.company(),
                        "monthlySalary": round(rng.uniform(400, 5000), 3),
                    }
                else:
                    ocr = {
                        "documentType": kind,
                        "months": 6,
                        "averageBalance": round(rng.uniform(100, 20000), 3),
                    }
            uploaded = min(self.now, created + timedelta(minutes=rng.uniform(1, 120)))
            rows.append((doc_id, iid, kind, mime, content, len(content), uploaded, ocr))
        self.tables["atum.wf_document"] = rows

    # ---- entry point -------------------------------------------------------
    def run(self) -> dict[str, list[Row]]:
        self.reference()
        self.customers_and_accounts()
        self.applications()
        self.digital_users()
        self.sessions()
        self.transactions()
        self.atumverse()
        return self.tables
