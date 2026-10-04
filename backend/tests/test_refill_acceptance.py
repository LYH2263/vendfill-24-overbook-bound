"""验收：合法连开两张 / 非法点位拒绝互斥写库 / 失败不留半张 / 改库存后重算 / 裁回种子。

种子（点位 id=1，6 条货道）：
  A1 矿泉水 cap20 stock5  transit0 -> gap15 待补 15
  A2 可乐   cap18 stock18 transit0 -> gap0  满仓
  B1 薯片   cap12 stock3  transit2 -> gap7  待补 7
  B2 巧克力 cap15 stock10 transit5 -> gap0  满仓
  C1 能量棒 cap10 stock0  transit0 -> gap10 待补 10
  C2 口香糖 cap24 stock24 transit2 -> gap-2 超占
=> total_fill=32, need=3, full=2, overbooked=1
"""
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models.models import Lane, Location, RefillOrder, Sale

SEED_TOTAL = 32
SEED_FULL_SLOTS = ["A2", "B2"]


def order_count(SessionLocal, location_id=1):
    db = SessionLocal()
    try:
        return db.scalar(
            select(func.count()).select_from(RefillOrder).where(RefillOrder.location_id == location_id)
        )
    finally:
        db.close()


def test_two_consecutive_runs_each_persist_complete_own_snapshot(ctx):
    client, SessionLocal, _engine = ctx
    r1 = client.post("/api/refills/run?location_id=1")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["id"] == 1 and d1["total_fill"] == SEED_TOTAL and len(d1["lines"]) == 6

    r2 = client.post("/api/refills/run?location_id=1")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["id"] == 2 and d2["total_fill"] == SEED_TOTAL and len(d2["lines"]) == 6

    # 两张都是当时货道现算后整单落下的完整快照，互不夹带。
    rows = client.get("/api/refills?location_id=1").json()
    assert [r["id"] for r in rows] == [2, 1]
    assert all(r["total_fill"] == SEED_TOTAL for r in rows)
    db = SessionLocal()
    try:
        for oid in (1, 2):
            data = json.loads(db.get(RefillOrder, oid).lines_json)
            assert len(data["lines"]) == 6 and data["total_fill"] == SEED_TOTAL
    finally:
        db.close()


def test_unknown_location_rejects_and_writes_nothing(ctx):
    client, SessionLocal, _engine = ctx
    client.post("/api/refills/run?location_id=1")  # 先有一张合法单
    assert order_count(SessionLocal) == 1

    # 非法点位：生成与各读入口全部 404。
    assert client.post("/api/refills/run?location_id=999").status_code == 404
    assert client.get("/api/refills?location_id=999").status_code == 404
    assert client.get("/api/refills/latest?location_id=999").status_code == 404
    assert client.get("/api/refills/summary?location_id=999").status_code == 404
    assert client.get("/api/refills/full?location_id=999").status_code == 404

    # 非法点位一张单都不许落。
    assert order_count(SessionLocal, 999) == 0

    # 合法点位旧单仍在，汇总总补件数与满仓名单都不得因非法请求变化。
    assert order_count(SessionLocal) == 1
    s = client.get("/api/refills/summary?location_id=1").json()
    assert s["total_fill"] == SEED_TOTAL and s["full_count"] == 2
    full = client.get("/api/refills/full?location_id=1").json()["lanes"]
    assert [l["slot_no"] for l in full] == SEED_FULL_SLOTS


def test_failed_generation_leaves_no_half_order(ctx):
    client, SessionLocal, engine = ctx
    flag = {"fail": True}

    class FailingSession(Session):
        def commit(self):
            if flag["fail"]:
                raise RuntimeError("simulated commit failure")
            return super().commit()

    def override():
        db = FailingSession(bind=engine)
        try:
            yield db
        finally:
            db.close()

    original_override = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = override
    try:
        r = client.post("/api/refills/run?location_id=1")
        assert r.status_code == 500
        assert order_count(SessionLocal) == 0  # 没有半张单残留
    finally:
        app.dependency_overrides[get_db] = original_override

    # 故障消除后必须能正常整单落下。
    r = client.post("/api/refills/run?location_id=1")
    assert r.status_code == 200 and r.json()["total_fill"] == SEED_TOTAL
    assert order_count(SessionLocal) == 1
    assert len(r.json()["lines"]) == 6


