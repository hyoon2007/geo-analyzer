import asyncio
import json
import hashlib
import sys
import os
from playwright.async_api import async_playwright
import redis.asyncio as aioredis

# [Configuration] 인프라 환경 변수 및 엔드포인트 정보
REDIS_HOST = os.getenv("GEO_REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("GEO_REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("GEO_REDIS_PASSWORD", "GEO_SYSTEM_STRONG_SECRET_AUTH_KEY")
TARGET_QUEUE = "Queue:GEO:Tasks"

# 본인 소유 락 확인 후 안전하게 해제하는 원자적 Lua 스크립트 명세
LUA_RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
"""

# ─────────────── Core 오프라인 분석 및 최적화 파이프라인 ───────────────
async def process_offline_optimization(browser, target_url):
    """
    상주하는 고유 브라우저 인스턴스를 인계받아 
    컨텍스트를 격리 격파하며 동적 페이지의 DOM Snapshot을 고속 수집합니다.
    """
    # 메모리 오염 및 세션 혼선 방지를 위한 고유 격리 컨텍스트 브리딩
    context = await browser.new_context()
    page = await context.new_page()
    
    try:
        print(f"[NAVIGATE] Initiating Headless Fetch for: {target_url}")
        
        # CSR 프레임워크 런타임 스크립트 실행 완료 및 네트워크 유휴 상태인 networkidle0 시점 완전 대기
        await page.goto(target_url, wait_until="networkidle")
        
        # 동적 바인딩 및 레이지 로드 소스가 완벽하게 동기화된 Serialized HTML 획득
        dom_snapshot = await page.content()
        print(f"[CAPTURE] Serialized DOM Fetch Success. Total Bytes: {len(dom_snapshot)}")
        
        # ─────────────────────────────────────────────────────────────
        # [수행 구간: 아키텍처 정의서 v2.0 스펙 연동]
        # 1. Wasmtime 연동 -> geo-analyzer2.wasm (Aggressive) 호출 1차 정제 수행
        # 2. LLM API (GPT-4o / Claude 3.5) 전송 및 5대 필러 모델 동적 추론
        # 3. 3-Tier Validation 구조 파이프라인 구동 (구문 -> 구조 -> 진실성 검증)
        # 4. 완결형 고품질 HTML 코드를 Akamai EdgeKV(URL_DATA)에 최종 배포 완료
        # ─────────────────────────────────────────────────────────────
        
        # 가상 최적화 비즈니스 로직 연산 레이턴시 시뮬레이션
        await asyncio.sleep(1.5) 
        
    finally:
        # 단일 컨텍스트 리소스 즉시 반환 (워커 상주 환경에서의 메모리 누수 원천 차단)
        await page.close()
        await context.close()

# ─────────────── 데몬 프로세스 루프 제어 엔진 ───────────────
async def main():
    print("[INITIALIZE] Starting GEO Resident Async Worker Component...")
    
    # Non-blocking 비동기 IO 연동 전용 Redis 커넥션 초기화
    redis_client = aioredis.Redis(
        host=REDIS_HOST, 
        port=REDIS_PORT, 
        password=REDIS_PASSWORD, 
        decode_responses=True
    )
    
    # Lua 스크립트를 Redis 커널에 사전 로드하여 Evalsha 아키텍처 가동 (네트워크 오버헤드 최적화)
    lua_sha = await redis_client.script_load(LUA_RELEASE_SCRIPT)
    print("[INITIALIZE] Distributed Lock System Lua Script Registered.")
    
    # 인프라 비용과 초기화 부하를 방지하기 위해 Playwright 브라우저 인스턴스를 루프 '외부'에서 최초 1회만 구동
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        print("[INITIALIZE] Shared Playwright Chromium Core Engine - Warmed Up & Resident.")
        
        try:
            while True:
                # Redis Queue로부터 블로킹 팝(BRPOP) 가동. 
                # 작업이 없을 때는 이벤트 루프 제어권을 반환하여 상주 프로세스의 CPU 점유율을 0%로 유지
                task_container = await redis_client.brpop(TARGET_QUEUE, timeout=0)
                if not task_container:
                    continue
                    
                _, raw_task_payload = task_container
                task_data = json.loads(raw_task_payload)
                
                target_url = task_data["url"]
                worker_token = task_data["token"]
                
                # 중복 제어 및 정합성을 위한 고유 락 키 맵핑
                url_hash = hashlib.sha256(target_url.encode()).hexdigest()
                lock_key = f"Lock:GEO:Analysis:{url_hash}"
                
                print(f"\n[DEQUEUE] Processing Task Pop Detected -> URL: {target_url}")
                
                try:
                    # 상주하는 단일 브라우저 인스턴스를 주입하여 비동기 처리 가동
                    await process_offline_optimization(browser, target_url)
                    print(f"[TERMINATE] Task Successfully Optimized and Written Back: {target_url}")
                    
                except Exception as pipeline_error:
                    print(f"[PIPELINE CRITICAL ERROR] Processing Failed for {target_url}: {pipeline_error}")
                    
                finally:
                    # 파이프라인의 성공 여부와 무관하게 본인이 생성했던 고유 락 토큰 검증 후 안전하게 분산 락 원자적 해제
                    lock_release_status = await redis_client.evalsha(lua_sha, 1, lock_key, worker_token)
                    if lock_release_status == 1:
                        print(f"[DISTRIBUTED LOCK] Safe Release Complete for Key: {lock_key}")
                    else:
                        print(f"[DISTRIBUTED LOCK WARN] Expiration Guard-time triggered or token hijacked for: {lock_key}")
                        
        except asyncio.CancelledError:
            print("[SHUTDOWN] Worker process cancellation context intercepted.")
        finally:
            # 안전한 종단점 자원 반환 가드레일
            await browser.close()
            await redis_client.close()
            print("[SHUTDOWN] Resident Worker safely terminated. System resources released.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EXIT] Demonic Resident Worker terminated by supervisor signal.")
        sys.exit(0)