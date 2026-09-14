#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DaVinci V0 Production API Compatibility / Authentication Diagnostic Test

用途：
1. 驗證 GitHub Secret DAVINCI_API_KEY 是否真的進入 Python。
2. 驗證 DaVinci Production API 是否接受目前的 API Key。
3. 官方方式優先：
       Authorization: Bearer <API_KEY>
4. 若官方 Bearer 回傳：
       401 login failed: no authorization provided
   則自動測試其他常見 API Key header：
       X-API-Key
       api-key
       Authorization: <API_KEY>
5. 找到可用的認證方式後，繼續：
       Create Thread
       Create Message
       Create Run
       Poll Run
       Get Messages
6. 絕不印出 API Key 本身。

環境變數：
    DAVINCI_API_KEY
    DVC_BASE_URL
    DVC_ASSISTANT_ID
    DVC_TEST_PROMPT
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


# ============================================================
# Configuration
# ============================================================

BASE_URL = os.getenv(
    "DVC_BASE_URL",
    "https://prod.dvcbot.net/api/assts/v1"
).rstrip("/")

API_KEY = os.getenv(
    "DAVINCI_API_KEY",
    ""
).strip()

ASSISTANT_ID = os.getenv(
    "DVC_ASSISTANT_ID",
    "asst_dvc_o2KvewHb9c1XLXlHsFypCe8N"
).strip()

PROMPT = os.getenv(
    "DVC_TEST_PROMPT",
    "請只回答：DaVinci Production API 測試成功。"
)


# ============================================================
# Common headers
# ============================================================

COMMON_HEADERS = {
    "Content-Type": "application/json",
    "OpenAI-Beta": "assistants=v2",
    "Accept": "application/json",
    "User-Agent": "davinci-prod-official-test/1.0",
}


# ============================================================
# Authentication candidates
#
# 第一個就是 DaVinci 官方文件使用的 Bearer。
# 後面幾個只在 Bearer 被 gateway 判定為
# "no authorization provided" 時作診斷。
# ============================================================

def build_auth_candidates():
    return [
        (
            "Authorization: Bearer",
            {
                **COMMON_HEADERS,
                "Authorization": f"Bearer {API_KEY}",
            },
        ),
        (
            "X-API-Key",
            {
                **COMMON_HEADERS,
                "X-API-Key": API_KEY,
            },
        ),
        (
            "api-key",
            {
                **COMMON_HEADERS,
                "api-key": API_KEY,
            },
        ),
        (
            "Authorization: raw",
            {
                **COMMON_HEADERS,
                "Authorization": API_KEY,
            },
        ),
    ]


# ============================================================
# HTTP request
# ============================================================

def request(
    method,
    path,
    payload=None,
    headers=None,
    timeout=30,
):
    url = f"{BASE_URL}{path}"

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

    req_headers = dict(headers or COMMON_HEADERS)

    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout
        ) as resp:

            raw = resp.read().decode(
                "utf-8",
                errors="replace"
            )

            return (
                resp.status,
                dict(resp.headers),
                raw,
            )

    except urllib.error.HTTPError as e:

        raw = e.read().decode(
            "utf-8",
            errors="replace"
        )

        return (
            e.code,
            dict(e.headers),
            raw,
        )

    except Exception as e:

        return (
            None,
            {},
            f"{type(e).__name__}: {e}",
        )


# ============================================================
# JSON parser
# ============================================================

def parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


# ============================================================
# Safe response printer
# ============================================================

def show_response(
    label,
    status,
    headers,
    raw,
    max_chars=6000,
):
    print()
    print(f"--- {label} ---")
    print(f"HTTP: {status}")

    if status in (
        301,
        302,
        303,
        307,
        308,
    ):
        location = headers.get(
            "Location",
            ""
        )

        print(
            f"Location: {location}"
        )

    obj = parse_json(raw)

    if obj is not None:
        print(
            json.dumps(
                obj,
                ensure_ascii=False,
                indent=2
            )[:max_chars]
        )
    else:
        print(
            raw[:max_chars]
        )


