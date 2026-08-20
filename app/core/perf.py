"""ความเร็วที่วัดได้จากทราฟฟิกจริง — ใช้จัดอันดับให้ `model="auto"`

ทำไมไม่ใช้ตัวเลขจาก benchmark อย่างเดียว:
benchmark วัดตอนเครื่องว่าง · เวลาเลือกโมเดลให้คำขอที่กำลังมา สิ่งที่ต้องรู้คือ
"ตอนนี้ตัวไหนตอบเร็ว" ซึ่งขึ้นกับโหลด ณ ขณะนั้นด้วย · ตัวเลขจากทราฟฟิกจริงจึงตรงกว่า
และได้มาฟรี — LiteGate บันทึก latency/ttft/token ของทุกคำขออยู่แล้ว

ทำไมไม่ query UsageLog:
เส้นทางคำขอต้องไม่แตะฐานข้อมูลเพิ่มเพื่อเลือกโมเดล · EWMA ในหน่วยความจำอ่านได้ทันที
และ "ผิดไปนิดหน่อยแต่เร็ว" ดีกว่า "แม่นแต่เพิ่ม query ทุกคำขอ" สำหรับงานจัดอันดับ

หน่วยความจำหายตอนรีสตาร์ต — ตั้งใจ · ค่าที่เก็บคือสภาพ *ตอนนี้* ของกระบวนการนี้
ไม่ใช่ประวัติศาสตร์ ซึ่ง UsageLog เก็บให้อยู่แล้ว
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

# น้ำหนักของค่าใหม่ใน EWMA · 0.25 = จำได้ราว 4 คำขอหลังสุด
# สูงกว่านี้ไวเกินจนคำขอเดียวที่ผิดปกติเปลี่ยนอันดับ ต่ำกว่านี้ตามโหลดที่เปลี่ยนไม่ทัน
_ALPHA = 0.25

# ต้องเห็นกี่คำขอก่อนถึงเชื่อตัวเลข · น้อยกว่านี้ = ยังไม่มีข้อมูลพอจะจัดอันดับ
# หนึ่งคำขอที่บังเอิญเร็วไม่ควรทำให้โมเดลนั้นได้ทราฟฟิกทั้งหมดไป
MIN_SAMPLES = 3


@dataclass
class ModelPerf:
    """สภาพของ alias หนึ่งตามที่ทราฟฟิกจริงบอก"""

    samples: int = 0
    ttft_ms: float | None = None          # EWMA
    output_tps: float | None = None       # EWMA · token ต่อวินาทีช่วง decode

    @property
    def usable(self) -> bool:
        return self.samples >= MIN_SAMPLES and self.output_tps is not None


@dataclass
class PerfStore:
    """EWMA ต่อ alias — เขียนจากทุก worker thread จึงต้องมี lock"""

    _by_alias: dict[str, ModelPerf] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, alias: str, *, latency_ms: int, ttft_ms: int | None,
               output_tokens: int) -> None:
        """บันทึกผลของคำขอหนึ่ง — เรียกจากทางเดิน finalize ที่มีอยู่แล้ว

        คำขอที่ไม่มี output ไม่บอกอะไรเรื่องความเร็ว decode — ข้ามไป ไม่งั้น error
        ที่ตอบเร็วเพราะไม่ได้ทำอะไรเลยจะทำให้โมเดลนั้นดูเร็วที่สุด
        """
        if output_tokens <= 0 or latency_ms <= 0:
            return
        # ช่วง decode = เวลาทั้งหมด ลบเวลาที่รอ token แรก
        decode_ms = max(1, latency_ms - (ttft_ms or 0))
        tps = output_tokens * 1000.0 / decode_ms
        with self._lock:
            perf = self._by_alias.setdefault(alias, ModelPerf())
            perf.samples += 1
            perf.output_tps = _blend(perf.output_tps, tps)
            if ttft_ms is not None and ttft_ms > 0:
                perf.ttft_ms = _blend(perf.ttft_ms, float(ttft_ms))

    def get(self, alias: str) -> ModelPerf | None:
        with self._lock:
            return self._by_alias.get(alias)

    def snapshot(self) -> dict[str, ModelPerf]:
        with self._lock:
            return dict(self._by_alias)

    def clear(self) -> None:
        with self._lock:
            self._by_alias.clear()


def _blend(previous: float | None, value: float) -> float:
    return value if previous is None else (1 - _ALPHA) * previous + _ALPHA * value
