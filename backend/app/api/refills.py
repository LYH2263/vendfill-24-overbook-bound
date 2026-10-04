import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Lane, Location, RefillOrder
from app.services.fill_engine import build_fill_lines, summarize
router = APIRouter(prefix="/refills", tags=["refills"])

# 没有任何补货单时的只读空快照：读接口永远不触发生成/写库。
def _empty_snapshot(location_id: int) -> dict:
    return {
        "id": None, "location_id": location_id,
        "total_fill": 0, "need_fill_count": 0, "full_count": 0,
        "overbooked_count": 0, "lines": [],
    }

def _get_location(db: Session, location_id: int) -> Location:
    loc = db.get(Location, location_id)
    if not loc:
        raise HTTPException(404, "点位不存在")
    return loc

def _latest_order(db: Session, location_id: int) -> RefillOrder | None:
    return db.scalars(
        select(RefillOrder).where(RefillOrder.location_id == location_id)
        .order_by(RefillOrder.id.desc())
    ).first()

def _snapshot(db: Session, location_id: int) -> dict:
    """只读：返回最近一张已完整提交的补货单；没有就给空快照，绝不现场生成。"""
    order = _latest_order(db, location_id)
    if not order:
        return _empty_snapshot(location_id)
    return {"id": order.id, "location_id": location_id, **json.loads(order.lines_json)}

@router.post("/run")
def run_refill(location_id: int = 1, db: Session = Depends(get_db)):
    # 拒绝路径先走完：点位不存在直接 404，此前之后都不允许产生任何写库。
    _get_location(db, location_id)
    # 全部读 + 计算在事务外完成，快照只反映“当时”货道，不夹带其它单据的半截数字。
    lanes = db.scalars(select(Lane).where(Lane.location_id == location_id).order_by(Lane.slot_no)).all()
    payload = [{"id": l.id, "slot_no": l.slot_no, "sku_name": l.sku_name,
                "capacity": l.capacity, "stock": l.stock, "in_transit": l.in_transit} for l in lanes]
    summary = summarize(build_fill_lines(payload))
    order = RefillOrder(location_id=location_id, created_at=datetime.utcnow(),
                        lines_json=json.dumps(summary, ensure_ascii=False))
    # 单条整单原子提交：成功则整张落下，任何异常都回滚，绝不留半张单。
    try:
        db.add(order)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "补货单生成失败，未写入任何数据")
    db.refresh(order)
    return {"id": order.id, "location_id": location_id, **summary}

@router.get("")
def list_refills(location_id: int = 1, db: Session = Depends(get_db)):
    _get_location(db, location_id)
    rows = db.scalars(
        select(RefillOrder).where(RefillOrder.location_id == location_id)
        .order_by(RefillOrder.id.desc())
    ).all()
    out = []
    for r in rows:
        data = json.loads(r.lines_json)
        out.append({"id": r.id, "location_id": location_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "total_fill": data["total_fill"], "need_fill_count": data["need_fill_count"],
                    "full_count": data["full_count"], "overbooked_count": data["overbooked_count"]})
    return out

@router.get("/latest")
def latest(location_id: int = 1, db: Session = Depends(get_db)):
    _get_location(db, location_id)
    return _snapshot(db, location_id)

@router.get("/full")
def full_lanes(location_id: int = 1, db: Session = Depends(get_db)):
    _get_location(db, location_id)
    data = _snapshot(db, location_id)
    return {"location_id": location_id, "lanes": [l for l in data["lines"] if l["status"] == "full"]}

@router.get("/summary")
def refill_summary(location_id: int = 1, db: Session = Depends(get_db)):
    _get_location(db, location_id)
    data = _snapshot(db, location_id)
    return {
        "location_id": location_id,
        "total_fill": data["total_fill"],
        "need_fill_count": data["need_fill_count"],
        "full_count": data["full_count"],
        "overbooked_count": data["overbooked_count"],
    }
