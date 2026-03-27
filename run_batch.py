# -*- coding: utf-8 -*-
"""batch v3: sequential step3 restart -> scheduler ON"""
import asyncio
import aiohttp
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = "http://localhost:8000/api"

TASKS = [
    ("3ec17b1c-3e53-46e5-978d-f25ce399bd93", "reban-empty-chair"),
    ("ada1911b-dc26-4758-8b38-15d057ea8fc1", "memory-compress-melody"),
    ("a82725dc-0c5a-4fb5-b055-8981f4055619", "silence-first-bat"),
]


async def wait_free(session):
    while True:
        async with session.get(f"{BASE}/pipeline/status") as r:
            data = await r.json()
        if not data.get("running"):
            return
        print(f"  lock held, waiting...")
        await asyncio.sleep(15)


async def wait_done(session, pid, name):
    print(f"  [{name}] waiting...")
    while True:
        async with session.get(f"{BASE}/projects/{pid}") as r:
            data = await r.json()
            st = data.get("status", "")
        if st == "done":
            print(f"  [{name}] DONE")
            return True
        if st == "failed":
            print(f"  [{name}] FAILED: {data.get('error_msg','')}")
            return False
        await asyncio.sleep(15)


async def main():
    print("BATCH START")

    async with aiohttp.ClientSession() as session:
        for pid, name in TASKS:
            print(f"\n>> {name}: step 3 reset")
            await wait_free(session)

            url = f"{BASE}/pipeline/resume/{pid}?from_step=3&reset=true"
            async with session.post(url) as r:
                result = await r.json()
            if not result.get("ok"):
                print(f"  FAILED: {result}")
                continue
            print(f"  started")

            ok = await wait_done(session, pid, name)
            if not ok:
                print(f"  [{name}] failed, continuing")
            await asyncio.sleep(3)

        # scheduler ON
        print("\n>> scheduler ON")
        await wait_free(session)
        async with session.post(f"{BASE}/feedback/schedule",
                                json={"enabled": True, "interval_hours": 2.0}) as r:
            print(f"  result: {await r.json()}")

    print("\nBATCH DONE")


if __name__ == "__main__":
    asyncio.run(main())