# ============================================================
# Error exit
# ============================================================

def fail(code, message):
    print()
    print(f"❌ {message}")
    sys.exit(code)


# ============================================================
# Authentication diagnostic
# ============================================================

def diagnose_authentication():
    """
    先只打 Create Thread。

    原因：
    如果 Create Thread 就 401，
    沒必要繼續測 Thread / Message / Run。

    回傳：
        (auth_name, headers)

    如果全部失敗：
        return (None, None)
    """

    print()
    print("========================================")
    print("DaVinci Production Authentication Diagnostic")
    print("========================================")
    print(
        "目的：確認 Production API 實際接受哪種認證方式"
    )
    print()

    candidates = build_auth_candidates()

    results = []

    for index, (
        auth_name,
        headers,
    ) in enumerate(
        candidates,
        start=1
    ):

        print(
            f"\n[{index}/{len(candidates)}] "
            f"測試認證方式：{auth_name}"
        )

        status, response_headers, raw = request(
            "POST",
            "/threads",
            {},
            headers=headers,
        )

        print(
            f"HTTP: {status}"
        )

        if status in (
            301,
            302,
            303,
            307,
            308,
        ):
            print(
                "Redirect Location:",
                response_headers.get(
                    "Location",
                    ""
                ),
            )

        obj = parse_json(raw)

        if obj is not None:
            print(
                json.dumps(
                    obj,
                    ensure_ascii=False,
                    indent=2
                )[:3000]
            )
        else:
            print(
                raw[:3000]
            )

        results.append(
            (
                auth_name,
                status,
                raw,
            )
        )

        # ----------------------------------------------------
        # 成功
        # ----------------------------------------------------

        if status is not None and 200 <= status < 300:

            print()
            print(
                "✅ 認證成功：",
                auth_name
            )

            return (
                auth_name,
                headers,
            )

        # ----------------------------------------------------
        # Redirect
        # ----------------------------------------------------

        if status in (
            301,
            302,
            303,
            307,
            308,
        ):

            print()
            print(
                "🚨 Production endpoint 發生 Redirect。"
            )

            print(
                "本測試刻意不自動跟隨 Redirect，"
                "避免 POST 被錯誤導向 Sandbox。"
            )

            # Redirect 本身已經是重要診斷資訊，
            # 但如果只是某一認證方式的 redirect，
            # 還是繼續測其他 authentication。
            continue

        # ----------------------------------------------------
        # 如果是一般 HTTP 錯誤
        # 繼續下一種 authentication。
        # ----------------------------------------------------

        print(
            f"⚠️ {auth_name} 不成功，"
            "繼續測試下一種認證方式。"
        )

    # ========================================================
    # 全部失敗
    # ========================================================

    print()
    print("========================================")
    print("❌ 所有認證方式皆失敗")
    print("========================================")

    print()
    print("測試結果摘要：")

    for auth_name, status, raw in results:

        print(
            f"- {auth_name}: HTTP={status}"
        )

        obj = parse_json(raw)

        if isinstance(obj, dict):

            msg = (
                obj.get("msg")
                or obj.get("message")
                or obj.get("error")
            )

            if msg:
                print(
                    f"  回應：{msg}"
                )

    print()
    print("目前最重要的判斷：")

    bearer_no_auth = False

    for auth_name, status, raw in results:

        if (
            auth_name == "Authorization: Bearer"
            and status == 401
            and "no authorization provided" in raw.lower()
        ):
            bearer_no_auth = True

    if bearer_no_auth:

        print(
            "⚠️ Production API 回報 "
            "\"no authorization provided\"。"
        )

        print(
            "這代表伺服器端沒有把 Bearer Authorization "
            "視為有效認證。"
        )

        print()
        print(
            "如果 X-API-Key / api-key 也同樣 401，"
        )

        print(
            "則問題高度集中在 DaVinci Production "
            "API Gateway / Authentication middleware。"
        )

    return (
        None,
        None,
    )


