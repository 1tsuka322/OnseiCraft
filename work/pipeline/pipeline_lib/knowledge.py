# -*- coding: utf-8 -*-
"""知识库变更提案系统（GPT 建议的 corpus → mining → proposal → validate → approve）。

边界（硬规则）：
- **提案不能直接改生产表**。`knowledge/proposals/*.json` 是唯一入口。
- 只有 `validate` 全过 + 人工 `approve --by human --confirm` 才合并。
- 合并前备份、合并后跑表自检；自检不过自动回滚。
- 每次合并自动快照成新版本，并把版本号写回提案 → 「成品由哪版术语表产生」可回答。
- `parallel.json`（17MB 风格库）只记哈希不复制，避免仓库膨胀。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time

from . import manifest, paths

SNAP_MAX_BYTES = 5 << 20
KNOW_LOCK = os.path.join(paths.KNOW, ".approve.lock")
COMMIT_PENDING = os.path.join(paths.KNOW, "commit_pending.json")
STATES = ["candidate", "reviewed", "validated", "approved", "production",
          "rejected", "needs_review"]
TYPES = ["asr_errata", "term_glossary", "pin_set", "do_not_change"]

TABLE_OF = {
    "asr_errata": "asr_errata.json",
    "term_glossary": "glossary.json",
    "pin_set": "pin_sets.json",
    "do_not_change": "do_not_change.json",
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def pending_commits():
    """上次 approve 没走完留下的记录（表已改/快照失败）。没有就 None。"""
    if not os.path.exists(COMMIT_PENDING):
        return None
    try:
        return json.load(open(COMMIT_PENDING, encoding="utf-8"))
    except Exception:
        return {"stage": "unreadable"}


def _clear_pending():
    try:
        os.remove(COMMIT_PENDING)
    except OSError:
        pass


def _write_pending(rec):
    os.makedirs(os.path.dirname(COMMIT_PENDING), exist_ok=True)
    json.dump(rec, open(COMMIT_PENDING, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- registry / 版本
def load_registry():
    if os.path.exists(paths.KNOW_REGISTRY):
        return json.load(open(paths.KNOW_REGISTRY, encoding="utf-8"))
    return {"schema": 1, "current": None, "versions": []}


def save_registry(reg):
    os.makedirs(paths.KNOW, exist_ok=True)
    tmp = paths.KNOW_REGISTRY + ".tmp"
    json.dump(reg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, paths.KNOW_REGISTRY)


def _counts(fn):
    p = os.path.join(paths.STYLE, fn)
    if not os.path.exists(p):
        return {"exists": False}
    size = os.path.getsize(p)
    if fn.endswith(".txt"):
        n = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
        return {"exists": True, "lines": n, "bytes": size}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"exists": True, "bytes": size, "parsed": False}
    if fn == "asr_errata.json":
        es = d.get("entries") or []
        modes = {}
        for e in es:
            modes[e.get("mode", "auto")] = modes.get(e.get("mode", "auto"), 0) + 1
        return {"exists": True, "entries": len(es), "modes": modes, "bytes": size}
    if fn == "do_not_change.json":
        return {"exists": True, "pairs": len(d.get("pairs") or []), "bytes": size}
    if isinstance(d, dict):
        return {"exists": True, "keys": len(d), "bytes": size}
    return {"exists": True, "items": len(d), "bytes": size}


def snapshot(note=""):
    reg = load_registry()
    n = len(reg["versions"]) + 1
    vid = "v%04d" % n
    d = os.path.join(paths.KNOW_VERSIONS, "%s_%s" % (vid, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(d, exist_ok=False)
    files = {}
    for fn, role, desc in paths.KNOWLEDGE_FILES:
        p = os.path.join(paths.STYLE, fn)
        rec = {"role": role, "desc": desc, "sha256": manifest.sha256_file(p),
               "bytes": os.path.getsize(p) if os.path.exists(p) else None,
               "exists": os.path.exists(p), "copied": False}
        if rec["exists"] and rec["bytes"] <= SNAP_MAX_BYTES:
            shutil.copy2(p, os.path.join(d, fn))
            rec["copied"] = True
        files[fn] = rec
    meta = {"id": vid, "created_at": _now(), "note": note, "files": files,
            "counts": {fn: _counts(fn) for fn, _, _ in paths.KNOWLEDGE_FILES}}
    json.dump(meta, open(os.path.join(d, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    reg["versions"].append({"id": vid, "dir": os.path.relpath(d, paths.ROOT).replace("\\", "/"),
                            "created_at": meta["created_at"], "note": note,
                            "agg": manifest.knowledge_agg(files)})
    reg["current"] = vid
    save_registry(reg)
    print("已建知识表版本 %s（%d 个文件，%d 个完整复制）"
          % (vid, len(files), sum(1 for v in files.values() if v["copied"])))
    return vid


def ensure_registry():
    reg = load_registry()
    if not reg["versions"]:
        snapshot("初始化：登记当前生产表")
        reg = load_registry()
    return reg


def drift():
    """生产表相对「当前版本」被改过没有。改了就是未登记变更（历史最大隐患）。"""
    reg = ensure_registry()
    cur = next((v for v in reg["versions"] if v["id"] == reg["current"]), None)
    if not cur:
        return [], {}
    meta = json.load(open(os.path.join(paths.ROOT, cur["dir"], "meta.json"), encoding="utf-8"))
    changed = []
    now = {}
    for fn, role, desc in paths.KNOWLEDGE_FILES:
        h = manifest.sha256_file(os.path.join(paths.STYLE, fn))
        now[fn] = h
        if (meta["files"].get(fn) or {}).get("sha256") != h:
            changed.append(fn)
    return changed, now


def status():
    reg = ensure_registry()
    changed, now = drift()
    print("知识表版本：%s（共 %d 个快照）" % (reg["current"], len(reg["versions"])))
    for v in reg["versions"][-3:]:
        print("  %-6s %s  %s" % (v["id"], v["created_at"], v.get("note") or ""))
    print("\n生产表（%s）：" % paths.STYLE)
    print("  %-22s %-10s %s" % ("文件", "角色", "内容"))
    for fn, role, desc in paths.KNOWLEDGE_FILES:
        c = _counts(fn)
        s = ", ".join("%s=%s" % (k, v) for k, v in c.items()
                      if k in ("entries", "keys", "pairs", "items", "modes", "lines"))
        if not s:
            s = ("%.0f KB" % (c["bytes"] / 1024.0)) if c.get("bytes") else "（缺）"
        print("  %-22s %-10s %s" % (fn, role, s))
    if changed:
        print("\n⚠️ 生产表已改动但**未登记版本**：%s" % ", ".join(changed))
        print("   要么 `pipeline knowledge snapshot --note \"…\"` 登记，")
        print("   要么这是别人的临时改动 —— 未登记 = 追溯链断了。")
    else:
        print("\n✅ 生产表与当前版本一致（无未登记变更）")
    pend = pending_commits()
    if pend:
        print("\n⚠️ 有未完成的提交：%s（stage=%s，%s）"
              % (pend.get("pid"), pend.get("stage"), pend.get("at")))
        print("   表可能已回滚；核对备份 %s 后手工清理 knowledge/commit_pending.json"
              % pend.get("backup"))


def diff(v1=None, v2=None):
    reg = ensure_registry()
    ids = [v["id"] for v in reg["versions"]]
    if not v1:
        v1 = ids[-2] if len(ids) >= 2 else ids[-1]
    if not v2:
        v2 = ids[-1]
    def load(vid):
        rec = next(v for v in reg["versions"] if v["id"] == vid)
        return json.load(open(os.path.join(paths.ROOT, rec["dir"], "meta.json"),
                              encoding="utf-8"))
    a, b = load(v1), load(v2)
    print("版本差异 %s → %s" % (v1, v2))
    for fn, _, _ in paths.KNOWLEDGE_FILES:
        ha = (a["files"].get(fn) or {}).get("sha256")
        hb = (b["files"].get(fn) or {}).get("sha256")
        if ha != hb:
            ca, cb = a["counts"].get(fn, {}), b["counts"].get(fn, {})
            print("  %-22s %s → %s  %s → %s"
                  % (fn, (ha or "无")[:8], (hb or "无")[:8],
                     _brief(ca), _brief(cb)))


def _brief(c):
    return ",".join("%s=%s" % (k, v) for k, v in c.items()
                    if k in ("entries", "keys", "pairs", "items"))


# ---------------------------------------------------------------- 提案
def _ppath(pid):
    return os.path.join(paths.KNOW_PROPOSALS, pid + ".json")


def load_proposal(pid):
    if not os.path.exists(_ppath(pid)):
        raise SystemExit("找不到提案：%s（`pipeline knowledge list` 看现有）" % pid)
    return json.load(open(_ppath(pid), encoding="utf-8"))


def save_proposal(p):
    p["updated_at"] = _now()
    tmp = _ppath(p["id"]) + ".tmp"
    json.dump(p, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, _ppath(p["id"]))


def new_proposal_id(kind):
    return "%s_%s_%03d" % (kind, time.strftime("%Y%m%d"),
                           len(glob.glob(os.path.join(paths.KNOW_PROPOSALS,
                                                      "%s_%s_*" % (kind, time.strftime("%Y%m%d"))))) + 1)


def propose(path):
    """导入提案（只登记，不动生产表）。支持单个对象或数组。"""
    os.makedirs(paths.KNOW_PROPOSALS, exist_ok=True)
    raw = json.load(open(path, encoding="utf-8"))
    items = raw if isinstance(raw, list) else [raw]
    ids = []
    for it in items:
        kind = it.get("type", "asr_errata")
        pid = it.get("id") or new_proposal_id(kind)
        it["id"] = pid
        it.setdefault("status", "candidate")
        it.setdefault("created_at", _now())
        it.setdefault("source", os.path.basename(path))
        it.setdefault("approved_by", None)
        it.setdefault("knowledge_version", None)
        save_proposal(it)
        ids.append(pid)
        print("提案 %s（%s，状态 %s）" % (pid, kind, it["status"]))
    return ids


def _errata_fields(p):
    ja = (p.get("ja") or "").strip()
    fix = (p.get("fix") or "").strip()
    if not ja or not fix:
        m = re.match(r"\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$", p.get("proposal") or "")
        if m:
            ja, fix = m.group(1), m.group(2)
    return ja, fix


def _load_table(fn):
    p = os.path.join(paths.STYLE, fn)
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def _errata_matcher():
    """复用 asr_fix_scan 的判定（表记白名单 + 反例 + ctx），避免两套语义。"""
    import sys
    if paths.SCRIPTS not in sys.path:
        sys.path.insert(0, paths.SCRIPTS)
    import asr_fix_scan as AF
    return AF


def _match_errata(text, e, ignore_mode=False):
    AF = _errata_matcher()
    ja, fix = e["ja"], e["fix"]
    if ja not in text:
        return False, "未出现错形"
    if text.count(ja) != 1 and not e.get("all_ok"):
        return False, "同段多次出现（需 all_ok）"
    if AF.is_table_variant(ja, fix):
        return False, "表记差异白名单"
    mode = e.get("mode", "auto")
    # `review` 的语义是「识别出来、但永不自动落地」。校验**正例**时我们要验的是
    # 「这条规则能不能认出这个错形」，不是「会不会自动改」——否则任何 review 条目
    # 的正例都必然失败（R154 前的口径错误，导致 review 条目根本没法提交）。
    if mode == "review" and not ignore_mode:
        return False, "review 模式永不自动改"
    if mode == "guard":
        ctx = e.get("ctx") or []
        if not ctx:
            return False, "guard 无 ctx"
        if not any(re.search(c, text) for c in ctx):
            return False, "ctx 未命中"
    blk = AF.errata_blocked(text, ja, e)
    if blk:
        return False, "被反例拦下：%s" % blk
    return True, "命中"


def validate(pid):
    """确定性验证。不跑模型、不改表。结果写 knowledge/tests/<id>.json。"""
    p = load_proposal(pid)
    kind = p.get("type")
    checks = []

    def ck(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    ck("type 合法", kind in TYPES, kind)
    ck("有证据", bool(p.get("evidence")), "evidence 不能为空")
    ck("有反例计数", p.get("counterexamples") is not None,
       "counterexamples=0 是必填（空值 = 没查过）")

    if kind == "asr_errata":
        ja, fix = _errata_fields(p)
        ck("ja/fix 非空且不同", ja and fix and ja != fix, "%r→%r" % (ja, fix))
        mode = p.get("mode", "auto")
        ck("mode 合法", mode in ("auto", "guard", "review"), mode)
        if mode == "guard":
            ck("guard 必须有 ctx 正向白名单", bool(p.get("ctx")),
               "R133 政策：guard 未命中 ctx = 新语境 → 只进 review")
        for key in ("ctx", "block_if"):
            bad = []
            for pat in p.get(key) or []:
                try:
                    re.compile(pat)
                except Exception as ex:
                    bad.append("%s(%s)" % (pat, ex))
                if "(?!" in pat or "(?<!" in pat:
                    bad.append("%s(负向断言已退役)" % pat)
            ck("%s 正则可编译且非负向" % key, not bad, bad)
        if mode == "auto":
            ck("auto 必须 legit=0", p.get("legit") == 0,
               "错形本身是常见词时绝不给 auto（会改坏正确句）")
        works = p.get("works") or []
        ck("works 作用域格式", all(re.match(r"^RJ\d+$", w) for w in works), works)

        tbl = _load_table("asr_errata.json") or {"entries": []}
        same = [e for e in tbl["entries"] if e.get("ja") == ja]
        ck("表内无同形冲突", not same or all(e.get("fix") == fix for e in same),
           "已存在 %d 条同形条目" % len(same))
        # 链式替换只对**会自动落地的条目**才成立：mode=review 的规则永不自动改，
        # 它出现在 fix 里不会引发二次改写。以前没区分，导致
        # 「反行動→班行動」被一条 review 的「行動→公道」误拦（2026-10-28 实测）。
        chain = [e["ja"] for e in tbl["entries"]
                 if e.get("ja") and e["ja"] != ja and e["ja"] in (fix or "")
                 and (e.get("mode") or "auto") != "review"]
        ck("无链式替换", not chain, chain)
        overlap = [e["ja"] for e in tbl["entries"]
                   if e.get("ja") and e["ja"] != ja and ja
                   and (ja in e["ja"] or e["ja"] in ja)
                   and (e.get("mode") or "auto") != "review"
                   and e["ja"] not in (p.get("overlap_ok") or [])]
        ck("无未声明的重叠", not overlap, overlap)

        tests = p.get("tests") or {}
        for i, t in enumerate(tests.get("positive") or []):
            tx = t.get("text") if isinstance(t, dict) else t
            ok, why = _match_errata(tx or "", dict(p, ja=ja, fix=fix), ignore_mode=True)
            ck("正例#%d 命中" % (i + 1), ok, "%s｜%s" % (why, (tx or "")[:60]))
        for i, t in enumerate(tests.get("negative") or []):
            tx = t.get("text") if isinstance(t, dict) else t
            ok, why = _match_errata(tx or "", dict(p, ja=ja, fix=fix))
            ck("反例#%d 不命中" % (i + 1), not ok, "%s｜%s" % (why, (tx or "")[:60]))

    elif kind == "term_glossary":
        ja, zh = (p.get("ja") or "").strip(), (p.get("zh") or "").strip()
        ck("ja/zh 非空", bool(ja and zh), "%r→%r" % (ja, zh))
        ck("zh 无日文假名", not re.search(r"[\u3040-\u30ff]", zh or ""), zh)
        ck("zh 长度合理", 1 <= len(zh or "") <= 12, len(zh or ""))
        tbl = _load_table("glossary.json") or {}
        old = (tbl.get(ja) or {}).get("zh")
        ck("无译法冲突", old is None or old == zh, "表内现值=%r" % old)

    elif kind == "pin_set":
        ja, allowed = (p.get("ja") or "").strip(), p.get("allowed") or []
        ck("ja 非空", bool(ja), ja)
        ck("allowed 非空", bool(allowed), allowed)
        ck("allowed 无日文", all(not re.search(r"[\u3040-\u30ff]", a) for a in allowed), allowed)
        tbl = _load_table("pin_sets.json") or {}
        old = (tbl.get(ja) or {}).get("allowed") or []
        ck("与现有允许集不矛盾", not old or set(allowed) <= set(old) or
           set(old) <= set(allowed), "现有=%s" % old)

    elif kind == "do_not_change":
        pair = p.get("pair") or []
        ck("pair 是两个写法", isinstance(pair, list) and len(pair) == 2 and all(pair), pair)
        tbl = _load_table("do_not_change.json") or {"pairs": []}
        ck("未重复登记", pair not in [x.get("pair") for x in tbl.get("pairs") or []
                                  if isinstance(x, dict)], pair)

    ok = all(c["ok"] for c in checks)
    p["status"] = "validated" if ok else "needs_review"
    p["validation"] = {"ran_at": _now(), "ok": ok, "checks": checks}
    save_proposal(p)
    os.makedirs(paths.KNOW_TESTS, exist_ok=True)
    json.dump({"id": pid, "type": kind, "ran_at": _now(), "ok": ok, "checks": checks},
              open(os.path.join(paths.KNOW_TESTS, pid + ".json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("验证 %s：%s（%d/%d 通过）" % (pid, "✅ 通过" if ok else "❌ 未过",
                                    sum(1 for c in checks if c["ok"]), len(checks)))
    for c in checks:
        if not c["ok"]:
            print("  ❌ %-28s %s" % (c["name"], c["detail"]))
    return ok


def _merge_into(tbl, p, pid):
    """在内存里把提案应用到表副本。返回 (新表, 是否 no-op)。"""
    t = json.loads(json.dumps(tbl))          # 深拷贝，失败时原表不受影响
    kind = p["type"]
    if kind == "asr_errata":
        ja, fix = _errata_fields(p)
        entry = {"ja": ja, "fix": fix}
        for k in ("reading", "evidence", "mode", "ctx", "works", "block_if",
                  "legit", "all_ok", "overlap_ok", "version", "strict_boundary"):
            if p.get(k) is not None:
                entry[k] = p[k]
        entry.setdefault("version", "proposal:" + pid)
        entries = t.setdefault("entries", [])
        same = [e for e in entries if e.get("ja") == ja and e.get("fix") == fix]
        entries[:] = [e for e in entries if not (e.get("ja") == ja and e.get("fix") == fix)]
        entries.append(entry)
        return t, bool(same)
    if kind == "term_glossary":
        ja = (p.get("ja") or "").strip()
        zh = (p.get("zh") or "").strip()
        cur = (t.get(ja) or {}).get("zh")
        t[ja] = {"zh": zh, "hit": 0, "rate": 0.0, "source": "proposal:" + pid}
        return t, cur == zh
    if kind == "pin_set":
        ja = (p.get("ja") or "").strip()
        cur = (t.get(ja) or {}).get("allowed")
        t[ja] = {"allowed": p["allowed"], "source": "proposal:" + pid,
                 "user_pinned": True}
        return t, cur == p["allowed"]
    if kind == "do_not_change":
        pairs = t.setdefault("pairs", [])
        key = tuple(sorted(p["pair"]))            # 规范化，防顺序不同算两条
        for x in pairs:
            if tuple(sorted(x.get("pair") or [])) == key:
                return t, True                    # 已有 → no-op
        pairs.append({"pair": p["pair"], "source": "proposal:" + pid})
        return t, False
    raise SystemExit("未知类型：%s" % kind)


def approve(pid, by="human", confirm=False):
    """合并到生产表。**事务化**：锁 → 幂等 → 内存构造 → 临时文件自检 → 原子替换
    → 快照 → 提案状态。任一步失败都回滚表并写 commit_pending。"""
    from .queue import FileLock

    p = load_proposal(pid)
    if not confirm:
        raise SystemExit("拒绝合并：需 `--confirm`（这是写生产表的动作）")
    if p.get("status") == "approved":
        print("提案 %s 已生效过（%s），本次 no-op" % (pid, p.get("knowledge_version")))
        return p.get("knowledge_version")
    if p.get("status") != "validated":
        raise SystemExit("拒绝合并：状态是 %s，需先 `knowledge validate %s` 通过"
                         % (p.get("status"), pid))
    if by != "human":
        raise SystemExit("拒绝合并：approved_by 必须是 human（Agent 不得自批）")
    fn = TABLE_OF.get(p["type"])
    if not fn:
        raise SystemExit("未知类型：%s" % p["type"])

    with FileLock(KNOW_LOCK, "knowledge-approve:%s" % pid):
        # 拿到锁后**重新读**：别的进程可能刚改过（被并发 approve 掉、或刚被 reject）。
        # 只复查 approved 不够——状态若已不是 validated，同样必须停。
        p = load_proposal(pid)
        if p.get("status") == "approved":
            print("提案 %s 已生效过（%s），本次 no-op" % (pid, p.get("knowledge_version")))
            return p.get("knowledge_version")
        if p.get("status") != "validated":
            raise SystemExit("拒绝合并：锁内复查状态是 %s（被其他进程改动），需先 validate"
                             % p.get("status"))

        src = os.path.join(paths.STYLE, fn)
        os.makedirs(paths.KNOW_BACKUPS, exist_ok=True)
        bak = os.path.join(paths.KNOW_BACKUPS,
                           "%s.%s.bak" % (fn, time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(src, bak)
        tbl = json.load(open(src, encoding="utf-8"))

        new_tbl, noop = _merge_into(tbl, p, pid)
        if noop:
            print("提案 %s 的内容已在表内（同值），本次 no-op" % pid)

        # 写临时文件 → 对临时文件自检 → 原子替换。
        # with 块保证异常时句柄关闭（Windows 上句柄没关，后面 os.remove 会 PermissionError）；
        # dump 半途失败只留 .tmp 残骸，src 未动（表仍是旧的），清理后原样抛出。
        tmp = src + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new_tbl, f, ensure_ascii=False, indent=1)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        ok, detail = _post_check(fn, path=tmp)
        if not ok:
            try:
                os.remove(tmp)
            except OSError:
                pass
            p["status"] = "validated"
            p["approve_error"] = detail
            save_proposal(p)
            raise SystemExit("❌ 合并后自检失败（表未改，临时文件已删）：%s" % detail)
        os.replace(tmp, src)
        print("已合并进 %s（备份 %s）" % (fn, os.path.basename(bak)))

        # 表已生效：从这里开始失败都要回滚 + 记 commit_pending。
        # 回滚本身也可能失败（磁盘满/备份损坏）——那种情况**必须**留 pending，
        # 否则表是新内容、registry 无版本、提案未标记，追溯链三处全断。
        try:
            vid = snapshot("approve %s（%s→%s）"
                           % (pid, p["type"], p.get("proposal") or p.get("ja")))
        except Exception as e:
            try:
                shutil.copy2(bak, src)
            except Exception as e2:
                _write_pending({"pid": pid, "table": fn,
                                "backup": os.path.relpath(bak, paths.ROOT).replace("\\", "/"),
                                "stage": "rollback_failed", "at": _now(),
                                "error": "%s / 回滚失败: %s" % (e, e2)})
                raise SystemExit("❌ 快照失败且回滚也失败（表为新内容、未入版本）。"
                                 "手工核对备份 %s 后恢复，见 knowledge/commit_pending.json" % bak)
            _write_pending({"pid": pid, "table": fn,
                            "backup": os.path.relpath(bak, paths.ROOT).replace("\\", "/"),
                            "stage": "snapshot_failed", "at": _now(), "error": str(e)})
            p["status"] = "validated"
            p["approve_error"] = "snapshot_failed: %s" % e
            save_proposal(p)
            raise SystemExit("❌ 快照失败，表已回滚。见 knowledge/commit_pending.json")

        # 快照已生效，只剩提案状态——它失败时表/版本都是对的，只是提案还挂着
        # validated：写 pending（带 knowledge_version）让人工补标记，别静默吞掉。
        try:
            p["status"] = "approved"
            p["approved_by"] = by
            p["approved_at"] = _now()
            p["knowledge_version"] = vid
            p["merged_table"] = fn
            p["backup"] = os.path.relpath(bak, paths.ROOT).replace("\\", "/")
            p.pop("approve_error", None)      # 清掉上次失败尝试的残留
            save_proposal(p)
        except Exception as e:
            _write_pending({"pid": pid, "table": fn,
                            "backup": os.path.relpath(bak, paths.ROOT).replace("\\", "/"),
                            "stage": "proposal_status_failed", "at": _now(),
                            "error": str(e), "knowledge_version": vid})
            raise SystemExit("❌ 提案状态更新失败（表与快照 %s 已生效）。"
                             "见 knowledge/commit_pending.json" % vid)
        _clear_pending()
        print("✅ 提案 %s 已生效，知识表版本 %s" % (pid, vid))
        return vid


def _errata_self_check(p):
    """表内结构自检（只读 p，不依赖全局 ROOT）。

    为什么不直接调 `errata_audit.py --test`：那个脚本把 ROOT 写死，读的是生产表；
    在 `KIKOERU_STYLE` 重定向（沙箱/试跑）时会验错文件，给出**假通过**。
    所以这里先做结构检查，只有确认用的是生产表时才追加跑那个全库门禁。
    """
    d = json.load(open(p, encoding="utf-8"))
    es = [e for e in (d.get("entries") or []) if e.get("ja") and e.get("fix")]
    probs = []
    for e in es:
        if e.get("mode") == "guard" and not e.get("ctx") and not e.get("guard"):
            probs.append("GUARD_NO_CTX:%s" % e["ja"])
        for key in ("ctx", "block_if"):
            for c in e.get(key) or []:
                try:
                    re.compile(c)
                except Exception as ex:
                    probs.append("BAD_%s:%s(%s)" % (key.upper(), e["ja"], ex))
                if "(?!" in c or "(?<!" in c:
                    probs.append("NEG_ASSERT:%s(%s)" % (e["ja"], c))
        for o in es:
            if o is e:
                continue
            if e["ja"] != o["ja"] and o["ja"] in e["fix"]:
                probs.append("CHAIN:%s→%s" % (e["ja"], o["ja"]))
            if e["ja"] != o["ja"] and e["ja"] in o["ja"] and len(e["ja"]) < len(o["ja"]):
                if o["ja"] not in (e.get("overlap_ok") or []):
                    probs.append("OVERLAP:%s⊂%s" % (e["ja"], o["ja"]))
    return probs


def _post_check(fn, path=None):
    src = path or os.path.join(paths.STYLE, fn)
    if fn == "asr_errata.json":
        probs = _errata_self_check(src)
        if probs:
            return False, "表结构自检未过：" + "; ".join(probs[:6])
        default_style = os.path.join(paths.ROOT, "data", "style")
        # 只有「生产表 + 未重定向」时才跑全库 errata_audit（它把 ROOT 写死）
        if os.path.abspath(src) != os.path.abspath(
                os.path.join(default_style, fn)):
            return True, "表结构自检通过（非生产表，跳过全库 errata_audit）"
        code = subprocess.run([paths.python_exe(),
                               os.path.join(paths.SCRIPTS, "errata_audit.py"), "--test"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if code.returncode != 0:
            return False, (code.stdout or "")[-800:]
        return True, "结构自检 + errata_audit --test 全过"
    try:
        json.load(open(src, encoding="utf-8"))
        return True, "JSON 可解析"
    except Exception as e:
        return False, str(e)


def reject(pid, reason=""):
    p = load_proposal(pid)
    p["status"] = "rejected"
    p["reject_reason"] = reason
    p["rejected_at"] = _now()
    save_proposal(p)
    print("已拒绝 %s：%s" % (pid, reason or "（未写原因）"))


def list_proposals(show_all=False):
    ps = []
    for f in sorted(glob.glob(os.path.join(paths.KNOW_PROPOSALS, "*.json"))):
        try:
            ps.append(json.load(open(f, encoding="utf-8")))
        except Exception:
            pass
    if not show_all:
        ps = [p for p in ps if p.get("status") not in ("approved", "rejected")]
    if not ps:
        print("没有待处理提案")
        return
    print("%-26s %-14s %-12s %s" % ("id", "type", "status", "提案"))
    for p in ps:
        print("%-26s %-14s %-12s %s" % (p["id"], p.get("type", "?"), p.get("status", "?"),
                                        (p.get("proposal") or p.get("ja") or "")[:60]))
