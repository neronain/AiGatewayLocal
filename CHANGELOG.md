# Changelog

One line per change, in the words of the commit that made it. Newest first.
Version badge and `pyproject.toml` are the source of truth for the release number; entries below are
grouped by the day they landed on `main`.

## 1.6.0 — 2026-09-20

### ปิดช่องโหว่รายได้: ตัดการเชื่อมต่อ = ใช้ฟรี

ทั้ง codebase **ไม่มี `asyncio.shield` เลยสักที่** · จุดที่เรียก `ctx.finalize()` เกือบ
ทุกจุดอยู่ในบล็อก `finally` ซึ่งรันใต้ `CancelledError` เมื่อ client หลุด → **`await`
ตัวแรกข้างใน finalize โยนทิ้งทันที** → `usage.submit()` และ `quota.record()` ไม่เคยรัน

ผลคือ token ที่ backend เผาไปจริงไม่ถูกนับเข้าโควตาใครเลย · ผู้ใช้ไม่ต้องตั้งใจโกงด้วยซ้ำ —
coding agent ที่ยกเลิก request เมื่อผู้ใช้พิมพ์ต่อก็ทำให้เกิดอาการนี้ตลอดเวลา และยิ่ง
ยกเลิกตอนใกล้ตอบจบ ยิ่งเสียเยอะ

- `finalize()` ห่อด้วย shield แล้ว — งานบันทึกย้ายไปเป็น task ของตัวเองที่วิ่งต่อจนจบ
  ส่วนผู้เรียกยังได้ `CancelledError` ตามสัญญาของ asyncio
- ห่อไว้ **ในตัว `finalize` จุดเดียว** ไม่ใช่ไล่แก้ทีละจุดเรียก เพราะสามโปรโตคอล
  (openai · anthropic · responses) ใช้ `_RequestContext` ร่วมกันและเรียกรวม 12 จุด
- เก็บ strong reference ไว้ใน `_PENDING` — asyncio ถือ task แบบ weak reference
  ไม่มีใครถือไว้ = GC เก็บทิ้งกลางคันได้ แล้วกลับไปเสียเงินเหมือนเดิมโดยเทสยังเขียว
- backlog มีเพดาน 2048 ตัว · เต็มแล้วทิ้งพร้อม log error ดีกว่าให้ gateway ตายทั้งตัว
- drain ตอนปิดแอป (ก่อน `state.stop()`) ไม่งั้น request รอบสุดท้ายก่อนดีพลอยหายไป
- **`finalize` กันเรียกซ้ำแล้วจริง** — docstring เขียนว่า "exactly once" มาตลอดแต่ไม่เคย
  มีอะไรบังคับ · พอมี shield การเรียกซ้ำจะกลายเป็นการคิดเงินซ้ำ ไม่ใช่แค่ log ซ้ำ

### metrics ที่เคยโกหก

gateway นี้เสิร์ฟ streaming เป็นหลัก แต่ `call_next` ของ Starlette คืนค่าเมื่อ **header
พร้อม** ไม่ใช่เมื่อ body ไหลจบ · ตัวเลขที่นับตรงนั้นจึงผิดแทบทุกแถว และผิดแบบที่ดูเผิน ๆ
เหมือนใช้ได้ ซึ่งแย่กว่าไม่มีตัวเลขเลย

- **`requests_in_flight` ลดค่าก่อน body เริ่มไหลด้วยซ้ำ** → stream ที่กำลังวิ่งอยู่
  มองไม่เห็นเลยบน dashboard ซึ่งตรงข้ามกับสิ่งที่ gauge นี้มีไว้ตอบ
- **`request_duration` เป็น time-to-first-header** → stream 3 นาทีถูกบันทึกเป็น 40 ms
- ห่อ `body_iterator` ให้ทั้งคู่ไปนับจบตอน body ไหลหมดจริง · รวมกรณี client หลุดกลางทาง
  ซึ่งถ้าไม่นับ gauge จะค้างสูงถาวรจนกว่าจะรีสตาร์ต
- **TTFT export แล้ว** (`litegate_time_to_first_token_seconds`) — เดิมวัดไว้และเขียนลง
  UsageLog แต่ไม่เคย export จึงดูย้อนหลังได้ทีละแถวใน DB เท่านั้น ตั้ง alert หรือดู
  percentile ไม่ได้เลย ทั้งที่เป็นตัวชี้วัดหลักของ gateway ที่เสิร์ฟ streaming

### hot path