# ============================================================
# Main API flow
# ============================================================

def main():

    print("========================================")
    print("DaVinci V0 Production API 官方相容性測試")
    print("========================================")

    print(
        f"Base URL: {BASE_URL}"
    )

    print(
        f"Assistant ID: {ASSISTANT_ID}"
    )

    print(
        "API Key: 已由 GitHub Secret 提供（不顯示）"
    )

    print(
        f"API Key length: {len(API_KEY)}"
    )

    print()

    # --------------------------------------------------------
    # Validate environment
    # --------------------------------------------------------

    if not API_KEY:

        fail(
            2,
            "缺少 DAVINCI_API_KEY。"
            "請確認 GitHub Repository Secrets 中已有此 Secret。"
        )

    if not ASSISTANT_ID:

        fail(
            2,
            "缺少 DVC_ASSISTANT_ID。"
        )

    # --------------------------------------------------------
    # Step 0
    # Authentication diagnostic
    # --------------------------------------------------------

    auth_name, auth_headers = (
        diagnose_authentication()
    )

    if not auth_name:

        fail(
            11,
            "DaVinci Production API 認證測試全部失敗。"
        )

    print()
    print(
        f"目前採用認證方式：{auth_name}"
    )

    # --------------------------------------------------------
    # Step 1
    # Create Thread
    #
    # 因為診斷階段已成功建立一次 Thread，
    # 這裡重新建立一個乾淨 Thread，
    # 確保後續流程是完整獨立測試。
    # --------------------------------------------------------

    status, headers, raw = request(
        "POST",
        "/threads",
        {},
        headers=auth_headers,
    )

    show_response(
        "1/5 Create Thread",
        status,
        headers,
        raw,
    )

    if status in (
        301,
        302,
        303,
        307,
        308,
    ):

        fail(
            10,
            "Create Thread 發生 Redirect。"
        )

    if status is None or not (
        200 <= status < 300
    ):

        fail(
            11,
            "Create Thread 失敗。"
        )

    thread = parse_json(raw) or {}

    thread_id = thread.get(
        "id"
    )

    if not thread_id:

        fail(
            12,
            "Create Thread 成功但找不到 thread id。"
        )

    print(
        f"Thread ID: {thread_id}"
    )

    # --------------------------------------------------------
    # Step 2
    # Create Message
    # --------------------------------------------------------

    status, headers, raw = request(
        "POST",
        f"/threads/{thread_id}/messages",
        {
            "role": "user",
            "content": PROMPT,
        },
        headers=auth_headers,
    )

    show_response(
        "2/5 Create Message",
        status,
        headers,
        raw,
    )

    if status in (
        301,
        302,
        303,
        307,
        308,
    ):

        fail(
            13,
            "Message endpoint 發生 Redirect。"
        )

    if status is None or not (
        200 <= status < 300
    ):

        fail(
            14,
            "Create Message 失敗。"
        )

    # --------------------------------------------------------
    # Step 3
    # Create Run
    # --------------------------------------------------------

    status, headers, raw = request(
        "POST",
        f"/threads/{thread_id}/runs",
        {
            "assistant_id": ASSISTANT_ID
        },
        headers=auth_headers,
    )

    show_response(
        "3/5 Create Run",
        status,
        headers,
        raw,
    )

    if status in (
        301,
        302,
        303,
        307,
        308,
    ):

        fail(
            15,
            "Run endpoint 發生 Redirect。"
        )

    if status is None or not (
        200 <= status < 300
    ):

        fail(
            16,
            "Create Run 失敗。"
            "若是 404 assistant not found，"
            "代表 Production runtime 找不到該 Assistant。"
        )

    run = parse_json(raw) or {}

    run_id = run.get(
        "id"
    )

    if not run_id:

        fail(
            17,
            "Create Run 成功但找不到 run id。"
        )

    print(
        f"Run ID: {run_id}"
    )

    # --------------------------------------------------------
    # Step 4
    # Poll Run
    # --------------------------------------------------------

    final_run = run

    for attempt in range(
        1,
        61
    ):

        status, headers, raw = request(
            "GET",
            f"/threads/{thread_id}/runs/{run_id}",
            None,
            headers=auth_headers,
        )

        obj = parse_json(raw) or {}

        state = obj.get(
            "status"
        )

        print(
            f"Poll {attempt:02d}/60: "
            f"HTTP={status} "
            f"status={state}"
        )

        if status in (
            301,
            302,
            303,
            307,
            308,
        ):

            fail(
                18,
                "Run polling 發生 Redirect。"
            )

        if status is None or not (
            200 <= status < 300
        ):

            show_response(
                "Run polling error",
                status,
                headers,
                raw,
            )

            fail(
                19,
                "Run polling 失敗。"
            )

        final_run = obj

        if state in {
            "completed",
            "failed",
            "cancelled",
            "expired",
            "requires_action",
        }:

            break

        time.sleep(2)

    state = final_run.get(
        "status"
    )

    print()
    print(
        f"Run final status: {state}"
    )

    # --------------------------------------------------------
    # requires_action
    #
    # 這其實也是很重要的成功訊號：
    # Authentication 成功 + Assistant 已找到。
    # --------------------------------------------------------

    if state == "requires_action":

        print()
        print(
            "✅ Assistant 已被 Production runtime 找到。"
        )

        print(
            "⚠️ Run 進入 requires_action。"
        )

        print(
            "代表下一步需要處理 Tool / Plugin 呼叫。"
        )

        print()

        print(
            json.dumps(
                final_run.get(
                    "required_action"
                ),
                ensure_ascii=False,
                indent=2,
            )[:10000]
        )

        sys.exit(20)

    # --------------------------------------------------------
    # Other non-completed states
    # --------------------------------------------------------

    if state != "completed":

        print()

        print(
            json.dumps(
                final_run,
                ensure_ascii=False,
                indent=2,
            )[:10000]
        )

        fail(
            21,
            "Run 沒有 completed。"
        )

    # --------------------------------------------------------
    # Step 5
    # Get Messages
    # --------------------------------------------------------

    status, headers, raw = request(
        "GET",
        f"/threads/{thread_id}/messages?order=desc",
        None,
        headers=auth_headers,
    )

    show_response(
        "5/5 Get Messages",
        status,
        headers,
        raw,
    )

    if status is None or not (
        200 <= status < 300
    ):

        fail(
            22,
            "取得 Assistant 回覆失敗。"
        )

    obj = parse_json(raw) or {}

    answer = ""

    for item in obj.get(
        "data",
        []
    ):

        if item.get(
            "role"
        ) != "assistant":

            continue

        for content in item.get(
            "content",
            []
        ):

            if content.get(
                "type"
            ) != "text":

                continue

            text_obj = (
                content.get(
                    "text"
                )
                or {}
            )

            answer = (
                text_obj.get(
                    "value"
                )
                or ""
            )

            if answer:
                break

        if answer:
            break

    # --------------------------------------------------------
    # Success
    # --------------------------------------------------------

    print()
    print("========================================")
    print("🎉 DaVinci Production API 測試成功")
    print("========================================")

    print(
        f"Authentication: {auth_name}"
    )

    print(
        "Assistant 回覆："
    )

    print(
        answer
        or
        "（API 已 completed，但未解析到文字內容，"
        "請查看上方 Messages JSON。）"
    )

    print()
    print(
        "結論："
    )

    print(
        "Production Thread → Message → Run "
        "→ Completed → Message 回覆流程可用。"
    )

    # --------------------------------------------------------
    # Cleanup
    # --------------------------------------------------------

    try:

        cleanup_status, _, _ = request(
            "DELETE",
            f"/threads/{thread_id}",
            None,
            headers=auth_headers,
        )

        print(
            f"Cleanup Thread: HTTP={cleanup_status}"
        )

    except Exception as e:

        print(
            f"Cleanup skipped: {type(e).__name__}: {e}"
        )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()
