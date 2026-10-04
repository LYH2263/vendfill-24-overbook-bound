from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Lane
from app.services.fill_engine import compute_gap
router = APIRouter(prefix="/lanes", tags=["lanes"])


class LanePatch(BaseModel):
    stock: int | None = Field(default=None, ge=0)
    in_transit: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, gt=0)


@router.get("")
def list_lanes(location_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Lane).order_by(Lane.slot_no)
    if location_id is not None: q = q.where(Lane.location_id == location_id)
    out = []
    for r in db.scalars(q).all():
        gap = compute_gap(r.capacity, r.stock, r.in_transit)
        out.append({"id": r.id, "location_id": r.location_id, "slot_no": r.slot_no, "sku_name": r.sku_name,
                    "capacity": r.capacity, "stock": r.stock, "in_transit": r.in_transit, "gap": gap,
                    "fill_pct": round(r.stock / r.capacity * 100, 1) if r.capacity else 0})
    return out


@router.patch("/{lane_id}")
def update_lane(lane_id: int, body: LanePatch, db: Session = Depends(get_db)):
    lane = db.get(Lane, lane_id)
    if lane is None:
        raise HTTPException(status_code=404, detail="货道不存在")
    changed = False
    for field in ("stock", "in_transit", "capacity"):
        value = getattr(body, field)
        if value is not None:
            setattr(lane, field, value)
            changed = True
    if changed:
        db.commit()
        db.refresh(lane)
    gap = compute_gap(lane.capacity, lane.stock, lane.in_transit)
    return {"id": lane.id, "location_id": lane.location_id, "slot_no": lane.slot_no,
            "sku_name": lane.sku_name, "capacity": lane.capacity, "stock": lane.stock,
            "in_transit": lane.in_transit, "gap": gap}