- **เลิกเขียน DB ทุกคำขอ** — `last_used_at` เคย commit ทุกคำขอ คือ write transaction
  เต็มตัวต่อหนึ่งคำขอ เพียงเพื่อข้อมูลที่หน้าเว็บแสดงเป็น "ใช้ล่าสุด" · เขียนนาทีละครั้งพอ
- **health probe เลิกเปิด connection ใหม่ทุกรอบ** — เดิมสร้าง `AsyncClient` ใหม่ทุก probe
  ทุก endpoint ทุก 15 วินาที คูณจำนวน worker · ใช้ client ตัวเดียวตลอดอายุ router

684 tests (+9) · ruff clean

---

## 1.5.0 — 2026-09-20

Two bugs that were invisible on the day they happened, one lint gate brought back from the dead,
and the first secret scan this repository has ever had.

- **A model edited in the console stopped being LMDS-managed.** `GET /admin/models` returned every
  endpoint field except `managed_by`, and the console rebuilds its save payload from that response —
  so saving from the console silently deleted the record of which machine and which LMDS bundle a
  backend came from. `grep -c managed_by app/static/app.js` was 0. Nothing on the request path reads
  the field, so the damage showed up months later as a missing Apply-fix button and a `/advice`
  command that no longer names the real machine. The listing now returns it, the console carries it
  through untouched, and the server re-attaches it on a whole-document save that never mentioned it —
  so an old console, a script, or a `curl` of yesterday's document can no longer unmanage a fleet.
  An explicit `"managed_by": null` still removes it.
- **A three-minute stream held a database connection for three minutes.** `get_session` never
  committed after its last read, and FastAPI does not close the dependency stack until the response
  body has finished — for a streaming request, the whole life of the stream. The ceiling was a few
  dozen concurrent requests, and on SQLite the WAL grew without bound behind read snapshots nobody
  released. Nothing past that point needs the request session: usage buffers in memory, and both
  quota counter stores open their own. All three protocol surfaces release at the same point.
  The SQLite pool is sized deliberately now instead of falling through to a default nobody chose.
  Known limit: this proves the connection is released, not that the ceiling moved — measure under
  real load before raising the worker count.
- **`ruff check` had been failing for long enough that nobody read it.** 27 findings, all in the
  blocking CI step. A lint gate everyone ignores is worse than none: it trains people to skip the
  whole job summary. Every dead import was checked for indirect use before deletion — an import
  that registers something is load-bearing, not dead. No new suppressions.
- **A secret scan, which this repository never had.** It is public and the gateway issues real
  credentials, so a key committed here is a live key — while the sibling LMDS repository, which
  issues none, has had a scan for months. The pattern covers this product's own `lg_sk_` and legacy
  `edu_sk_` formats. It does not skip `tests/`, because nothing there hardcodes a key today and
  keeping it that way is worth more than the convenience. A second step refuses a tracked
  environment file.
- **The product is no longer for education.** The OpenAPI description, the package metadata and
  most of the documentation still said so — that string is the first thing any integration reads.
  The database tables are still `courses` / `enrollments` / `course_models` with `course_id` on five
  of them; renaming those is a migration with real risk, so the mismatch is now documented rather
  than surprising.
- `docs/API.md` gains the four admin model endpoints that were real, tested, and mentioned only in
  prose. `docs/DEPLOYMENT.md` gains a Redis section and an honest known-limitations section covering
  the metrics that cannot be trusted across workers, the readiness gauges that can page on a healthy
  gateway and go silent on a real outage, two pieces of dead config, and a rate limit keyed on
  source IP that caps a whole tenant behind one NAT.
- Ops, not a code change: `GW_REDIS_URL` is set on the deployed gateway. Quota counters move off
  SQLite, where `increment` was a read-modify-write through the ORM that lost concurrent updates —
  always in the member's favour — and serialised every write behind one file lock.

## 1.4.x — 2026-08-20 → 2026-09-20

### 2026-09-20

Documentation only — no behaviour changed. Entries carry no commit hash because
they landed as a docs pass rather than one commit each.

- Quota counters now run on Redis on the live gateway: `GW_REDIS_URL` was empty
  and is now `redis://127.0.0.1:6379/0`, which moves counting from
  `DatabaseCounterStore` to `ResilientCounterStore(Redis, Database)`. Documented
  in DEPLOYMENT.md §5f and NETWORK.md — **strongly recommended, not optional,
  on any deployment running more than one worker**, because the database
  increment is a read-modify-write that loses concurrent updates
