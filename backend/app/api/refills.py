import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Lane, Location, RefillOrder
from app.services.fill_engine import build_fill_lines, summarize

router = APIRouter(prefix="/refills", tags=["refills"])


def _locked_location(db: Session, location_id: int) -> Location | None:
    """锁点位行：同一点位的两次补货生成在此串行，
    后一单必须等前一单提交后，才能按当时货道重新读数计算。"""
    return db.scalars(
        select(Location).where(Location.id == location_id).with_for_update()
    ).first()


def _current_lanes(db: Session, location_id: int, for_update: bool = False) -> list[dict]:
    """从事务内读取货道当前库存快照。

    for_update=True 时连同货道行一起锁定（调用方须已持有该点位锁），
    保证"读数→整单计算→落库"期间没有库存改动插入，两次生成各按当时货道现算。
    """
    stmt = select(Lane).where(Lane.location_id == location_id).order_by(Lane.slot_no)
    if for_update:
        stmt = stmt.with_for_update()
    rows = db.scalars(stmt).all()
    return [
        {
            "id": r.id,
            "slot_no": r.slot_no,
            "sku_name": r.sku_name,
            "capacity": r.capacity,
            "stock": r.stock,
            "in_transit": r.in_transit,
        }
        for r in rows
    ]


def _live_summary(db: Session, location_id: int) -> dict:
    """按当前货道实时计算的补货建议，只读，不落任何单。"""
    return summarize(build_fill_lines(_current_lanes(db, location_id)))


def _order_brief(order: RefillOrder) -> dict:
    data = json.loads(order.lines_json)
    return {
        "id": order.id,
        "location_id": order.location_id,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "total_fill": data["total_fill"],
        "need_fill_count": data["need_fill_count"],
        "full_count": data["full_count"],
        "overbooked_count": data["overbooked_count"],
    }


@router.post("/run")
def run_refill(location_id: int = 1, db: Session = Depends(get_db)):
    # 先锁点位：点位不存在直接拒绝，本事务不做任何写入。
    loc = _locked_location(db, location_id)
    if loc is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="点位不存在")
    try:
        # 明细全部按当前货道现算完毕后，才写入这一张单；
        # 单表单行（明细为 JSON 快照），flush + commit 一次提交，
        # 失败整体回滚，库里不留半张单。
        summary = summarize(build_fill_lines(_current_lanes(db, location_id, for_update=True)))
        order = RefillOrder(
            location_id=location_id,
            created_at=datetime.utcnow(),
            lines_json=json.dumps(summary, ensure_ascii=False),
        )
        db.add(order)
        db.flush()
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="补货单生成失败，已整体回滚")
    db.refresh(order)
    return {"id": order.id, "location_id": location_id, **summary}


@router.get("")
def list_refills(location_id: int = 1, db: Session = Depends(get_db)):
    if db.get(Location, location_id) is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    rows = db.scalars(
        select(RefillOrder)
        .where(RefillOrder.location_id == location_id)
        .order_by(RefillOrder.id)
    ).all()
    return [_order_brief(r) for r in rows]


@router.get("/latest")
def latest(location_id: int = 1, db: Session = Depends(get_db)):
    if db.get(Location, location_id) is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    order = db.scalars(
        select(RefillOrder)
        .where(RefillOrder.location_id == location_id)
        .order_by(RefillOrder.id.desc())
    ).first()
    # 没有单就如实返回 404，禁止为了凑数字而顺手再生成一张。
    if order is None:
        raise HTTPException(status_code=404, detail="暂无补货单")
    data = json.loads(order.lines_json)
    return {"id": order.id, "location_id": location_id, **data}


@router.get("/full")
def full_lanes(location_id: int = 1, db: Session = Depends(get_db)):
    if db.get(Location, location_id) is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    # 满仓名单按当前货道实时计算，只读不写，不依赖"最近单"，更不会触发生成。
    summary = _live_summary(db, location_id)
    return {
        "location_id": location_id,
        "lanes": [l for l in summary["lines"] if l["status"] == "full"],
    }


@router.get("/summary")
def refill_summary(location_id: int = 1, db: Session = Depends(get_db)):
    if db.get(Location, location_id) is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    # 汇总数字按当前货道实时计算，只读不写。
    data = _live_summary(db, location_id)
    return {
        "location_id": location_id,
        "total_fill": data["total_fill"],
        "need_fill_count": data["need_fill_count"],
        "full_count": data["full_count"],
        "overbooked_count": data["overbooked_count"],
    }


@router.get("/{order_id}")
def get_refill(order_id: int, db: Session = Depends(get_db)):
    order = db.get(RefillOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="补货单不存在")
    data = json.loads(order.lines_json)
    return {"id": order.id, "location_id": order.location_id, **data}


@router.delete("/{order_id}")
def delete_refill(order_id: int, db: Session = Depends(get_db)):
    """裁单：只删 refill_orders 自己这一张，绝不波及点位/货道/销售等业务表。"""
    order = db.get(RefillOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="补货单不存在")
    db.delete(order)
    db.commit()
    return {"deleted": order_id}


@router.delete("")
def clear_refills(location_id: int = 1, db: Session = Depends(get_db)):
    """按点位把补货单裁回绿仓种子状态（种子本身没有补货单），只动 refill_orders。"""
    if db.get(Location, location_id) is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    result = db.execute(
        delete(RefillOrder).where(RefillOrder.location_id == location_id)
    )
    db.commit()
    return {"deleted": result.rowcount or 0}