def test_second_run_after_stock_change_follows_new_stock(ctx):
    client, SessionLocal, _engine = ctx
    d1 = client.post("/api/refills/run?location_id=1").json()
    assert d1["total_fill"] == SEED_TOTAL

    # 改库存：A1 5->0（缺口 15->20），C1 0->10（缺口 10->0，转满仓）。
    db = SessionLocal()
    try:
        a1 = db.scalars(select(Lane).where(Lane.slot_no == "A1")).one()
        c1 = db.scalars(select(Lane).where(Lane.slot_no == "C1")).one()
        a1.stock = 0
        c1.stock = 10
        db.commit()
    finally:
        db.close()

    d2 = client.post("/api/refills/run?location_id=1").json()
    assert d2["id"] == 2
    assert d2["total_fill"] == 27          # 20 + 7 + 0
    assert d2["need_fill_count"] == 2
    assert d2["full_count"] == 3
    line_a1 = next(l for l in d2["lines"] if l["slot_no"] == "A1")
    assert line_a1["stock"] == 0 and line_a1["gap"] == 20 and line_a1["fill_qty"] == 20

    # 第二张绝不能等于改库存前那张；第一张旧快照保持改前数字不变。
    assert d2["total_fill"] != d1["total_fill"]
    rows = client.get("/api/refills?location_id=1").json()
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["total_fill"] == SEED_TOTAL
    assert by_id[2]["total_fill"] == 27
    latest = client.get("/api/refills/latest?location_id=1").json()
    assert latest["id"] == 2 and latest["total_fill"] == 27


def test_read_endpoints_never_auto_generate_an_order(ctx):
    client, SessionLocal, _engine = ctx
    # 没有单时：读接口给空结果，而不是替用户偷偷生成一张来凑名单。
    assert client.get("/api/refills/latest?location_id=1").json()["id"] is None
    s = client.get("/api/refills/summary?location_id=1").json()
    assert s["total_fill"] == 0 and s["full_count"] == 0
    assert client.get("/api/refills/full?location_id=1").json()["lanes"] == []
    assert client.get("/api/refills?location_id=1").json() == []
    assert order_count(SessionLocal) == 0  # 一连串读之后依旧零单

    # 真正生成后，满仓页与汇总才反映已提交的单。
    client.post("/api/refills/run?location_id=1")
    assert [l["slot_no"] for l in client.get("/api/refills/full?location_id=1").json()["lanes"]] == SEED_FULL_SLOTS
    assert client.get("/api/refills/summary?location_id=1").json()["total_fill"] == SEED_TOTAL


def test_prune_orders_back_to_seed_restores_green_without_wiping_business_tables(ctx):
    client, SessionLocal, _engine = ctx
    client.post("/api/refills/run?location_id=1")
    client.post("/api/refills/run?location_id=1")
    assert order_count(SessionLocal) == 2

    # 只裁补货单回种子行数（0 张），禁止把点位/货道/销量整表删光再假装绿。
    db = SessionLocal()
    try:
        db.query(RefillOrder).delete()
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert db.scalar(select(func.count()).select_from(RefillOrder)) == 0
        assert db.scalar(select(func.count()).select_from(Location)) == 1
        assert db.scalar(select(func.count()).select_from(Lane)) == 6
        assert db.scalar(select(func.count()).select_from(Sale)) == 6
    finally:
        db.close()

    # 裁完即绿：满仓页空名单、汇总为零，且读入口不会再生单。
    assert client.get("/api/refills/full?location_id=1").json()["lanes"] == []
    assert client.get("/api/refills/summary?location_id=1").json()["total_fill"] == 0
    assert client.get("/api/refills?location_id=1").json() == []

    # 货道种子仍在 -> 重新生成立刻得到与种子一致的完整单。
    d = client.post("/api/refills/run?location_id=1").json()
    assert d["total_fill"] == SEED_TOTAL and d["full_count"] == 2 and len(d["lines"]) == 6