- Say what the product is for without saying "education" — the FastAPI
  description behind `GET /openapi.json`, and the package description
- Document the four registry-authoring endpoints the API reference never
  listed: `POST /admin/models`, `POST /admin/models/preview`,
  `POST /admin/models/detect`, `DELETE /admin/models/{alias}`
- DEPLOYMENT.md §10: a known-limitations section an operator has to read before
  trusting `/metrics` — multi-worker scrapes, what the duration histogram
  really measures, what the in-flight gauge really counts, `GW_WORKERS`,
  `quota_defaults.term_start_months`, the nginx per-IP limits, unenforced
  `ApiKey.scopes`, and port 8080
- Write down that the tables are still called `courses`, `enrollments` and
  `course_models` while the code says `Workspace` and `Membership`, instead of
  letting the next reader find out from a backup
- Test count in the README was 652 in the badge and 397 in the Development
  section — two places to keep in step and both stale. The badge now says 675
  (re-derive it at release time) and the Development section no longer repeats
  a number

### 2026-09-02

- Catch the wrong tool parser, not just a missing one (`2c4d1d6`)

### 2026-08-28

- merge: ติดตั้งใหม่บนเครื่องจริงแล้วเพิ่มโมเดลจากคอนโซลไม่ได้ (`c9a0b02`)

### 2026-08-27

- ติดตั้งใหม่บนเครื่องจริงแล้วเพิ่มโมเดลจากคอนโซลไม่ได้ (`5b5831b`)
- SQLite ล็อกกันเองจน auth ล้ม ทั้งที่ backend ไม่ได้ผิดอะไร (`487279b`)

### 2026-08-26

- List Free Claude Code, and say how it installs itself (`bc1e036`)
- Put the downloads behind Details, and list what we cannot mirror (`4f133a4`)
- Say when this console was put on the machine (`cc1e49f`)
- Offer the project scope, and the file git will not take (`95f1f56`)
- Cover the tools the team actually has open (`bdcdcec`)
- Put the countdown where the eye lands (`492dbab`)
- Move the menu to the side so the phone gets its screen back (`addb4e4`)
- Let the console reach the routing that was already there (`d2dee73`)
- Hand people the config instead of the documentation (`ac69769`)

### 2026-08-22

- Carry the formats a school actually installs with (`b456ede`)
- Offer the switcher to people who only have a terminal (`e660328`)
- Stop answering to the name we stopped using (`48b43fb`)

### 2026-08-21

- Answer where the reader is standing (`683b0c3`)
- Say what --demo is before offering it, and what Docker inherits (`6422477`)
- The console was denied from the machine it runs on (`eb4adb7`)
- A fresh install answers on the name you happen to type (`162adce`)
- TLS refused to install on the Ubuntu release most people are running (`686468c`)
- Somewhere to go when the console password is the thing you lost (`b6734a8`)
- An hourly window, and labels back in English (`7ee49d4`)
- A key can now carry a ceiling of its own (`fb57397`)
- Issuing a key now shows the quota the person will actually get (`363981d`)

### 2026-08-20

- Make installing this as easy as the deploy tool it ships beside (`b55e030`)
- The list of models a cloud key can reach, where the key is entered (`656e2be`)
- Somewhere to actually put the API key (`b6d82ee`)
- A model deleted on one worker stayed alive on the other three (`7763147`)
- Show which models the key can actually reach, as a choice (`063bd7c`)
- MiniMax China and Global are two providers, not one with a footnote (`20650ce`)
- README: show the member page with something in it (`521facc`)
- Stop appending a second /v1 to cloud base URLs (`2950d4e`)
- Member page lists only what the key can call right now (`320d479`)
- หน้า member: รวมแค็ตตาล็อกเข้ากับตารางการใช้งาน + ไอคอนความสามารถ (`85544e0`)
- หน้าแรกเป็นหน้าต้อนรับ ไม่ใช่ก้อน JSON (`226a7f1`)
- เพิ่มโมเดลจากผู้ให้บริการออนไลน์ได้ — 8 เจ้า พร้อมค่าตั้งต้น (`9ed2db3`)
- หน้า /console/member/ — สมาชิกเอา key ตัวเองมาดูสิทธิ์ได้ (`1cf363d`)
- ปุ่มพับไม่เคยขึ้นเลย — โค้ดไปลงในทางเดิน sign-in ไม่ใช่ boot() (`09bc989`)
- พับได้ทุกแท็บ + ปุ่มย่อทั้งชุด (`ef9d6ab`)
