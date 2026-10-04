"""补货生成的互斥/原子/只读入口验收。

绿仓种子：1 个点位、6 条货道、6 条销量，0 张补货单。
- 合法点位连续生成：各按当时货道现算，各落一张快照单
- 点位不存在：404 拒绝，补货单列表/汇总/满仓都不得增加
- 先成功后非法：旧单旧汇总仍在，非法不得再增单
- 生成失败：整体回滚，不留半张单
- latest 无单时 404，绝不靠"顺手再生成一张"凑名单
- full/summary 实时只读计算，不写库
- 裁单只能动 refill_orders，裁回绿仓种子行数
- 改库存后第二次生成必须跟新库存
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import Lane, Location, RefillOrder, Sale

# 与 seed.py 完全一致的绿仓种子行
SEED_LANES = [
    ("A1", "矿泉水", 20, 5, 0),
    ("A2", "可乐", 18, 18, 0),
    ("B1", "薯片", 12, 3, 2),
    ("B2", "巧克力", 15, 10, 5),
    ("C1", "能量棒", 10, 0, 0),
    ("C2", "口香糖", 24, 24, 2),
]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    from sqlalchemy.orm import Session

    db = Session(engine)
    loc = Location(code="VM-01", name="地铁口 A 点位", address="城东地铁 1 号口")
    db.add(loc)
    db.flush()
    for slot, sku, cap, stock, transit in SEED_LANES:
        db.add(Lane(location_id=loc.id, slot_no=slot, sku_name=sku,
                    capacity=cap, stock=stock, in_transit=transit))
        db.flush()
    lane_ids = [r.id for r in db.scalars(select(Lane).order_by(Lane.slot_no)).all()]
    for i, lid in enumerate(lane_ids):
        db.add(Sale(lane_id=lid, qty=2 + i))
    db.commit()

    def override_get_db():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    return TestClient(app)


def _count(engine, model):
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


def _lane_id_by_slot(engine, slot):
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        return s.scalar(select(Lane.id).where(Lane.slot_no == slot))


def test_seed_totals_and_two_consecutive_runs_each_persisted(client, db_session):
    # 连续两次合法生成：各落一张，且都按当时货道现算
    r1 = client.post("/api/refills/run?location_id=1")
    r2 = client.post("/api/refills/run?location_id=1")
    assert r1.status_code == 200 and r2.status_code == 200
    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert id1 != id2
    assert _count(db_session, RefillOrder) == 2

    # 列表里两张单都在，合计一致
    lst = client.get("/api/refills?location_id=1").json()
    assert [o["id"] for o in lst] == [id1, id2]
    for o in lst:
        assert o["total_fill"] == 32  # 15 + 7 + 10
        assert o["need_fill_count"] == 3
        assert o["full_count"] == 2
        assert o["overbooked_count"] == 1

    # 逐单取快照，两张都是各自当时的完整明细，不是半张
    for oid in (id1, id2):
        d = client.get(f"/api/refills/{oid}").json()
        assert len(d["lines"]) == 6
        assert d["total_fill"] == 32


def test_nonexistent_location_rejected_without_any_write(client, db_session):
    assert _count(db_session, RefillOrder) == 0
    r = client.post("/api/refills/run?location_id=999")
    assert r.status_code == 404
    # 列表、汇总、满仓对不存在点位同样拒绝
    assert client.get("/api/refills?location_id=999").status_code == 404
    assert client.get("/api/refills/latest?location_id=999").status_code == 404
    assert client.get("/api/refills/summary?location_id=999").status_code == 404
    assert client.get("/api/refills/full?location_id=999").status_code == 404
    # 任何地方都不得多出补货单
    assert _count(db_session, RefillOrder) == 0


def test_success_then_illegal_keeps_old_order_and_summary(client, db_session):
    ok = client.post("/api/refills/run?location_id=1")
    assert ok.status_code == 200
    old_id = ok.json()["id"]
    old_summary = client.get("/api/refills/summary?location_id=1").json()

    bad = client.post("/api/refills/run?location_id=999")
    assert bad.status_code == 404

    # 旧单仍在，且只有那一张
    lst = client.get("/api/refills?location_id=1").json()
    assert [o["id"] for o in lst] == [old_id]
    # 旧汇总数字不变
    assert client.get("/api/refills/summary?location_id=1").json() == old_summary
    # 最近单仍是旧那张
    assert client.get("/api/refills/latest?location_id=1").json()["id"] == old_id


def test_failed_run_leaves_no_half_order(client, db_session, monkeypatch):
    # 计算阶段抛错：事务必须整体回滚，库里不留半张单
    import app.api.refills as refills_mod

    def boom(_lines):
        raise RuntimeError("simulated compute failure")

    monkeypatch.setattr(refills_mod, "summarize", boom)
    r = client.post("/api/refills/run?location_id=1")
    assert r.status_code == 500
    assert _count(db_session, RefillOrder) == 0

    monkeypatch.undo()
    # 回滚后同点位仍可正常生成
    assert client.post("/api/refills/run?location_id=1").status_code == 200
    assert _count(db_session, RefillOrder) == 1


def test_latest_full_summary_never_generate_orders(client, db_session):
    # 一张单都没有时：latest 如实 404，而不是偷偷生成一张
    r = client.get("/api/refills/latest?location_id=1")
    assert r.status_code == 404
    assert _count(db_session, RefillOrder) == 0

    # 满仓页与汇总无单也能看（按当前货道实时算），且不产生任何单
    full = client.get("/api/refills/full?location_id=1")
    summ = client.get("/api/refills/summary?location_id=1")
    assert full.status_code == 200 and summ.status_code == 200
    slots = sorted(l["slot_no"] for l in full.json()["lanes"])
    assert slots == ["A2", "B2"]
    assert summ.json()["total_fill"] == 32
    assert summ.json()["full_count"] == 2
    assert _count(db_session, RefillOrder) == 0


def test_full_list_and_summary_follow_live_lanes_without_order(client, db_session):
    # 把满仓货道 A2 改出缺口：满仓名单应立即少一条，且全程不落单
    a2 = _lane_id_by_slot(db_session, "A2")
    patched = client.patch(f"/api/lanes/{a2}", json={"stock": 17})
    assert patched.status_code == 200
    full = client.get("/api/refills/full?location_id=1").json()
    assert [l["slot_no"] for l in full["lanes"]] == ["B2"]
    summ = client.get("/api/refills/summary?location_id=1").json()
    assert summ["full_count"] == 1
    assert summ["total_fill"] == 33  # A2 多出 1 件缺口
    assert _count(db_session, RefillOrder) == 0


def test_second_run_after_stock_change_follows_new_stock(client, db_session):
    r1 = client.post("/api/refills/run?location_id=1").json()
    c1 = _lane_id_by_slot(db_session, "C1")

    # C1 补满：缺口 10 -> 0
    assert client.patch(f"/api/lanes/{c1}", json={"stock": 10}).status_code == 200
    r2 = client.post("/api/refills/run?location_id=1").json()

    assert r1["id"] != r2["id"]
    assert r2["total_fill"] != r1["total_fill"]
    assert r1["total_fill"] == 32 and r2["total_fill"] == 22

    def line(order, lane_id):
        return next(l for l in order["lines"] if l["lane_id"] == lane_id)

    assert line(r1, c1)["fill_qty"] == 10
    assert line(r2, c1)["fill_qty"] == 0
    assert line(r2, c1)["status"] == "full"

    # 旧单快照保持改库存前的数字，不被新计算覆盖
    stored1 = client.get(f"/api/refills/{r1['id']}").json()
    assert line(stored1, c1)["fill_qty"] == 10
    assert stored1["total_fill"] == 32
    assert _count(db_session, RefillOrder) == 2


def test_trim_orders_back_to_seed_row_counts(client, db_session):
    # 绿仓种子：1 点位 / 6 货道 / 6 销量 / 0 单
    assert _count(db_session, Location) == 1
    assert _count(db_session, Lane) == 6
    assert _count(db_session, Sale) == 6

    client.post("/api/refills/run?location_id=1")
    client.post("/api/refills/run?location_id=1")
    assert _count(db_session, RefillOrder) == 2

    # 按点位裁单：只清空 refill_orders
    r = client.delete("/api/refills?location_id=1")
    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert _count(db_session, RefillOrder) == 0
    # 业务表种子行一条都不许少（禁止整表删光假装绿）
    assert _count(db_session, Location) == 1
    assert _count(db_session, Lane) == 6
    assert _count(db_session, Sale) == 6

    # 逐张裁单同样只动补货单表
    client.post("/api/refills/run?location_id=1")
    oid = client.get("/api/refills?location_id=1").json()[0]["id"]
    assert client.delete(f"/api/refills/{oid}").status_code == 200
    assert _count(db_session, RefillOrder) == 0
    assert _count(db_session, Lane) == 6


def test_delete_unknown_order_404(client):
    assert client.delete("/api/refills/12345").status_code == 404
