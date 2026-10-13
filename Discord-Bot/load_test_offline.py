"""Offline, non-destructive load test. No network, Discord, or Kingshot API calls."""
import argparse
import asyncio
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT))
from kingshot.repository import JsonRepository
from kingshot.reporting import ReportStore

STORAGE = {
    "users_file": "data/users.json", "players_file": "data/players.json",
    "codes_file": "data/codes.json", "logs_file": "data/logs.jsonl",
}

def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    return result, round((time.perf_counter() - start) * 1000, 2)

async def main(count: int):
    # tempfile is outside of the actual production data directory.
    with tempfile.TemporaryDirectory(prefix="dmd-offline-benchmark-") as tmp:
        scratch = Path(tmp)
        repo = JsonRepository(scratch, STORAGE)
        uid_list = [str(890000000 + i) for i in range(count)]
        t0 = time.perf_counter()
        added = 0
        for index in range(0, count, 50):
            result = await repo.bulk_register("999000111222333444", uid_list[index:index + 50], kingdom=1850)
            added += len(result["added"])
        registration_ms = (time.perf_counter() - t0) * 1000
        assert added == count
        t0 = time.perf_counter()
        dup = await repo.bulk_register("999000111222333444", uid_list[:50], kingdom=1850)
        duplicates_ms = (time.perf_counter() - t0) * 1000
        assert not dup["added"] and len(dup["existing"]) == 50
        t0 = time.perf_counter()
        all_players = await repo.list_all_players()
        listing_ms = (time.perf_counter() - t0) * 1000
        assert len(all_players) == count
        await repo.update_code_cache(["OFFLINEBENCH2026"])
        t0 = time.perf_counter()
        for uid in uid_list:
            await repo.mark_redeemed(uid, "OFFLINEBENCH2026", {"message": "simulated; no external request"})
        redeemed_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        summary = await ReportStore(repo).summary("OFFLINEBENCH2026")
        report_ms = (time.perf_counter() - t0) * 1000
        assert summary["total"] == count and summary["accepted_unknown"] == count
        await repo.mark_code_expired("OFFLINEBENCH2026", "Offline test expired")
        assert "OFFLINEBENCH2026" in await repo.list_unredeemable_codes()
        await repo.set_withdrawn(uid_list[0], True)
        assert len(await repo.list_auto_redeem_players()) == count - 1
        await repo.set_withdrawn(uid_list[0], False)
        t0 = time.perf_counter()
        archive_path = scratch / "benchmark-backup.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted((scratch / "data").glob("*")):
                if p.is_file():
                    z.write(p, arcname=f"data/{p.name}")
        with zipfile.ZipFile(archive_path) as z:
            assert z.testzip() is None
        backup_ms = (time.perf_counter() - t0) * 1000
        result = {
            "mode": "OFFLINE ONLY", "simulated_uids": count, "external_api_requests": 0,
            "registration_ms": round(registration_ms, 2),
            "duplicate_50_ms": round(duplicates_ms, 2),
            "list_all_ms": round(listing_ms, 2),
            "write_redeemed_ms": round(redeemed_ms, 2),
            "report_summary_ms": round(report_ms, 2),
            "archive_and_verify_ms": round(backup_ms, 2),
            "passed": True,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # JSON report alone, no IDs; temp database removed automatically.
        return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=100, choices=[100, 500])
    args = parser.parse_args()
    asyncio.run(main(args.count))
