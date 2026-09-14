#!/usr/bin/env python3
# DaVinci V0 Production API compatibility test
# API key is read from DVC_API_KEY and is never printed.

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("DVC_BASE_URL", "https://prod.dvcbot.net/api/assts/v1").rstrip("/")
API_KEY = os.getenv("DVC_API_KEY", "").strip()
ASSISTANT_ID = os.getenv("DVC_ASSISTANT_ID", "asst_dvc_o2KvewHb9c1XLXlHsFypCe8N").strip()
PROMPT = os.getenv("DVC_TEST_PROMPT", "請只回答：DaVinci Production API 測試成功。")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "OpenAI-Beta": "assistants=v2",
    "Accept": "application/json",
}


def request(method, path, payload=None, timeout=30):
    url = f"{BASE_URL}{path}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, dict(e.headers), raw
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {e}"


def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def show_response(label, status, headers, raw):
    print(f"\n--- {label} ---")
    print(f"HTTP: {status}")
    if status in (301, 302, 303, 307, 308):
        print(f"Location: {headers.get('Location', '')}")
    obj = parse_json(raw)
    if obj is not None:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:6000])
    else:
        print(raw[:6000])


def fail(code, message):
    print(f"\n❌ {message}")
    sys.exit(code)


def main():
    print("========================================")
    print("DaVinci V0 Production API 官方相容性測試")
    print("========================================")
    print(f"Base URL: {BASE_URL}")
    print(f"Assistant ID: {ASSISTANT_ID}")
    print("API Key: 已由 GitHub Secret 提供（不顯示）")

    if not API_KEY:
        fail(2, "缺少 DVC_API_KEY。請在 GitHub Repository Secrets 建立此 Secret。")
    if not ASSISTANT_ID:
        fail(2, "缺少 DVC_ASSISTANT_ID。")

    # 1. Create thread
    status, headers, raw = request("POST", "/threads", {})
    show_response("1/5 Create Thread", status, headers, raw)
    if status in (301, 302, 303, 307, 308):
        print("\n🚨 結論：Production endpoint 發生 HTTP redirect。")
        print("本測試刻意不跟隨 redirect，以便確認是否被導向 Sandbox。")
        sys.exit(10)
    if status is None or not (200 <= status < 300):
        fail(11, "Create Thread 失敗。請看上面的 HTTP 與回應內容。")

    thread = parse_json(raw) or {}
    thread_id = thread.get("id")
    if not thread_id:
        fail(12, "Create Thread 成功但找不到 thread id。")
    print(f"Thread ID: {thread_id}")

    # 2. Create message
    status, headers, raw = request(
        "POST",
        f"/threads/{thread_id}/messages",
        {"role": "user", "content": PROMPT},
    )
    show_response("2/5 Create Message", status, headers, raw)
    if status in (301, 302, 303, 307, 308):
        fail(13, "Message endpoint 發生 redirect。")
    if status is None or not (200 <= status < 300):
        fail(14, "Create Message 失敗。")

    # 3. Create run
    status, headers, raw = request(
        "POST",
        f"/threads/{thread_id}/runs",
        {"assistant_id": ASSISTANT_ID},
    )
    show_response("3/5 Create Run", status, headers, raw)
    if status in (301, 302, 303, 307, 308):
        fail(15, "Run endpoint 發生 redirect。")
    if status is None or not (200 <= status < 300):
        fail(16, "Create Run 失敗；若是 404 assistant not found，代表 Production runtime 找不到該 Assistant。")

    run = parse_json(raw) or {}
    run_id = run.get("id")
    if not run_id:
        fail(17, "Create Run 成功但找不到 run id。")

    # 4. Poll run
    final_run = run
    for attempt in range(1, 61):
        status, headers, raw = request("GET", f"/threads/{thread_id}/runs/{run_id}")
        obj = parse_json(raw) or {}
        state = obj.get("status")
        print(f"Poll {attempt:02d}/60: HTTP={status} status={state}")
        if status in (301, 302, 303, 307, 308):
            fail(18, "Run polling 發生 redirect。")
        if status is None or not (200 <= status < 300):
            fail(19, "Run polling 失敗。")
        final_run = obj
        if state in {"completed", "failed", "cancelled", "expired", "requires_action"}:
            break
        time.sleep(2)

    state = final_run.get("status")
    print(f"\nRun final status: {state}")

    if state == "requires_action":
        print("\n✅ Assistant 已被 Production runtime 找到。")
        print("⚠️ Run 進入 requires_action，代表下一步需要處理工具/Plugin 呼叫。")
        print(json.dumps(final_run.get("required_action"), ensure_ascii=False, indent=2)[:10000])
        sys.exit(20)

    if state != "completed":
        print(json.dumps(final_run, ensure_ascii=False, indent=2)[:10000])
        fail(21, "Run 沒有 completed。")

    # 5. Read messages
    status, headers, raw = request("GET", f"/threads/{thread_id}/messages?order=desc")
    show_response("5/5 Get Messages", status, headers, raw)
    if status is None or not (200 <= status < 300):
        fail(22, "取得 Assistant 回覆失敗。")

    obj = parse_json(raw) or {}
    answer = ""
    for item in obj.get("data", []):
        if item.get("role") != "assistant":
            continue
        for content in item.get("content", []):
            if content.get("type") == "text":
                answer = (content.get("text") or {}).get("value", "")
                if answer:
                    break
        if answer:
            break

    print("\n========================================")
    print("🎉 DaVinci Production API 測試成功")
    print("========================================")
    print("Assistant 回覆：")
    print(answer or "（API 已 completed，但未解析到文字內容，請查看上方 Messages JSON。）")
    print("\n結論：Production Thread → Message → Run → Completed → Message 回覆流程可用。")

    # Best-effort cleanup; failure does not change a successful test result.
    status, _, _ = request("DELETE", f"/threads/{thread_id}")
    print(f"Cleanup Thread: HTTP={status}")


if __name__ == "__main__":
    main()
